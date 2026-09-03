"""Stateless public chat facade around the evidence-backed immersive engine."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import threading
import tempfile
import time
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import PublicSettings, Settings
from .mvp_policy import MVP_CHARACTERS, scene_visual_key
from .mvp_service import (
    MVPService,
    _SESSION_LOCK,
    _SESSION_STATES,
    _WORLD_STATE_LOCK,
    _WORLD_STATES,
    _normalize_stage_motion,
)
from .provider_registry import ProviderRegistry
from .public_contracts import (
    ChatRequest,
    HistoryTurn,
    PendingRendezvous,
    PresenceArrivalRequest,
    PresenceResolveRequest,
    PresenceTransitionRequest,
    StateEvent,
    StatePayload,
    StateUpdateProposal,
    SummarizeRequest,
)
from .public_media import PublicMediaCatalog
from .public_stickers import PublicStickerCatalog
from .public_providers import ProviderHTTPPool, ProviderSpec, simple_completion
from .public_repository import PublicRuntimeRepository
from .public_security import (
    PublicSecurityError,
    redact_sensitive_text,
    safe_output,
    sign_state,
    verify_state,
)


class StatelessConversationStore:
    """Deliberately inert replacement for the internal SQLite chat store."""

    def duplicate_response(self, _request_key: str | None) -> None:
        return None

    def claim_request(self, _request_key: str, _character_id: str) -> bool:
        return True

    def release_request(self, _request_key: str | None) -> None:
        return None

    def session_state(self, _session_id: str) -> None:
        return None

    def world_state(self, _world_session_id: str) -> None:
        return None


class GenerationBusy(RuntimeError):
    pass


class CharacterUnavailable(RuntimeError):
    pass


_PUBLIC_CONTENT_CHARACTER_LIMIT = 1200
_PUBLIC_JSON_CANDIDATE_LIMIT = 16


# A locally generated answer is exposed only after the MVP has run the same
# final hard-guard pass as a provider answer.  Recoverable empty/malformed
# envelopes therefore no longer become a terminal public error merely because
# the safe fallback replaced the whole answer.
_PUBLIC_WHOLE_ANSWER_FALLBACKS: frozenset[str] = frozenset()

# Diagnostics exposed through the public idempotency record describe the
# answer actually shown to the user. State transitions, style-profile lookup,
# and other bookkeeping adjustments must not make an otherwise untouched
# provider response look "normalized".
_PUBLIC_REWRITE_ADJUSTMENTS = frozenset({
    "answer_guardrail_retry",
    "empty_output_recovery",
    "in_person_block_rewrite",
})
_PUBLIC_NORMALIZATION_ADJUSTMENTS = frozenset({
    "explicit_relationship_guard",
    "latest_state_guard",
    "in_person_blocks_reclassified",
    "address_alias_normalized",
    "relationship_address_normalized",
    "unsupported_quote_sanitized",
    "public_punctuation_normalized",
})


def _public_immersive_thinking_decision(provider: ProviderSpec) -> dict[str, Any]:
    """Build the complete provider request contract for public dialogue."""

    return {
        "requested": "off",
        "effective": "off",
        "reason": "public_immersive_policy",
        "provider_kind": provider.provider_id,
        "request_fields": ProviderRegistry.thinking_request_fields(
            provider.provider_id,
            "off",
        ),
        "max_provider_http_calls": 2,
        "disable_compatibility_retries": True,
    }


class GenerationGate:
    def __init__(self, active: int = 4, queued: int = 8, queue_timeout: float = 30):
        self.semaphore = asyncio.Semaphore(active)
        self.queued_limit = queued
        self.queue_timeout = queue_timeout
        self._lock = asyncio.Lock()
        self._waiting = 0

    @contextmanager
    def _noop(self) -> Iterator[None]:
        yield

    async def acquire(self) -> None:
        async with self._lock:
            if self.semaphore.locked() and self._waiting >= self.queued_limit:
                raise GenerationBusy("generation_queue_full")
            self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=self.queue_timeout)
            except TimeoutError as exc:
                raise GenerationBusy("generation_queue_timeout") from exc
        finally:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)

    def release(self) -> None:
        self.semaphore.release()

    async def run(self, callback):
        await self.acquire()
        try:
            return await callback()
        finally:
            self.release()


def _history_turns(history: list[HistoryTurn]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_user: HistoryTurn | None = None
    for item in history[-24:]:
        if item.role == "user":
            pending_user = item
        elif pending_user:
            user_content = pending_user.content or "\n".join(
                f"[发送表情：{block.caption or block.asset_id}]"
                if block.type == "sticker"
                else block.text
                for block in pending_user.content_blocks
            )
            assistant_content = item.content or "\n".join(
                f"[发送表情：{block.caption or block.asset_id}]"
                if block.type == "sticker"
                else block.text
                for block in item.content_blocks
            )
            turns.append(
                {
                    "user": user_content,
                    "assistant": assistant_content,
                    "communication_channel": item.communication_channel,
                    "analyst_content_blocks": [
                        block.model_dump() for block in pending_user.content_blocks
                    ],
                    "content_blocks": [block.model_dump() for block in item.content_blocks],
                    "created_at": item.created_at,
                    "mode": "immersive",
                }
            )
            pending_user = None
    return turns[-12:]


def _trim_content_blocks(
    blocks: list[dict[str, Any]],
    *,
    limit: int = _PUBLIC_CONTENT_CHARACTER_LIMIT,
) -> tuple[list[dict[str, Any]], bool]:
    remaining = limit
    trimmed: list[dict[str, str]] = []
    truncated = False
    for block in blocks:
        if str(block.get("type") or "") == "sticker":
            if not any(item.get("type") == "sticker" for item in trimmed):
                trimmed.append({
                    "type": "sticker",
                    "asset_id": str(block.get("asset_id") or ""),
                    "caption": str(block.get("caption") or ""),
                    "src": str(block.get("src") or ""),
                    "thumbnail_src": str(block.get("thumbnail_src") or ""),
                    "display_src": str(block.get("display_src") or ""),
                    "display_mime_type": str(block.get("display_mime_type") or ""),
                    "display_animated": bool(block.get("display_animated")),
                    "animated": bool(block.get("animated")),
                })
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        separator_cost = 1 if trimmed else 0
        available = max(0, remaining - separator_cost)
        if available <= 0:
            truncated = True
            break
        if len(text) > available:
            text = text[:available]
            truncated = True
        trimmed.append({"type": str(block["type"]), "text": text})
        remaining -= len(text) + separator_cost
        if truncated:
            break
    return trimmed, truncated


def _render_blocks(blocks: list[dict[str, str]]) -> str:
    return "\n".join(
        (
            f"[表情：{block.get('caption') or block.get('asset_id')}]"
            if block.get("type") == "sticker"
            else str(block.get("text") or "").strip()
        )
        for block in blocks
        if block.get("type") == "sticker" or str(block.get("text") or "").strip()
    ).strip()


def _model_input_from_blocks(blocks: list[dict[str, Any]], fallback: str = "") -> str:
    rendered = []
    for block in blocks:
        if block.get("type") == "sticker":
            rendered.append(f"[分析员发送了表情：{block.get('caption') or block.get('asset_id')}]")
        elif str(block.get("text") or "").strip():
            rendered.append(str(block["text"]).strip())
    return "\n".join(rendered).strip() or fallback.strip()


_STICKER_FILENAME_PATTERN = re.compile(
    r"(?<![\w-])[^\s，。！？!?；;（）()\[\]【】]{1,80}\.(?:gif|png|jpe?g|webp)(?![\w-])",
    re.IGNORECASE,
)

_PUBLIC_LITERAL_PATTERN = re.compile(
    r"```.*?(?:```|\Z)"
    r"|~~~.*?(?:~~~|\Z)"
    r"|`[^`\r\n]*`"
    r"|(?:https?|ftp)://[^\s<>\"']+"
    r"|\bwww\.[^\s<>\"']+"
    r"|\b(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,}"
    r"(?::\d{1,5})?(?:[/?#][^\s<>\"']*)?"
    r"|(?<![A-Z0-9_])(?:\.\.?/|/|\?[A-Z0-9_.~-]+=)[^\s<>\"']+"
    r"|\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE | re.DOTALL,
)
_PUBLIC_CODE_LINE_PATTERN = re.compile(
    r"(?:^\s*(?:>>>|PS>|\$\s|(?:def|class|return|import|from|const|let|var|"
    r"function|SELECT|INSERT|UPDATE|DELETE|CREATE)\b|[A-Za-z_$][\w.$-]*\s*(?:=|:=)|"
    r"</?[A-Za-z][^>]*>))"
    r"|(?:\b[A-Za-z_$][\w.$]*\s*\([^()\r\n]*[\"'][^()\r\n]*\))",
    re.IGNORECASE,
)
_PUBLIC_ASCII_PUNCTUATION = {
    ",": "，",
    "?": "？",
    "!": "！",
    ":": "：",
    ";": "；",
}
_PUBLIC_LEFT_CONTEXT_SKIP = frozenset(" \t\r\n\"'”’）)]】》」』?!？！")
_PUBLIC_RIGHT_CONTEXT_SKIP = frozenset(" \t\r\n\"'“‘（([【《「『")
_PUBLIC_SENTENCE_TRAIL_SKIP = frozenset(
    " \t\r\n\"'”’）)]】》」』."
)


def _is_han_character(value: str | None) -> bool:
    if not value:
        return False
    point = ord(value)
    return (
        point == 0x3007
        or 0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0xF900 <= point <= 0xFAFF
        or 0x20000 <= point <= 0x3134F
    )


def _is_emoji_symbol(value: str | None) -> bool:
    if not value:
        return False
    point = ord(value)
    return (
        0x2600 <= point <= 0x27BF
        or 0x1F000 <= point <= 0x1FAFF
    )


def _normalize_public_immersive_punctuation(value: str) -> str:
    """Use Chinese punctuation in prose while preserving literal payloads.

    Public model output occasionally mixes an ASCII comma or question mark
    into otherwise Chinese dialogue. The replacement is deliberately
    contextual: comma/colon/semicolon require Han text on both sides, while
    question/exclamation runs may also terminate a Han sentence. URLs,
    emails, Markdown code, code-like lines, and valid embedded JSON are masked
    before that decision so display cleanup cannot mutate literal data.
    """

    original = str(value or "")
    text = original[:_PUBLIC_CONTENT_CHARACTER_LIMIT]
    suffix = original[_PUBLIC_CONTENT_CHARACTER_LIMIT:]
    if not text or not any(mark in text for mark in _PUBLIC_ASCII_PUNCTUATION):
        return original

    stripped = text.strip()
    if stripped and not suffix:
        try:
            json.loads(stripped)
        except (TypeError, ValueError, RecursionError):
            pass
        else:
            return original

    protected = bytearray(len(text))

    def protect(start: int, end: int) -> None:
        protected[start:end] = b"\x01" * max(0, end - start)

    for match in _PUBLIC_LITERAL_PATTERN.finditer(text):
        protect(match.start(), match.end())

    offset = 0
    for line in text.splitlines(keepends=True):
        line_body = line.rstrip("\r\n")
        if _PUBLIC_CODE_LINE_PATTERN.search(line_body):
            protect(offset, offset + len(line_body))
        offset += len(line)

    decoder = json.JSONDecoder()
    json_candidates = 0
    for match in re.finditer(r"[\[{]", text):
        start = match.start()
        if protected[start]:
            continue
        if start and text[start - 1] in "[{":
            continue
        json_candidates += 1
        if json_candidates > _PUBLIC_JSON_CANDIDATE_LIMIT:
            break
        try:
            decoded, length = decoder.raw_decode(text[start:])
        except RecursionError:
            break
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, (dict, list)):
            protect(start, start + length)

    def context_left(index: int) -> str | None:
        cursor = index - 1
        while cursor >= 0 and text[cursor] in _PUBLIC_LEFT_CONTEXT_SKIP:
            cursor -= 1
        return text[cursor] if cursor >= 0 else None

    def context_right(index: int, *, sentence_end: bool = False) -> str | None:
        skip = _PUBLIC_SENTENCE_TRAIL_SKIP if sentence_end else _PUBLIC_RIGHT_CONTEXT_SKIP
        cursor = index
        while cursor < len(text) and text[cursor] in skip:
            cursor += 1
        return text[cursor] if cursor < len(text) else None

    normalized = list(text)
    index = 0
    while index < len(text):
        mark = text[index]
        if protected[index] or mark not in _PUBLIC_ASCII_PUNCTUATION:
            index += 1
            continue
        if mark in "?!":
            run_end = index
            while (
                run_end < len(text)
                and not protected[run_end]
                and text[run_end] in "?!"
            ):
                run_end += 1
            left = context_left(index)
            right = context_right(run_end, sentence_end=True)
            if _is_han_character(left) and (
                right is None
                or _is_han_character(right)
                or _is_emoji_symbol(right)
                or right in ",;:，。！？；：…"
            ):
                for cursor in range(index, run_end):
                    normalized[cursor] = _PUBLIC_ASCII_PUNCTUATION[text[cursor]]
            index = run_end
            continue
        if _is_han_character(context_left(index)) and _is_han_character(
            context_right(index + 1)
        ):
            normalized[index] = _PUBLIC_ASCII_PUNCTUATION[mark]
        index += 1
    return "".join(normalized) + suffix


def _strip_sticker_filenames(value: str) -> str:
    """Prevent a provider from leaking local media filenames into dialogue."""

    return _STICKER_FILENAME_PATTERN.sub("", str(value or "")).strip()


def _current_hong_kong_day() -> str:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()


_STICKER_EXPLICIT_TERMS = (
    "表情包", "发个表情", "发送表情", "来个表情", "用表情", "贴图", "发张图",
)
_STICKER_STRONG_TERMS = (
    "太棒", "恭喜", "庆祝", "成功", "谢谢你", "好感动", "哈哈哈", "笑死", "惊喜", "太好了",
)
_STICKER_PLAYFUL_TERMS = (
    "哈哈", "开玩笑", "调侃", "可爱", "撒娇", "呜呜", "哭", "生气", "无语", "尴尬", "吐槽",
)

_CONTROLLED_JOINT_LOCATIONS: dict[str, dict[str, Any]] = {
    "commercial_street": {
        "location": "商业街",
        "explicit_aliases": ("商业街",),
        "aliases": ("商业街", "逛街"),
        "activities": {"shopping_together": "和分析员一起逛街"},
    },
    "shopping_mall": {
        "location": "购物中心",
        "explicit_aliases": ("商场", "购物中心", "购物商场"),
        "aliases": ("商场", "购物中心", "购物商场"),
        "activities": {"shopping_together": "和分析员一起购物"},
    },
    "park": {
        "location": "公园",
        "explicit_aliases": ("公园",),
        "aliases": ("公园", "散步"),
        "activities": {"strolling_together": "和分析员一起散步"},
    },
    "base_restaurant": {
        "location": "基地餐厅",
        "explicit_aliases": ("基地餐厅", "餐厅"),
        "aliases": ("基地餐厅", "餐厅"),
        "activities": {"eating_together": "和分析员一起用餐"},
    },
    "base_canteen": {
        "location": "基地食堂",
        # Kept only so an older signed state and “去找正在食堂的队员” can be
        # resolved. It is deliberately absent from the invitation catalog and
        # cannot be selected by a new direct movement request.
        "public_invitation": False,
        "explicit_aliases": ("基地食堂", "食堂"),
        "aliases": ("基地食堂", "食堂"),
        "activities": {"eating_together": "和分析员一起用餐"},
    },
    "base_lounge": {
        "location": "基地休息区",
        "explicit_aliases": ("基地休息区", "休息区", "基地公共区", "公共区"),
        "aliases": ("基地休息区", "休息区", "基地公共区", "公共区", "茶话会", "聊天"),
        "activities": {"relaxing_together": "和分析员一起休息聊天"},
    },
    "observation": {
        "location": "观景区",
        "explicit_aliases": ("观景区",),
        "aliases": ("观景区", "看风景", "观景"),
        "activities": {"viewing_together": "和分析员一起看风景"},
    },
    "training": {
        "location": "训练区",
        "explicit_aliases": ("训练区", "训练场", "训练室"),
        "aliases": ("训练区", "训练场", "训练室", "训练"),
        "activities": {"training_together": "和分析员一起训练"},
    },
    "archive": {
        "location": "资料室",
        "explicit_aliases": ("资料室", "资料阅览区", "阅览区"),
        "aliases": ("资料室", "资料阅览区", "阅览区", "查资料", "看资料"),
        "activities": {"reading_together": "和分析员一起查阅资料"},
    },
    "base_beach": {
        "location": "基地海滩",
        "explicit_aliases": ("基地海滩", "海滩"),
        "aliases": ("基地海滩", "海滩", "海边"),
        "activities": {"walking_by_sea_together": "和分析员一起在海滩散步"},
    },
    "base_arcade": {
        "location": "基地游戏厅",
        "explicit_aliases": ("基地游戏厅", "游戏厅"),
        "aliases": ("基地游戏厅", "游戏厅", "街机厅", "打游戏"),
        "activities": {"playing_games_together": "和分析员一起玩游戏"},
    },
    "base_hot_spring": {
        "location": "基地温泉",
        "explicit_aliases": ("基地温泉", "温泉"),
        "aliases": ("基地温泉", "温泉", "泡温泉"),
        "activities": {"relaxing_at_hot_spring": "和分析员一起在温泉放松"},
    },
    "base_healing_center": {
        "location": "基地疗愈中心",
        "explicit_aliases": ("基地疗愈中心", "疗愈中心"),
        "aliases": ("基地疗愈中心", "疗愈中心", "治疗中心"),
        "activities": {"resting_at_healing_center": "和分析员一起在疗愈中心休整"},
    },
    "base_bar": {
        "location": "基地酒吧",
        "explicit_aliases": ("基地酒吧", "酒吧"),
        "aliases": ("基地酒吧", "酒吧", "去喝一杯", "坐坐"),
        # Do not imply alcohol consumption; the destination is a base social
        # space and the activity remains suitable for every character.
        "activities": {"chatting_at_bar": "和分析员一起在酒吧坐坐聊天"},
    },
    "character_room": {
        # The public catalog deliberately uses a relationship-neutral label;
        # the signed state resolves it to the selected character at runtime.
        "location": "她的房间",
        "explicit_aliases": ("她的房间", "你的房间", "角色房间"),
        "aliases": ("她的房间", "你的房间", "角色房间"),
        "activities": {"spending_time_in_room": "和分析员在房间里相处"},
        "invitation_text": "要不要一起去你的房间？",
        "dynamic_location": "character_room",
    },
    "analyst_room": {
        "location": "我的房间",
        "explicit_aliases": ("我的房间", "分析员的房间"),
        "aliases": ("我的房间", "分析员的房间"),
        "activities": {"spending_time_in_room": "和分析员在房间里相处"},
        "invitation_text": "要不要一起去我的房间？",
        "dynamic_location": "analyst_room",
    },
}
_JOINT_MOVE_REQUEST_TERMS = (
    "一起去", "一起出发", "和我去", "陪我去", "跟我去", "带你去", "带你逛", "带你散步",
    "我们去", "一起逛", "一起散步", "陪我逛", "跟我逛", "和我逛", "陪我散步", "跟我散步",
    "带角色", "带她去", "带她逛", "带她散步", "和角色去", "陪角色去", "跟角色去",
    "和她去", "陪她去", "跟她去", "和她逛", "陪她逛", "跟她逛",
)
_JOINT_MOVE_TARGET_TERMS = ("一起去找", "和我去找", "陪我去找", "跟我去找", "去找", "去见", "去看看")
_JOINT_MOVE_CONTINUATION_TERMS = (
    "那就走吧", "我们走吧", "一起走吧", "走吧", "出发吧", "现在出发", "这就出发", "现在走",
)
_JOINT_MOVE_ACCEPT_TERMS = (
    "可以", "当然", "愿意", "没问题", "走吧", "出发", "陪你", "跟你去", "跟你逛", "和你去",
    "和你逛", "一起去", "我们去", "那就走", "现在走", "这就出发", "马上出发", "现在出发",
    "好呀", "好啊", "好吧", "行啊", "行吧", "正合我意", "乐意奉陪", "就这么定",
    "这就过去", "立即过去", "马上过去", "动身", "这就走", "现在就走",
)
_JOINT_MOVE_NEGATIVE_TERMS = (
    "不去", "不想去", "不想", "不愿意", "不能", "不可以", "不行", "不好", "先不", "还是不", "算了",
    "没办法", "无法", "做不到", "不合适",
    "没空", "不方便", "拒绝", "下次", "以后", "改天",
    "稍后", "晚点", "等会", "待会", "一会儿", "过一会", "晚些时候", "你可以先去", "你先去",
    "我先去", "先去吧", "等我", "明天", "到时候", "之后再",
    "有机会", "如果", "假如", "理论上", "也许", "或许",
)
_JOINT_MOVE_HISTORICAL_TERMS = (
    "去过", "逛过", "曾经", "已经去", "上次去", "以前去",
)
_JOINT_MOVE_ANSWER_QUESTION_TERMS = (
    "要不要", "想不想", "愿不愿意", "能不能", "可不可以", "好不好", "可以吗", "好吗", "有空吗", "去吗",
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    compact = str(text or "").casefold()
    return any(term.casefold() in compact for term in terms)


def _joint_move_has_blocker(text: str) -> bool:
    # Chinese invitation forms can contain a literal negative substring
    # (“想不想” contains “不想”, “能不能” contains “不能”) without being a
    # refusal. Remove only those reviewed question forms before checking true
    # negative, future, conditional, and historical blockers.
    value = str(text or "")
    for invitation in ("要不要", "想不想", "愿不愿意", "能不能", "可不可以", "好不好"):
        value = value.replace(invitation, "")
    return _contains_any(value, _JOINT_MOVE_NEGATIVE_TERMS + _JOINT_MOVE_HISTORICAL_TERMS)


class PublicChatService:
    def __init__(
        self,
        settings: Settings,
        public_settings: PublicSettings,
        *,
        provider_client: ProviderHTTPPool | None = None,
    ):
        self.public_settings = public_settings
        self.provider_client = provider_client
        self.repository = PublicRuntimeRepository(settings, public_settings)
        # Pass the inert store at construction time: the public process never
        # opens, reads, or writes the internal SQLite conversation database.
        ephemeral_database = (
            Path(tempfile.gettempdir())
            / "project-snow-public"
            / str(os.getpid())
            / "conversations.sqlite3"
        )
        self.mvp = MVPService(
            settings,
            self.repository,
            force_chat_enabled=True,
            conversation_database_path=ephemeral_database,
            conversation_store=StatelessConversationStore(),
        )
        self.gate = GenerationGate()
        self._state_cleanup_lock = threading.RLock()
        self.media = PublicMediaCatalog(
            public_settings.media_root,
            public_settings.media_version,
            (character.character_id for character in MVP_CHARACTERS),
        )
        self.stickers = PublicStickerCatalog(
            public_settings.sticker_root,
            public_settings.sticker_version,
        )

    def close(self) -> None:
        self.mvp.close()
        self.repository.close()

    def ensure_character_available(self, character_id: str) -> None:
        if character_id not in self.mvp._views():
            raise CharacterUnavailable(character_id)

    def validate_content_blocks(self, blocks: list[Any], channel: str) -> list[dict[str, Any]]:
        """Resolve sticker ids and return canonical, manifest-backed blocks."""

        canonical: list[dict[str, Any]] = []
        sticker_count = 0
        sticker_seen = False
        for block in blocks:
            item = block.model_dump() if hasattr(block, "model_dump") else dict(block)
            block_type = str(item.get("type") or "")
            if block_type == "sticker":
                if channel != "text":
                    raise ValueError("sticker blocks are only valid in text communication")
                sticker_count += 1
                if sticker_count > 1:
                    raise ValueError("a message may contain at most one sticker")
                sticker_seen = True
                resolved = self.stickers.resolve(str(item.get("asset_id") or ""))
                if not resolved:
                    raise ValueError("sticker asset is unavailable")
                canonical.append({"type": "sticker", **resolved})
                continue
            if sticker_seen:
                raise ValueError("sticker must follow the message text")
            canonical.append({
                "type": block_type,
                "text": str(item.get("text") or "").strip(),
            })
        return canonical

    def sticker_candidates(
        self,
        *,
        request_id: str,
        character_id: str,
        message: str = "",
        recent_history: list[HistoryTurn] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return a deterministic, role-safe candidate set for this turn."""
        history = recent_history or []
        diagnostics = diagnostics if diagnostics is not None else {}
        recent_asset_ids: list[str] = []
        history_fingerprint: list[str] = []
        for turn in history[-8:]:
            role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
            content = turn.get("content") if isinstance(turn, dict) else getattr(turn, "content", None)
            blocks = turn.get("content_blocks") if isinstance(turn, dict) else getattr(turn, "content_blocks", None)
            block_fingerprint: list[str] = []
            for block in blocks or []:
                block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                asset_id = block.get("asset_id") if isinstance(block, dict) else getattr(block, "asset_id", None)
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                if role == "assistant" and block_type == "sticker" and asset_id:
                    recent_asset_ids.append(str(asset_id))
                block_fingerprint.append(f"{block_type}:{asset_id or text or ''}")
            history_fingerprint.append(
                f"{role}:{str(content or '')[:240]}:{'|'.join(block_fingerprint)}"
            )
        assistant_sticker_turns = 0
        for turn in reversed(history):
            role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
            if role != "assistant":
                continue
            blocks = turn.get("content_blocks") if isinstance(turn, dict) else getattr(turn, "content_blocks", None)
            blocks = blocks or []
            if any(
                (block.get("type") if isinstance(block, dict) else getattr(block, "type", None)) == "sticker"
                for block in blocks
            ):
                assistant_sticker_turns += 1
            else:
                break
            if assistant_sticker_turns >= 2:
                break
        explicit = _contains_any(message, _STICKER_EXPLICIT_TERMS)
        if assistant_sticker_turns >= 2 and not explicit:
            diagnostics.update({
                "sticker_gate": "closed",
                "sticker_rejected_reason": "cooldown",
                "sticker_consecutive_turns": assistant_sticker_turns,
            })
            return []

        history_key = "\x1e".join(history_fingerprint)
        digest = sha256(
            (
                f"sticker\x1f{request_id}\x1f{character_id}\x1f{_current_hong_kong_day()}"
                f"\x1f{history_key}"
            ).encode()
        ).digest()
        if _contains_any(message, _STICKER_STRONG_TERMS):
            probability, tier, emotion = 0.40, "strong", {"strong", "celebration"}
        elif _contains_any(message, _STICKER_PLAYFUL_TERMS):
            probability, tier, emotion = 0.25, "emotional", {"playful", "emotion"}
        else:
            # An explicit request is a gate, not an emotion.  Keep the
            # neutral label only for the ordinary 8% branch; otherwise a
            # generic "发个表情" request would reject every expressive asset.
            probability, tier, emotion = 0.08, "ordinary", set()
        roll = digest[0] / 256
        diagnostics.update({
            "sticker_probability_tier": tier,
            "sticker_probability": probability,
            "sticker_roll": round(roll, 6),
        })
        if not explicit and roll >= probability:
            diagnostics.update({
                "sticker_gate": "closed",
                "sticker_rejected_reason": "probability_miss",
            })
            return []
        catalog_page = self.stickers.list(limit=500)
        if catalog_page.get("status") != "ok":
            diagnostics.update({
                "sticker_gate": "closed",
                "sticker_rejected_reason": "catalog_unavailable",
            })
            return []
        catalog = catalog_page.get("stickers") or []
        if not catalog:
            diagnostics.update({
                "sticker_gate": "closed",
                "sticker_rejected_reason": "catalog_unavailable",
            })
            return []
        role_specific = []
        generic = []
        for item in catalog:
            owners = item.get("character_ids") or []
            if owners and character_id not in owners and "generic" not in owners:
                continue
            tags = {str(value).casefold() for value in item.get("emotion_tags") or []}
            if emotion and tags and not tags.intersection(emotion):
                continue
            if owners and character_id in owners:
                role_specific.append(item)
            elif not owners or "generic" in owners:
                generic.append(item)
        # Prefer the selected character's own package. Generic assets are a
        # deliberate fallback and assets owned by another character never
        # enter the candidate set.
        compatible = role_specific or generic
        if not compatible:
            diagnostics.update({
                "sticker_gate": "closed",
                "sticker_rejected_reason": "no_matching_candidate",
            })
            return []
        recent_asset_set = set(recent_asset_ids[-8:])
        fresh = [
            item
            for item in compatible
            if str(item.get("asset_id") or "") not in recent_asset_set
        ]
        if fresh:
            compatible = fresh
        start = int.from_bytes(digest[1:3], "big") % len(compatible)
        ordered = (compatible[start:] + compatible[:start])[:8]
        candidates = [
            {
                "asset_id": str(item.get("asset_id") or ""),
                "caption": str(item.get("caption") or "")[:120],
                "tags": [str(item.get("section") or "未分类"), *[str(value) for value in item.get("emotion_tags") or []]],
                "character_ids": [str(value) for value in item.get("character_ids") or []],
                "candidate_scope": str(item.get("candidate_scope") or "generic"),
            }
            for item in ordered
            if item.get("asset_id")
        ]
        diagnostics.update({
            "sticker_gate": "eligible",
            "sticker_candidate_count": len(candidates),
            "sticker_explicit_request": explicit,
        })
        diagnostics.pop("sticker_rejected_reason", None)
        return candidates

    def characters(self) -> list[dict[str, Any]]:
        result = []
        for character in MVP_CHARACTERS:
            # Never synthesize a URL for an unverified package.  The client
            # can render its neutral placeholder while readiness blocks the
            # candidate from promotion.
            avatar = self.media.avatar(character.character_id)
            result.append(
                {
                    "character_id": character.character_id,
                    "display_name": character.display_name,
                    "aliases": list(character.aliases),
                    "search_tokens": list(character.search_tokens),
                    # The package is mounted separately from the GPL image and
                    # is verified before any URL is advertised to a client.
                    "avatar": avatar,
                }
            )
        return result

    def analyst_avatar(self) -> dict[str, Any] | None:
        """Return the separately packaged default analyst portrait, if verified."""

        return self.media.analyst_avatar()

    @staticmethod
    def movement_catalog() -> list[dict[str, str]]:
        """Expose only the reviewed invitation choices, never prompt internals."""

        values: list[dict[str, str]] = []
        for location_id, definition in _CONTROLLED_JOINT_LOCATIONS.items():
            if definition.get("public_invitation") is False:
                continue
            activities = dict(definition.get("activities") or {})
            activity_id = next(iter(activities), "")
            values.append(
                {
                    "location_id": location_id,
                    "display_name": str(definition.get("location") or location_id),
                    "activity_id": activity_id,
                    "activity_name": str(activities.get(activity_id) or ""),
                    "invitation_text": str(
                        definition.get("invitation_text")
                        or f"要不要一起去{definition.get('location') or location_id}？"
                    ),
                }
            )
        return values

    @staticmethod
    def _character_name(character_id: str) -> str:
        return next(
            (
                character.display_name
                for character in MVP_CHARACTERS
                if character.character_id == character_id
            ),
            "她",
        )

    @classmethod
    def _resolved_location_definition(
        cls,
        location_id: str,
        definition: dict[str, Any],
        character_id: str,
    ) -> dict[str, Any]:
        resolved = {**definition, "activities": dict(definition.get("activities") or {})}
        dynamic_location = str(definition.get("dynamic_location") or "")
        if dynamic_location == "character_room":
            character_name = cls._character_name(character_id)
            resolved["location"] = f"{character_name}的房间"
            resolved["waiting_activity"] = f"在自己的房间等分析员"
        elif dynamic_location == "analyst_room":
            resolved["location"] = "分析员的房间"
            resolved["waiting_activity"] = "在分析员的房间等分析员"
        else:
            resolved["waiting_activity"] = (
                f"在{resolved.get('location') or location_id}等分析员"
            )
        return resolved

    @classmethod
    def validate_movement_request(cls, request: ChatRequest) -> None:
        """Reject a forged structured invitation before any provider work."""

        selected = str(request.movement_location_id or "").strip()
        if not selected:
            return
        definition = _CONTROLLED_JOINT_LOCATIONS.get(selected)
        if not definition or definition.get("public_invitation") is False:
            raise ValueError("movement location is not publicly selectable")
        expected_message = str(
            definition.get("invitation_text")
            or f"要不要一起去{definition.get('location') or selected}？"
        ).strip()
        if request.message.strip() != expected_message:
            raise ValueError("movement invitation text does not match the catalog")
        expected_block_type = (
            "message" if request.communication_channel == "text" else "speech"
        )
        if (
            len(request.content_blocks) != 1
            or request.content_blocks[0].type != expected_block_type
            or request.content_blocks[0].text.strip() != expected_message
        ):
            raise ValueError("movement invitation must contain only the catalog text")
        resolved = cls._direct_joint_move_intent(
            request.message,
            None,
            request.character_id,
        )
        if not resolved or resolved[0] != selected:
            raise ValueError("movement location does not match the invitation")

    @classmethod
    def _movement_intent_for_request(
        cls,
        request: ChatRequest,
        state: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]] | None:
        intent = cls._joint_move_intent(
            request.message,
            request.recent_history,
            state,
            request.character_id,
        )
        selected = str(request.movement_location_id or "").strip()
        if not selected:
            return intent
        if not intent or intent[0] != selected:
            return None
        return selected, cls._resolved_location_definition(
            selected,
            intent[1],
            request.character_id,
        )

    @staticmethod
    def _history_text(turn: HistoryTurn | dict[str, Any]) -> str:
        if isinstance(turn, dict):
            return str(turn.get("content") or "")
        return str(getattr(turn, "content", "") or "")

    @staticmethod
    def _controlled_location_for_scene(location: str) -> tuple[str, dict[str, Any]] | None:
        value = str(location or "").strip()
        if not value:
            return None
        for location_id, definition in _CONTROLLED_JOINT_LOCATIONS.items():
            aliases = (
                str(definition.get("location") or ""),
                *tuple(definition.get("explicit_aliases") or ()),
            )
            if any(alias and (alias in value or value in alias) for alias in aliases):
                return location_id, definition
        return None

    @classmethod
    def _target_presence_move_intent(
        cls,
        message: str,
        state: dict[str, Any] | None,
        selected_character_id: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        value = str(message or "").strip()
        if not state or not _contains_any(value, _JOINT_MOVE_TARGET_TERMS):
            return None
        presence = state.get("presence") or {}
        for character in sorted(MVP_CHARACTERS, key=lambda item: len(item.display_name), reverse=True):
            if character.character_id == selected_character_id:
                continue
            names = tuple(dict.fromkeys((character.display_name, character.source_name, *character.aliases)))
            if not _contains_any(value, names):
                continue
            raw_scene = presence.get(character.character_id) or {}
            scene = raw_scene.model_dump() if hasattr(raw_scene, "model_dump") else dict(raw_scene)
            resolved = cls._controlled_location_for_scene(str(scene.get("location") or ""))
            if not resolved:
                return None
            location_id, definition = resolved
            target_definition = {
                **definition,
                "activities": {
                    **dict(definition.get("activities") or {}),
                    "meeting_companion": f"和分析员一起去找{character.display_name}",
                },
                "resolved_activity_id": "meeting_companion",
                "target_character_id": character.character_id,
                "target_character_name": character.display_name,
                "resolution": "target_presence",
            }
            return location_id, target_definition
        return None

    @classmethod
    def _direct_joint_move_intent(
        cls,
        message: str,
        state: dict[str, Any] | None = None,
        selected_character_id: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        value = str(message or "").strip()
        if _joint_move_has_blocker(value):
            return None
        target_intent = cls._target_presence_move_intent(value, state, selected_character_id)
        if target_intent:
            return target_intent
        if not _contains_any(value, _JOINT_MOVE_REQUEST_TERMS):
            return None
        # Prefer a named destination over a generic activity. "去商场逛街"
        # therefore resolves to the mall rather than the generic 逛街 alias.
        for location_id, definition in _CONTROLLED_JOINT_LOCATIONS.items():
            if definition.get("public_invitation") is False:
                continue
            if _contains_any(value, tuple(definition.get("explicit_aliases") or ())):
                return location_id, {**definition, "resolution": "current_explicit"}
        for location_id, definition in _CONTROLLED_JOINT_LOCATIONS.items():
            if definition.get("public_invitation") is False:
                continue
            if _contains_any(value, tuple(definition["aliases"])):
                return location_id, {**definition, "resolution": "current_activity"}
        return None

    @classmethod
    def _joint_move_intent(
        cls,
        message: str,
        recent_history: list[HistoryTurn] | None = None,
        state: dict[str, Any] | None = None,
        selected_character_id: str | None = None,
    ) -> tuple[str, dict[str, Any]] | None:
        direct = cls._direct_joint_move_intent(message, state, selected_character_id)
        if direct:
            return direct
        value = str(message or "").strip()
        if not _contains_any(value, _JOINT_MOVE_CONTINUATION_TERMS):
            return None
        if _joint_move_has_blocker(value):
            return None
        # A short continuation such as “那就走吧” inherits only the latest
        # explicit, current invitation from the bounded browser history.
        for turn in reversed((recent_history or [])[-8:]):
            inherited = cls._direct_joint_move_intent(
                cls._history_text(turn),
                state,
                selected_character_id,
            )
            if inherited:
                location_id, definition = inherited
                return location_id, {**definition, "resolution": "history_continuation"}
        return None

    @staticmethod
    def _joint_move_is_accepted(answer: str, *, rendezvous: bool = False) -> bool:
        """Conservatively recognize an immediate, affirmative reply.

        A bare substring such as ``好`` is intentionally insufficient: it
        would match greetings like ``你好`` and produce a false location
        mutation. Future, conditional, historical, or negative language wins
        over an otherwise positive phrase.
        """

        value = str(answer or "").strip()
        blocker_value = value
        if rendezvous:
            # "我先去那里等你" is the required text-channel hand-off, not a
            # future-plan refusal.  Remove only that bounded construction;
            # all other negative, conditional and delayed language still wins.
            blocker_value = re.sub(
                r"我先(?:去|过去|到).{0,32}(?:等你|等分析员)",
                "现在动身",
                blocker_value,
            )
        if not value or _joint_move_has_blocker(blocker_value):
            return False
        if _contains_any(value, _JOINT_MOVE_ANSWER_QUESTION_TERMS) and not _contains_any(
            value,
            ("当然", "愿意", "没问题", "走吧", "出发", "现在走", "好呀", "好啊", "好吧", "行啊", "行吧"),
        ):
            return False
        bare_acceptance = bool(
            re.search(
                r"(?:^|[，。！？!?；;\s])(?:好|行)(?:呀|啊|的|吧|啦)?(?:[，。！？!?；;\s]|$)",
                value,
            )
        )
        waiting_acceptance = rendezvous and bool(
            re.search(
                r"(?:我先(?:去|过去|到).{0,32}(?:等你|等分析员)|"
                r"(?:先到|先过去).{0,24}(?:等你|等分析员)|"
                r"(?:在那里|那边|到了).{0,16}等你)",
                value,
            )
        )
        return waiting_acceptance or bare_acceptance or _contains_any(
            value, _JOINT_MOVE_ACCEPT_TERMS
        )

    @staticmethod
    def _text_rendezvous_is_explicit(answer: str) -> bool:
        return bool(
            re.search(
                r"(?:我先(?:去|过去|到).{0,32}(?:等你|等分析员)|"
                r"(?:先到|先过去).{0,24}(?:等你|等分析员)|"
                r"(?:在那里|那边|到了).{0,16}等你)",
                str(answer or ""),
            )
        )

    def _apply_joint_movement(
        self,
        state: dict[str, Any],
        request: ChatRequest,
        result: dict[str, Any],
    ) -> tuple[dict[str, Any], StateEvent | None, dict[str, Any]]:
        diagnostics: dict[str, Any] = {"state_update_status": "not_requested"}
        intent = self._movement_intent_for_request(request, state)
        answer = str(result.get("answer") or "")
        accepted = self._joint_move_is_accepted(
            answer,
            rendezvous=request.communication_channel == "text",
        )
        raw_updates = result.get("state_updates") or []
        proposal: StateUpdateProposal | None = None
        if raw_updates:
            try:
                proposal = StateUpdateProposal.model_validate(raw_updates[0])
                diagnostics["model_proposal_status"] = "valid"
            except (TypeError, ValueError):
                diagnostics["model_proposal_status"] = "invalid"
        else:
            diagnostics["model_proposal_status"] = "absent"
        if not intent:
            diagnostics["state_update_rejected_reason"] = "movement_intent_unresolved"
            return state, None, diagnostics
        if not accepted:
            diagnostics["state_update_rejected_reason"] = "character_did_not_accept_now"
            return state, None, diagnostics
        if (
            request.communication_channel == "text"
            and not self._text_rendezvous_is_explicit(answer)
        ):
            diagnostics["state_update_rejected_reason"] = "rendezvous_semantics_unresolved"
            return state, None, diagnostics

        # The deterministic server resolver is authoritative. A model
        # proposal can confirm it, but absence, malformed data, or a mismatch
        # never suppresses an otherwise valid natural-language movement.
        location_id, raw_definition = intent
        definition = self._resolved_location_definition(
            location_id,
            raw_definition,
            request.character_id,
        )
        activity_id = str(
            definition.get("resolved_activity_id")
            or next(iter(definition.get("activities") or {}), "")
        )
        if not activity_id or activity_id not in definition.get("activities", {}):
            diagnostics["state_update_rejected_reason"] = "controlled_activity_unavailable"
            return state, None, diagnostics
        if proposal and (
            proposal.location_id != location_id or proposal.activity_id != activity_id
        ):
            diagnostics["model_proposal_status"] = "mismatch_ignored"

        location = str(definition["location"])
        activity = str(definition["activities"][activity_id])
        target_character_id = str(definition.get("target_character_id") or "") or None
        event_id = (
            f"{request.request_id}:rendezvous_waiting"
            if request.communication_channel == "text"
            else f"{request.request_id}:joint_move"
        )
        pending = dict(state.get("pending_rendezvous") or {})
        existing_pending = dict(pending.get(request.character_id) or {})
        if (
            request.communication_channel == "text"
            and str(existing_pending.get("rendezvous_id") or "") == event_id
        ):
            diagnostics.update({
                "state_update_status": "character_waiting",
                "state_update_type": "rendezvous_waiting",
                "location_id": location_id,
                "location_name": location,
                "display_name": location,
                "activity_id": activity_id,
                "pending_rendezvous": existing_pending,
            })
            existing_event = next(
                (
                    item
                    for item in state.get("recent_events") or []
                    if str(item.get("event_id") or "") == event_id
                ),
                None,
            )
            event = (
                StateEvent.model_validate(existing_event)
                if existing_event
                else StateEvent(
                    event_id=event_id,
                    event_type="rendezvous_waiting",
                    character_id=request.character_id,
                    communication_channel="text",
                    location=location,
                    location_id=location_id,
                    activity_id=activity_id,
                    target_character_id=target_character_id,
                )
            )
            return state, event, diagnostics
        existing = next(
            (
                item
                for item in state.get("recent_events") or []
                if str(item.get("event_id") or "") == event_id
            ),
            None,
        )
        if existing:
            try:
                event = StateEvent.model_validate(existing)
            except (TypeError, ValueError):
                diagnostics["state_update_rejected_reason"] = "existing_event_invalid"
                return state, None, diagnostics
            diagnostics.update({
                "state_update_status": "already_applied",
                "state_update_type": (
                    "rendezvous_waiting"
                    if request.communication_channel == "text"
                    else "joint_move"
                ),
                "location_id": location_id,
                "location_name": location,
                "display_name": location,
                "activity_id": activity_id,
                "target_character_id": target_character_id,
            })
            return state, event, diagnostics
        presence = dict(state.get("presence") or {})
        character_scene = dict(presence.get(request.character_id) or {})
        joined_activity = activity
        waiting_activity = str(
            definition.get("waiting_activity") or f"在{location}等分析员"
        )
        character_scene.update({
            "location": location,
            "activity": (
                waiting_activity
                if request.communication_channel == "text"
                else joined_activity
            ),
            "state_scope": "conversation_confirmed",
        })
        presence[request.character_id] = character_scene
        if request.communication_channel == "text":
            pending_record = {
                "rendezvous_id": event_id,
                "character_id": request.character_id,
                "location_id": location_id,
                "location_name": location,
                "activity_id": activity_id,
                "waiting_activity": waiting_activity,
                "joined_activity": joined_activity,
                "created_at": datetime.now(ZoneInfo("Asia/Hong_Kong")).isoformat(),
                "schedule_date": str(state.get("schedule_date") or ""),
            }
            pending[request.character_id] = pending_record
        else:
            pending_record = None
            pending.pop(request.character_id, None)
        event = StateEvent(
            event_id=event_id,
            event_type=(
                "rendezvous_waiting"
                if request.communication_channel == "text"
                else "joint_movement"
            ),
            character_id=request.character_id,
            communication_channel=request.communication_channel,
            location=location,
            location_id=location_id,
            activity_id=activity_id,
            target_character_id=target_character_id,
        )
        next_state = self._state_with_event(
            {
                **state,
                "presence": presence,
                "pending_rendezvous": pending,
            },
            event=event,
            analyst_location=(
                state.get("analyst_location")
                if request.communication_channel == "text"
                else location
            ),
        )
        diagnostics.update({
            "state_update_status": (
                "character_waiting"
                if request.communication_channel == "text"
                else "applied"
            ),
            "state_update_type": (
                "rendezvous_waiting"
                if request.communication_channel == "text"
                else "joint_move"
            ),
            "location_id": location_id,
            "location_name": location,
            "display_name": location,
            "activity_id": activity_id,
            "target_character_id": target_character_id,
            "intent_resolution": str(definition.get("resolution") or "current_explicit"),
            **(
                {"pending_rendezvous": pending_record}
                if pending_record is not None
                else {}
            ),
        })
        return next_state, event, diagnostics

    def _default_state(self, subject_hash: str) -> dict[str, Any]:
        # Every anonymous subject gets a deterministic, independent schedule
        # for one Hong Kong calendar day.  Including the subject in the HMAC
        # seed prevents one visitor's scene from becoming a global schedule.
        hong_kong_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        schedule_start = hong_kong_now.replace(hour=0, minute=0, second=0, microsecond=0)
        schedule_date = schedule_start.date().isoformat()
        schedule_expires = schedule_start + timedelta(days=1)
        schedule_key = self.public_settings.state_hmac_key or b"project-snow-public-schedule-dev"
        daily_seed = hmac.new(
            schedule_key,
            f"{subject_hash}\x1f{schedule_date}".encode("utf-8"),
            sha256,
        ).hexdigest()
        world_id = "public_subject_schedule_" + daily_seed[:32]
        world = self.mvp._world_snapshot(world_id)
        presence = {
            character_id: {
                **dict(scene),
                "state_scope": "subject_daily",
            }
            for character_id, scene in (world.get("presence") or {}).items()
        }
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=1,
            analyst_location=world.get("analyst_location"),
            presence=presence,
            relationships={},
            pending_rendezvous={},
            recent_events=[],
            schedule_date=schedule_date,
            schedule_revision=1,
            generated_at=schedule_start.isoformat(),
            expires_at=schedule_expires.isoformat(),
            subject_binding=subject_hash,
            state_key_id=self.public_settings.state_key_id,
        ).model_dump()

    def _normalized_state(self, token: str, subject_hash: str) -> dict[str, Any]:
        raw = verify_state(self.public_settings, token)
        defaults = self._default_state(subject_hash)
        if not raw:
            return defaults

        schema_version = str(raw.get("schema_version") or "public-state-1")
        if schema_version not in {"public-state-1", "public-state-2"}:
            raise PublicSecurityError("Public state schema is not supported")
        incoming_binding = str(raw.get("subject_binding") or "")
        if incoming_binding and not hmac.compare_digest(incoming_binding, subject_hash):
            raise PublicSecurityError("Public state belongs to another anonymous session")
        incoming_schedule_date = str(raw.get("schedule_date") or "")
        incoming_schedule_revision = max(0, int(raw.get("schedule_revision") or 0))
        if schema_version == "public-state-1":
            old_world = raw.get("world") if isinstance(raw.get("world"), dict) else {}
            incoming_presence = old_world.get("presence") or {}
            analyst_location = old_world.get("analyst_location")
            recent_events: list[dict[str, Any]] = []
            incoming_pending: dict[str, Any] = {}
            # Legacy state packages are user-authored conversation snapshots;
            # preserve their known scene while upgrading the envelope.
            incoming_schedule_date = str(defaults.get("schedule_date") or "")
        else:
            incoming_presence = raw.get("presence") or {}
            analyst_location = raw.get("analyst_location")
            recent_events = list(raw.get("recent_events") or [])[-4:]
            incoming_pending = (
                dict(raw.get("pending_rendezvous") or {})
                if isinstance(raw.get("pending_rendezvous"), dict)
                else {}
            )

        # A stale browser package must not keep yesterday's scene alive.  The
        # first request after Hong Kong midnight lazily replaces all positions,
        # activities, recent events and rendezvous records while retaining
        # relationship memory.  Incrementing the schedule revision lets the
        # browser reject a late response from the previous day.
        schedule_changed = (
            incoming_schedule_date != str(defaults.get("schedule_date") or "")
        )
        shared_schedule_migrated = False
        if schedule_changed:
            incoming_presence = {}
            analyst_location = defaults.get("analyst_location")
            recent_events = []
            incoming_pending = {}
        elif isinstance(incoming_presence, dict):
            legacy_shared_present = any(
                isinstance(scene, dict) and scene.get("state_scope") == "shared_daily"
                for scene in incoming_presence.values()
            )
            shared_schedule_migrated = legacy_shared_present
            conversation_confirmed_present = any(
                isinstance(scene, dict)
                and scene.get("state_scope") == "conversation_confirmed"
                for scene in incoming_presence.values()
            )
            if legacy_shared_present and not conversation_confirmed_present:
                analyst_location = defaults.get("analyst_location")

        canonical = {character.character_id: character for character in MVP_CHARACTERS}
        presence: dict[str, dict[str, Any]] = {}
        for character_id, character in canonical.items():
            fallback = dict(defaults["presence"][character_id])
            candidate = (
                dict(incoming_presence.get(character_id) or {})
                if isinstance(incoming_presence, dict)
                else {}
            )
            # 0.9.1's shared_daily scene was identical for every visitor. On
            # same-day upgrade it must not be relabelled and retained as if it
            # had been subject-specific. Keep only explicitly confirmed
            # conversation scenes; replace shared entries with today's
            # subject-derived fallback.
            if candidate.get("state_scope") == "shared_daily":
                candidate = {}
            presence[character_id] = {
                "character_id": character_id,
                "character_name": character.display_name,
                "location": str(candidate.get("location") or fallback["location"])[:120],
                "activity": str(candidate.get("activity") or fallback["activity"])[:240],
                "state_scope": (
                    "conversation_confirmed"
                    if candidate.get("state_scope") == "conversation_confirmed"
                    else "subject_daily"
                    if candidate.get("state_scope") in {"subject_daily", "shared_daily"}
                    else fallback.get("state_scope", "subject_daily")
                ),
            }

        validated_events: list[dict[str, Any]] = []
        for event in recent_events:
            try:
                validated_events.append(StateEvent.model_validate(event).model_dump())
            except (TypeError, ValueError):
                continue
        validated_pending: dict[str, dict[str, Any]] = {}
        if not schedule_changed:
            for character_id, item in incoming_pending.items():
                if character_id not in canonical or not isinstance(item, dict):
                    continue
                try:
                    rendezvous = PendingRendezvous.model_validate(item)
                except (TypeError, ValueError):
                    continue
                if rendezvous.character_id != character_id:
                    continue
                validated_pending[character_id] = rendezvous.model_dump()
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=(
                max(0, int(raw.get("revision") or 0)) + 1
                if schedule_changed or shared_schedule_migrated
                else max(0, int(raw.get("revision") or 0))
            ),
            analyst_location=str(analyst_location)[:120] if analyst_location else None,
            presence=presence,
            relationships=dict(raw.get("relationships") or {}),
            pending_rendezvous=validated_pending,
            recent_events=validated_events[-4:],
            schedule_date=str(defaults.get("schedule_date") or ""),
            schedule_revision=(
                max(
                    int(defaults.get("schedule_revision") or 1),
                    incoming_schedule_revision + 1,
                )
                if schedule_changed or shared_schedule_migrated
                else max(1, incoming_schedule_revision)
            ),
            generated_at=str(defaults.get("generated_at") or ""),
            expires_at=str(defaults.get("expires_at") or ""),
            subject_binding=subject_hash,
            state_key_id=self.public_settings.state_key_id,
        ).model_dump()

    @staticmethod
    def _scene_state(state: dict[str, Any], character_id: str) -> dict[str, Any]:
        scene = dict((state.get("presence") or {}).get(character_id) or {})
        analyst_location = str(state.get("analyst_location") or "").strip() or None
        character_location = str(scene.get("location") or "").strip() or None
        return {
            "analyst_location": analyst_location,
            "character_location": character_location,
            "character_activity": scene.get("activity"),
            "visual_key": scene_visual_key(character_location),
            "co_located": bool(
                analyst_location
                and character_location
                and analyst_location == character_location
            ),
            "state_scope": str(scene.get("state_scope") or "session_simulation"),
        }

    def _state_with_event(
        self,
        state: dict[str, Any],
        *,
        event: StateEvent,
        analyst_location: str | None = None,
        world_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        presence = dict(state.get("presence") or {})
        if world_snapshot:
            for character_id, scene in (world_snapshot.get("presence") or {}).items():
                if character_id not in presence:
                    continue
                previous = dict(presence[character_id])
                previous.update(
                    {
                        key: value
                        for key, value in dict(scene).items()
                        if key in {"location", "activity", "state_scope"} and value
                    }
                )
                presence[character_id] = previous
            analyst_location = world_snapshot.get("analyst_location")
        events = [*(state.get("recent_events") or []), event.model_dump()][-4:]
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=int(state.get("revision") or 0) + 1,
            analyst_location=analyst_location,
            presence=presence,
            relationships=dict(state.get("relationships") or {}),
            pending_rendezvous=dict(state.get("pending_rendezvous") or {}),
            recent_events=events,
            schedule_date=str(state.get("schedule_date") or ""),
            schedule_revision=int(state.get("schedule_revision") or 1),
            generated_at=str(state.get("generated_at") or ""),
            expires_at=str(state.get("expires_at") or ""),
            subject_binding=str(state.get("subject_binding") or ""),
            state_key_id=self.public_settings.state_key_id,
        ).model_dump()

    @staticmethod
    def _pending_for_character(
        state: dict[str, Any],
        character_id: str,
    ) -> dict[str, Any] | None:
        item = (state.get("pending_rendezvous") or {}).get(character_id)
        return dict(item) if isinstance(item, dict) else None

    @classmethod
    def _join_pending_rendezvous(
        cls,
        state: dict[str, Any],
        character_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        pending = dict(state.get("pending_rendezvous") or {})
        item = pending.pop(character_id, None)
        if not isinstance(item, dict):
            return state, None
        presence = dict(state.get("presence") or {})
        scene = dict(presence.get(character_id) or {})
        if item.get("location_name"):
            scene["location"] = str(item["location_name"])
        if item.get("joined_activity"):
            scene["activity"] = str(item["joined_activity"])
        scene["state_scope"] = "conversation_confirmed"
        presence[character_id] = scene
        return {
            **state,
            "presence": presence,
            "pending_rendezvous": pending,
        }, dict(item)

    def resolve_presence(
        self,
        request: PresenceResolveRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        pending = self._pending_for_character(state, request.character_id)
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "scene_state": self._scene_state(state, request.character_id),
            "state_package": sign_state(self.public_settings, state),
            "schema_version": "public-state-2",
            "pending_rendezvous": pending,
        }

    def transition_presence(
        self,
        request: PresenceTransitionRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        before = self._scene_state(state, request.character_id)
        joined_rendezvous: dict[str, Any] | None = None
        next_location = state.get("analyst_location")
        if request.target_channel == "in_person":
            next_location = before.get("character_location")
            if not next_location:
                raise ValueError("character scene has no location")
            state, joined_rendezvous = self._join_pending_rendezvous(
                state,
                request.character_id,
            )
        event = StateEvent(
            event_id=str(request.request_id),
            event_type="presence_transition",
            character_id=request.character_id,
            communication_channel=request.target_channel,
            location=next_location,
        )
        next_state = self._state_with_event(
            state,
            event=event,
            analyst_location=next_location,
        )
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "communication_channel": request.target_channel,
            "scene_state": self._scene_state(next_state, request.character_id),
            "channel_transition": {
                "status": "applied_immediately",
                "from": "text" if request.target_channel == "in_person" else "in_person",
                "to": request.target_channel,
                "trigger": "presence_ui",
            },
            "presence_transition": {
                "status": (
                    "joined_character"
                    if request.target_channel == "in_person"
                    else "communicator_opened"
                ),
                "location": next_location,
            },
            "state_package": sign_state(self.public_settings, next_state),
            "pending_rendezvous": self._pending_for_character(
                next_state,
                request.character_id,
            ),
            "movement_status": (
                {
                    "status": "joined",
                    "location_id": str(joined_rendezvous.get("location_id") or ""),
                    "location_name": str(joined_rendezvous.get("location_name") or ""),
                    "display_name": str(joined_rendezvous.get("location_name") or ""),
                    "character_id": request.character_id,
                    "schedule_date": str(next_state.get("schedule_date") or ""),
                    "schedule_revision": int(next_state.get("schedule_revision") or 1),
                }
                if joined_rendezvous
                else {"status": "not_requested"}
            ),
            "model_called": False,
        }

    def prepare_presence_arrival(
        self,
        request: PresenceArrivalRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        scene = self._scene_state(state, request.character_id)
        location = scene.get("character_location")
        if not location:
            raise ValueError("character scene has no location")
        if self.public_settings.arrival_probability == 0.5:
            decision = "noticed" if secrets.randbelow(2) == 0 else "unnoticed"
        else:
            threshold = int(self.public_settings.arrival_probability * 10_000)
            decision = "noticed" if secrets.randbelow(10_000) < threshold else "unnoticed"
        state, joined_rendezvous = self._join_pending_rendezvous(
            state,
            request.character_id,
        )
        next_state = self._state_with_event(
            state,
            event=StateEvent(
                event_id=str(request.arrival_id),
                event_type="arrival",
                character_id=request.character_id,
                communication_channel="in_person",
                location=location,
                arrival_decision=decision,
            ),
            analyst_location=location,
        )
        return {
            "arrival_id": str(request.arrival_id),
            "character_id": request.character_id,
            "communication_channel": "in_person",
            "scene_state": self._scene_state(next_state, request.character_id),
            "decision": decision,
            "status": "completed",
            "reaction": None,
            "state_package": sign_state(self.public_settings, next_state),
            "pending_rendezvous": self._pending_for_character(
                next_state,
                request.character_id,
            ),
            "movement_status": (
                {
                    "status": "joined",
                    "location_id": str(joined_rendezvous.get("location_id") or ""),
                    "location_name": str(joined_rendezvous.get("location_name") or ""),
                    "display_name": str(joined_rendezvous.get("location_name") or ""),
                    "character_id": request.character_id,
                    "schedule_date": str(next_state.get("schedule_date") or ""),
                    "schedule_revision": int(next_state.get("schedule_revision") or 1),
                }
                if joined_rendezvous
                else {"status": "not_requested"}
            ),
            "model_called": False,
            "terminal_error": "",
            "state": next_state,
        }

    @staticmethod
    def failed_presence_arrival(
        prepared: dict[str, Any],
        code: str,
        *,
        model_called: bool,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                **prepared,
                "reaction": None,
                "model_called": model_called,
                "terminal_error": code,
                "diagnostics": diagnostics or {},
            }.items()
            if key != "state"
        }

    async def finish_presence_arrival(
        self,
        prepared: dict[str, Any],
        request: PresenceArrivalRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        async def generate():
            return await asyncio.to_thread(
                self._finish_presence_arrival_sync,
                prepared,
                request,
                subject_hash,
                provider,
                api_key,
            )

        return await self.gate.run(generate)

    def _finish_presence_arrival_sync(
        self,
        prepared: dict[str, Any],
        request: PresenceArrivalRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        self.repository.reset_request_health()
        self.mvp.reset_generation_diagnostics()
        synthetic = ChatRequest(
            request_id=request.arrival_id,
            provider=request.provider,
            credential=request.credential,
            model=request.model,
            character_id=request.character_id,
            message="（到场事件）分析员刚刚来到你身边。请主动问候，并自然承接当前角色最近的聊天内容。",
            communication_channel="in_person",
            recent_history=request.recent_history,
            history_summary=request.history_summary,
            state_package=prepared["state_package"],
        )
        with self._request_state(
            synthetic,
            subject_hash,
            state_override=dict(prepared["state"]),
        ) as (session_id, world_id, _state):
            try:
                result = self.mvp.chat(
                    request.character_id,
                    synthetic.message,
                    session_id=session_id,
                    world_session_id=world_id,
                    communication_channel="in_person",
                    analyst_content_blocks=[],
                    model_settings=(provider.base_url, api_key, request.model),
                    model_info={
                        "provider_id": provider.provider_id,
                        "model_name": request.model,
                        "reason": "public_presence_arrival",
                    },
                    thinking_decision=_public_immersive_thinking_decision(provider),
                    max_tokens_override=1600,
                    persist_exchange=False,
                    remember_session=False,
                    presence_arrival=True,
                )
            except Exception as exc:
                from .mvp_service import MVPProviderError

                if not isinstance(exc, MVPProviderError):
                    raise
                return self.failed_presence_arrival(
                    prepared,
                    "provider_request_failed",
                    model_called=True,
                    diagnostics=self._diagnostics(
                        total_started,
                        generation_class="rejected",
                        error_stage="provider",
                    ),
                )

        adjustments = list(result.get("response_adjustments") or [])
        if result.get("content_block_guard_rejected"):
            diagnostics = self._diagnostics(
                total_started,
                result,
                generation_class="rejected",
                guard_code="in_person_block_semantics",
                error_stage="generation_validation",
            )
            return self.failed_presence_arrival(
                prepared,
                "role_guard_rejected",
                model_called=True,
                diagnostics=diagnostics,
            )
        # A proactive arrival is not a user-authored chat turn. Do not invent
        # one from a local empty-output fallback; keep the already-applied
        # presence transition and let the character remain silent instead.
        if "empty_model_output_guard" in adjustments:
            return self.failed_presence_arrival(
                prepared,
                "upstream_invalid_response",
                model_called=True,
                diagnostics=self._diagnostics(
                    total_started,
                    result,
                    generation_class="rejected",
                    guard_code="empty_model_output_guard",
                    error_stage="generation_validation",
                ),
            )
        rejected_adjustments = sorted(
            set(adjustments).intersection(_PUBLIC_WHOLE_ANSWER_FALLBACKS)
        )
        if rejected_adjustments:
            code = (
                "upstream_invalid_response"
                if "empty_model_output_guard" in rejected_adjustments
                else "role_guard_rejected"
            )
            diagnostics = self._diagnostics(
                total_started,
                result,
                generation_class="rejected",
                guard_code=rejected_adjustments[0] if rejected_adjustments else "",
                error_stage="generation_validation",
            )
            diagnostics["rejected_adjustments"] = rejected_adjustments
            return self.failed_presence_arrival(
                prepared,
                code,
                model_called=True,
                diagnostics=diagnostics,
            )
        blocks, answer, truncated, safety_category = self._public_generation_content(
            result,
            "in_person",
            response_adjustments=adjustments,
        )
        if not any(block.get("type") == "speech" for block in blocks):
            return self.failed_presence_arrival(
                prepared,
                "role_guard_rejected",
                model_called=True,
                diagnostics=self._diagnostics(
                    total_started,
                    result,
                    generation_class="rejected",
                    guard_code="in_person_speech_missing",
                    error_stage="generation_validation",
                ),
            )
        reaction = {
            "message_id": "presence_message_" + sha256(
                f"{subject_hash}\x1f{request.arrival_id}".encode()
            ).hexdigest()[:16],
            "character_id": request.character_id,
            "communication_channel": "in_person",
            "stage_motion": _normalize_stage_motion(
                result.get("stage_motion"),
                "in_person",
            ),
            "answer": answer,
            "content_blocks": blocks,
            "source": "presence_arrival",
            "arrival_id": str(request.arrival_id),
            "usage": result.get("usage") or {},
            "truncated": truncated,
            "safety_category": safety_category,
        }
        return {
            key: value
            for key, value in {
                **prepared,
                "reaction": reaction,
                "model_called": True,
                "terminal_error": "",
                "diagnostics": self._diagnostics(
                    total_started,
                    result,
                    generation_class=(
                        "valid_rewrite"
                        if set(adjustments).intersection(_PUBLIC_REWRITE_ADJUSTMENTS)
                        else "normalized"
                        if set(adjustments).intersection(_PUBLIC_NORMALIZATION_ADJUSTMENTS)
                        else "valid_initial"
                    ),
                ),
            }.items()
            if key != "state"
        }

    @contextmanager
    def _request_state(
        self,
        request: ChatRequest,
        subject_hash: str,
        *,
        state_override: dict[str, Any] | None = None,
    ) -> Iterator[tuple[str, str, dict[str, Any]]]:
        state = state_override or self._normalized_state(request.state_package, subject_hash)
        session_id = "public_session_" + sha256(
            f"{subject_hash}\x1f{request.request_id}\x1f{request.character_id}".encode()
        ).hexdigest()[:20]
        world_id = "public_world_" + sha256(f"{subject_hash}\x1f{request.request_id}".encode()).hexdigest()[:20]
        # A new local day is an explicit product choice.  “start_today” keeps
        # stable relationship/world state but does not leak yesterday's
        # transcript or summary into the prompt.  “continue_previous” keeps
        # the bounded history supplied by the browser and is phrased as an
        # optional topic, never as an obligation.
        continuity_decision = str(request.continuity_decision or "").strip()
        turns = [] if continuity_decision == "start_today" else _history_turns(request.recent_history)
        history_summary = "" if continuity_decision == "start_today" else request.history_summary
        session_state = {
            "character_id": request.character_id,
            "mode": "immersive",
            "mode_turns": {"immersive": turns, "assistant": []},
            "turns": turns,
            "premises": (
                [{"kind": "browser_history_summary", "value": history_summary[:6000]}]
                if history_summary
                else []
            ),
            "continuity_decision": continuity_decision,
            "local_day_key": request.local_day_key,
            "continuity_rule": (
                "可选承接上次话题，不责备分析员未完成过去计划。"
                if continuity_decision == "continue_previous"
                else "从今天开始，不引用昨天未完成话题，不要求分析员兑现过去计划。"
                if continuity_decision == "start_today"
                else "仅使用本次请求提供的连续性内容，不把旧话题写成义务。"
            ),
            "style_context": None,
            "communication_channel": request.communication_channel,
            "recent_story_titles": [],
        }
        world_state = {
            "world_session_id": world_id,
            "created_at": "browser_state",
            "analyst_location": state.get("analyst_location"),
            "presence": dict(state.get("presence") or {}),
        }
        if not world_state["presence"]:
            world_state = self.mvp._world_snapshot(world_id)
        with _SESSION_LOCK:
            _SESSION_STATES[session_id] = session_state
        with _WORLD_STATE_LOCK:
            _WORLD_STATES[world_id] = world_state
        try:
            yield session_id, world_id, state
        finally:
            with self._state_cleanup_lock:
                with _SESSION_LOCK:
                    _SESSION_STATES.pop(session_id, None)
                with _WORLD_STATE_LOCK:
                    _WORLD_STATES.pop(world_id, None)

    async def chat(
        self,
        request: ChatRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
        *,
        gate_reserved: bool = False,
    ) -> dict[str, Any]:
        async def generate():
            return await asyncio.to_thread(self._chat_sync, request, subject_hash, provider, api_key)

        if gate_reserved:
            return await generate()
        return await self.gate.run(generate)

    def policy_rejection(
        self,
        request: ChatRequest,
        subject_hash: str,
        provider: ProviderSpec,
        safety_category: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        scene = self._scene_state(state, request.character_id)
        next_state = self._state_with_event(
            state,
            event=StateEvent(
                event_id=str(request.request_id),
                event_type="communication",
                character_id=request.character_id,
                communication_channel=request.communication_channel,
                location=scene.get("analyst_location"),
            ),
            analyst_location=state.get("analyst_location"),
        )
        answer = "抱歉，这个方向我不能继续提供具体指导。我们可以换成安全、合法且不伤害他人的话题。"
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "provider": provider.provider_id,
            "model": redact_sensitive_text(request.model, 200),
            "answer": answer,
            "communication_channel": request.communication_channel,
            "stage_motion": "none",
            "content_blocks": [
                {
                    "type": "message" if request.communication_channel == "text" else "speech",
                    "text": answer,
                }
            ],
            "truncated": False,
            "state_package": sign_state(self.public_settings, next_state),
            "degraded_services": [],
            "retrieval": {},
            "usage": {},
            "safety_category": safety_category,
            "generation_outcome": "valid_initial",
            "validation_disposition": "accepted",
            "movement_status": {"status": "not_requested"},
            "response_adjustments": [],
            "terminal_error": "",
            "diagnostics": {
                "timings_ms": {"total": 0, "provider_http_calls": 0},
                "dependency_health": {},
                "provider_latency_ms": 0,
                "retrieval_latency_ms": 0,
                "rewrite_latency_ms": 0,
                "model_calls": 0,
                "generation_class": "valid_initial",
                "guard_code": "",
                "guard_violation_count": 0,
            },
        }

    def _chat_sync(
        self,
        request: ChatRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        total_started = time.perf_counter()
        self.repository.reset_request_health()
        self.mvp.reset_generation_diagnostics()
        try:
            canonical_blocks = self.validate_content_blocks(
                request.content_blocks,
                request.communication_channel,
            )
        except ValueError:
            return self._terminal_result(
                request,
                provider,
                total_started,
                code="sticker_unavailable",
                error_stage="content_validation",
            )
        model_message = _model_input_from_blocks(canonical_blocks, request.message)
        sticker_diagnostics: dict[str, Any] = {
            "sticker_gate": "closed",
            "sticker_rejected_reason": "unsupported_channel",
        }
        sticker_candidates = (
            self.sticker_candidates(
                request_id=str(request.request_id),
                character_id=request.character_id,
                message=request.message,
                recent_history=request.recent_history,
                diagnostics=sticker_diagnostics,
            )
            if request.communication_channel == "text"
            else []
        )
        with self._request_state(request, subject_hash) as (session_id, world_id, prior_state):
            movement_intent = self._movement_intent_for_request(request, prior_state)
            movement_catalog: list[dict[str, Any]] = []
            if movement_intent:
                location_id, definition = movement_intent
                activity_id = str(
                    definition.get("resolved_activity_id")
                    or next(iter(definition.get("activities") or {}), "")
                )
                if activity_id:
                    movement_catalog.append({
                        "location_id": location_id,
                        "activity_id": activity_id,
                        "aliases": list(definition.get("aliases") or ()),
                        "movement_mode": (
                            "rendezvous"
                            if request.communication_channel == "text"
                            else "joint"
                        ),
                    })
            try:
                result = self.mvp.chat(
                    request.character_id,
                    model_message,
                    session_id=session_id,
                    world_session_id=world_id,
                    communication_channel=request.communication_channel,
                    analyst_content_blocks=canonical_blocks,
                    client_message_id=None,
                    model_settings=(provider.base_url, api_key, request.model),
                    model_info={
                        "provider_id": provider.provider_id,
                        "model_name": request.model,
                        "reason": "public_byok",
                    },
                    thinking_decision=_public_immersive_thinking_decision(provider),
                    max_tokens_override=1600,
                    persist_exchange=False,
                    remember_session=False,
                    public_sticker_candidates=sticker_candidates,
                    # Do not ask the model to invent movement. The catalog is
                    # exposed only after the server has resolved an eligible
                    # current invitation or bounded-history continuation.
                    public_state_update_catalog=movement_catalog,
                )
            except Exception as exc:
                from .mvp_service import MVPProviderError

                if not isinstance(exc, MVPProviderError):
                    raise
                return self._terminal_result(
                    request,
                    provider,
                    total_started,
                    code="provider_request_failed",
                    error_stage="provider",
                )
            adjustments = list(result.get("response_adjustments") or [])
            if result.get("content_block_guard_rejected"):
                return self._terminal_result(
                    request,
                    provider,
                    total_started,
                    code="role_guard_rejected",
                    error_stage="generation_validation",
                    result=result,
                    response_adjustments=adjustments,
                    rejected_adjustments=["in_person_block_semantics"],
                )
            rejected_adjustments = sorted(
                set(adjustments).intersection(_PUBLIC_WHOLE_ANSWER_FALLBACKS)
            )
            activity_fallback = self._allows_public_activity_fallback(
                request,
                result,
                rejected_adjustments,
            )
            if rejected_adjustments:
                if not activity_fallback:
                    terminal_error = (
                        "upstream_invalid_response"
                        if "empty_model_output_guard" in rejected_adjustments
                        else "role_guard_rejected"
                    )
                    return self._terminal_result(
                        request,
                        provider,
                        total_started,
                        code=terminal_error,
                        error_stage="generation_validation",
                        result=result,
                        response_adjustments=adjustments,
                        rejected_adjustments=rejected_adjustments,
                    )
            content_blocks, answer, truncated, safety_category = self._public_generation_content(
                result,
                request.communication_channel,
                sticker_candidates=sticker_candidates,
                explicit_sticker=_contains_any(request.message, _STICKER_EXPLICIT_TERMS),
                sticker_diagnostics=sticker_diagnostics,
                response_adjustments=adjustments,
            )
            if not content_blocks or (
                not answer and not any(block.get("type") == "sticker" for block in content_blocks)
            ):
                return self._terminal_result(
                    request,
                    provider,
                    total_started,
                    code="upstream_invalid_response",
                    error_stage="generation_validation",
                    result=result,
                    response_adjustments=adjustments,
                )
            world_snapshot = self.mvp._world_snapshot(world_id)
            communication_event = StateEvent(
                event_id=str(request.request_id),
                event_type="communication",
                character_id=request.character_id,
                communication_channel=request.communication_channel,
                location=world_snapshot.get("analyst_location"),
            )
            next_state = self._state_with_event(
                prior_state,
                event=communication_event,
                world_snapshot=world_snapshot,
            )
            try:
                next_state, movement_event, state_update_diagnostics = self._apply_joint_movement(
                    next_state,
                    request,
                    {**result, "answer": answer},
                )
            except Exception:
                # State is presentation metadata. A signing or state-shape
                # regression must never discard a valid model reply.
                movement_event = None
                state_update_diagnostics = {
                    "state_update_status": "state_unchanged",
                    "state_update_rejected_reason": "state_apply_failed",
                }
            response_state_event = movement_event or communication_event
            response_scene_state = self._scene_state(next_state, request.character_id)
            request_health = self.repository.request_health()
            degraded = sorted(service for service, status in request_health.items() if status != "ok")
            if activity_fallback or set(adjustments).intersection(_PUBLIC_REWRITE_ADJUSTMENTS):
                generation_outcome = "valid_rewrite"
            elif set(adjustments).intersection(_PUBLIC_NORMALIZATION_ADJUSTMENTS):
                generation_outcome = "normalized"
            else:
                generation_outcome = "valid_initial"
            validation_disposition = str(result.get("validation_disposition") or "")
            if (
                validation_disposition == "accepted"
                and set(adjustments).intersection(_PUBLIC_NORMALIZATION_ADJUSTMENTS)
            ):
                validation_disposition = "normalized"
            elif validation_disposition not in {"accepted", "normalized", "safe_fallback"}:
                validation_disposition = (
                    "safe_fallback"
                    if activity_fallback or bool(set(adjustments).difference(_PUBLIC_NORMALIZATION_ADJUSTMENTS | _PUBLIC_REWRITE_ADJUSTMENTS))
                    else "normalized"
                    if adjustments
                    else "accepted"
                )
            state_status = str(state_update_diagnostics.get("state_update_status") or "not_requested")
            movement_status = {
                "status": (
                    state_status
                    if state_status in {
                        "applied",
                        "already_applied",
                        "character_waiting",
                        "state_unchanged",
                    }
                    else "not_accepted"
                    if state_update_diagnostics.get("state_update_rejected_reason") == "character_did_not_accept_now"
                    else "unresolved"
                    if movement_intent
                    else "not_requested"
                ),
                **{
                    key: state_update_diagnostics.get(key)
                    for key in (
                        "location_id",
                        "location_name",
                        "display_name",
                        "activity_id",
                        "target_character_id",
                    )
                    if state_update_diagnostics.get(key)
                },
                **(
                    {
                        "character_id": request.character_id,
                        "pending_rendezvous": state_update_diagnostics.get(
                            "pending_rendezvous"
                        ),
                    }
                    if state_status == "character_waiting"
                    else {}
                ),
                "schedule_date": str(next_state.get("schedule_date") or ""),
                "schedule_revision": int(next_state.get("schedule_revision") or 1),
            }
            return {
                "request_id": str(request.request_id),
                "character_id": request.character_id,
                "provider": provider.provider_id,
                "model": redact_sensitive_text(request.model, 200),
                "answer": answer,
                "communication_channel": request.communication_channel,
                "stage_motion": _normalize_stage_motion(
                    result.get("stage_motion"),
                    request.communication_channel,
                ),
                "content_blocks": content_blocks,
                "truncated": truncated,
                "state_package": sign_state(self.public_settings, next_state),
                "scene_state": response_scene_state,
                "state_event": response_state_event.model_dump(),
                "degraded_services": degraded,
                "retrieval": result.get("retrieval") or {},
                "usage": result.get("usage") or {},
                "safety_category": safety_category,
                "generation_outcome": generation_outcome,
                "validation_disposition": validation_disposition,
                "movement_status": movement_status,
                "pending_rendezvous": self._pending_for_character(
                    next_state,
                    request.character_id,
                ),
                "recovery_action": (
                    "refresh_scene"
                    if movement_status.get("status") == "state_unchanged"
                    else "none"
                ),
                "response_adjustments": adjustments,
                "terminal_error": "",
                "diagnostics": {
                    **self._diagnostics(
                        total_started,
                        result,
                        generation_class=generation_outcome,
                    ),
                    **sticker_diagnostics,
                    **state_update_diagnostics,
                },
            }

    def _allows_public_activity_fallback(
        self,
        request: ChatRequest,
        result: dict[str, Any],
        rejected_adjustments: list[str],
    ) -> bool:
        """Allow the safe current-activity answer to reach the public UI.

        The dialogue engine may replace a provider answer with a deterministic
        activity-only response when a provider paraphrases the shared scene
        label.  That response contains no private location or analyst claim,
        so treating it as a role rejection made ordinary questions such as
        ``你在干什么`` unusable.  Keep every other whole-answer fallback on
        the existing rejection path.
        """
        if not rejected_adjustments:
            return False
        focus = str(result.get("question_focus") or "")
        if not focus:
            try:
                focus = self.mvp._question_focus(
                    str(request.message or ""),
                    self.mvp._query_intents(str(request.message or "")),
                )
            except Exception:
                focus = ""
        if focus != "current_activity":
            return False
        return set(rejected_adjustments).issubset(
            {
                "live_scene_guard",
                "scene_privacy_guard",
                "mechanical_dialogue_guard",
                "natural_dialogue_guard",
                "repetition_guard",
                "direct_answer_guard",
            }
        )

    def _public_generation_content(
        self,
        result: dict[str, Any],
        communication_channel: str,
        *,
        sticker_candidates: list[dict[str, Any]] | None = None,
        explicit_sticker: bool = False,
        sticker_diagnostics: dict[str, Any] | None = None,
        response_adjustments: list[str] | None = None,
    ) -> tuple[list[dict[str, str]], str, bool, str | None]:
        allowed = {"message", "sticker"} if communication_channel == "text" else {"speech", "action"}
        text_blocks: list[dict[str, str]] = []
        sticker_block: dict[str, str] | None = None
        for item in result.get("content_blocks") or []:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").casefold()
            if block_type == "sticker":
                asset_id = str(item.get("asset_id") or "")
                resolved = self.stickers.resolve(asset_id)
                candidate_ids = {
                    str(candidate.get("asset_id") or "")
                    for candidate in (sticker_candidates or [])
                    if isinstance(candidate, dict)
                }
                candidate_allowed = (
                    sticker_candidates is None
                    or asset_id in candidate_ids
                )
                if communication_channel == "text" and resolved and candidate_allowed and sticker_block is None:
                    sticker_block = {
                        "type": "sticker",
                        "asset_id": str(resolved.get("asset_id") or asset_id),
                        "caption": _strip_sticker_filenames(str(resolved.get("caption") or ""))[:120],
                        "src": str(resolved.get("src") or ""),
                        "thumbnail_src": str(resolved.get("thumbnail_src") or ""),
                        "display_src": str(resolved.get("display_src") or ""),
                        "display_mime_type": str(resolved.get("display_mime_type") or ""),
                        "display_animated": bool(resolved.get("display_animated")),
                        "animated": bool(resolved.get("animated")),
                    }
                    if sticker_diagnostics is not None:
                        sticker_diagnostics.update({"sticker_selected": asset_id, "sticker_gate": "model"})
                elif communication_channel == "text" and resolved and not candidate_allowed:
                    if sticker_diagnostics is not None:
                        sticker_diagnostics.setdefault("sticker_rejected_reason", "asset_outside_candidate_scope")
                elif communication_channel == "text" and not resolved:
                    if sticker_diagnostics is not None:
                        sticker_diagnostics.setdefault("sticker_rejected_reason", "unknown_asset")
                continue
            text = _strip_sticker_filenames(str(item.get("text") or "").strip())
            if block_type in allowed and text:
                text_blocks.append({"type": block_type, "text": text})
            if len(text_blocks) >= 8:
                break
        if not text_blocks:
            answer = _strip_sticker_filenames(str(result.get("answer") or "").strip())
            default_type = "message" if communication_channel == "text" else "speech"
            if answer:
                text_blocks = [{"type": default_type, "text": answer}]
        raw_blocks: list[dict[str, Any]] = [*text_blocks]
        if sticker_block is not None and communication_channel == "text":
            raw_blocks.append(sticker_block)
        elif explicit_sticker and communication_channel == "text" and sticker_candidates:
            # The explicit user request is a deterministic server-side gate:
            # if the model omitted a sticker, select the first verified
            # candidate supplied for this request.  URLs are still resolved
            # through the signed catalog before entering the response.
            selected_id = str(sticker_candidates[0].get("asset_id") or "")
            resolved = self.stickers.resolve(selected_id)
            if resolved:
                sticker_block = {
                    "type": "sticker",
                    "asset_id": str(resolved.get("asset_id") or selected_id),
                    "caption": _strip_sticker_filenames(str(resolved.get("caption") or ""))[:120],
                    "src": str(resolved.get("src") or ""),
                    "thumbnail_src": str(resolved.get("thumbnail_src") or ""),
                    "display_src": str(resolved.get("display_src") or ""),
                    "display_mime_type": str(resolved.get("display_mime_type") or ""),
                    "display_animated": bool(resolved.get("display_animated")),
                    "animated": bool(resolved.get("animated")),
                }
                raw_blocks.append(sticker_block)
                if sticker_diagnostics is not None:
                    sticker_diagnostics.update({"sticker_selected": selected_id, "sticker_gate": "explicit"})
            elif sticker_diagnostics is not None:
                sticker_diagnostics.update({"sticker_rejected_reason": "unknown_asset"})
        elif sticker_diagnostics is not None:
            sticker_diagnostics.setdefault(
                "sticker_rejected_reason",
                "no_matching_candidate" if explicit_sticker else "model_declined",
            )
        rendered = "\n".join(
            str(block.get("text") or "").strip()
            for block in raw_blocks
            if str(block.get("text") or "").strip()
        ).strip()
        safe_answer, safety_category = safe_output(rendered)
        if safety_category:
            default_type = "message" if communication_channel == "text" else "speech"
            raw_blocks = [{"type": default_type, "text": safe_answer}]
        trimmed, truncated = _trim_content_blocks(raw_blocks)
        # Normalize only final public display prose. Structured parsing and
        # output security run against the provider's original bytes; trimming
        # first keeps this cleanup bounded and gives blocks and answer the same
        # normalized text.
        punctuation_normalized = False
        for block in trimmed:
            if block.get("type") != "sticker" and str(block.get("text") or "").strip():
                original_text = str(block["text"])
                normalized_text = _normalize_public_immersive_punctuation(original_text)
                block["text"] = normalized_text
                punctuation_normalized = punctuation_normalized or normalized_text != original_text
        if (
            punctuation_normalized
            and response_adjustments is not None
            and "public_punctuation_normalized" not in response_adjustments
        ):
            response_adjustments.append("public_punctuation_normalized")
        # Enforce the product order even if a provider returned sticker first.
        trimmed = [
            *[block for block in trimmed if block.get("type") != "sticker"],
            *[block for block in trimmed if block.get("type") == "sticker"][:1],
        ]
        answer = "\n".join(
            str(block.get("text") or "").strip()
            for block in trimmed
            if str(block.get("text") or "").strip()
        ).strip()
        return trimmed, answer, truncated, safety_category

    def _terminal_result(
        self,
        request: ChatRequest,
        provider: ProviderSpec,
        total_started: float,
        *,
        code: str,
        error_stage: str,
        result: dict[str, Any] | None = None,
        response_adjustments: list[str] | None = None,
        rejected_adjustments: list[str] | None = None,
    ) -> dict[str, Any]:
        result = result or {}
        diagnostics = self._diagnostics(
            total_started,
            result,
            generation_class="rejected",
            guard_code=(rejected_adjustments[0] if rejected_adjustments else str(result.get("guard_code") or "")),
            error_stage=error_stage,
        )
        if rejected_adjustments:
            diagnostics["rejected_adjustments"] = rejected_adjustments
        request_health = self.repository.request_health()
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "provider": provider.provider_id,
            "model": redact_sensitive_text(request.model, 200),
            "answer": "",
            "communication_channel": request.communication_channel,
            "content_blocks": [],
            "truncated": False,
            "state_package": request.state_package,
            "degraded_services": sorted(
                service for service, service_status in request_health.items() if service_status != "ok"
            ),
            "retrieval": result.get("retrieval") or {},
            "usage": result.get("usage") or {},
            "safety_category": None,
            "generation_outcome": "rejected",
            "validation_disposition": "rejected",
            "movement_status": {"status": "state_unchanged"},
            "response_adjustments": response_adjustments or [],
            "terminal_error": code,
            "diagnostics": diagnostics,
        }

    def _diagnostics(
        self,
        total_started: float,
        result: dict[str, Any] | None = None,
        *,
        generation_class: str = "",
        guard_code: str = "",
        error_stage: str = "",
    ) -> dict[str, Any]:
        result = result or {}
        repository_diagnostics = self.repository.request_diagnostics()
        timings = dict(repository_diagnostics.get("timings_ms") or {})
        timings.update(self.mvp.generation_diagnostics())
        # Keep the diagnostic contract stable for feedback and replay records,
        # including policy rejections or failures that happen before a model
        # generation starts.
        for key in ("provider_http_calls", "model_calls", "initial_model_ms", "guard_rewrite_ms"):
            timings.setdefault(key, 0)
        timings["total"] = max(0, int((time.perf_counter() - total_started) * 1000))
        # Keep both the historical timings map and the compact fields used by
        # the feedback/admin surface.  These values are derived from the
        # request-local collectors and never contain prompts, responses or
        # provider credentials.
        diagnostics = {
            "timings_ms": timings,
            "dependency_health": repository_diagnostics.get("dependency_health") or {},
            "provider_latency_ms": int(timings.get("initial_model_ms") or 0),
            "retrieval_latency_ms": int(
                timings.get("hybrid_total")
                or timings.get("fts5")
                or 0
            ),
            "rewrite_latency_ms": int(timings.get("guard_rewrite_ms") or 0),
            "model_calls": int(timings.get("model_calls") or 0),
            "generation_class": generation_class or str(result.get("generation_class") or ""),
            "guard_code": guard_code or str(result.get("guard_code") or ""),
            "guard_violation_count": int(result.get("guard_violation_count") or 0),
            "final_guard_violation_count": int(result.get("final_guard_violation_count") or 0),
            "guard_resolution": str(result.get("guard_resolution") or ""),
        }
        if error_stage:
            diagnostics["error_stage"] = error_stage
        return diagnostics

    async def summarize(
        self,
        request: SummarizeRequest,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        transcript = "\n".join(
            f"{item.role}[{item.communication_channel}]: "
            + " | ".join(
                f"sticker:{block.caption or block.asset_id}"
                if block.type == "sticker"
                else f"{block.type}:{block.text}"
                for block in item.content_blocks
            )
            for item in request.turns
        )
        prompt = json.dumps(
            {
                "previous_summary": request.previous_summary,
                "new_turns": transcript,
                "required": {
                    "confirmed_relationships": "仅记录用户明确确认的关系与称呼",
                    "world_state": "仅记录明确发生或确认的状态",
                    "open_threads": "仍待继续的话题",
                    "summary": "不超过1200字的中文摘要",
                },
            },
            ensure_ascii=False,
        )
        async def generate_summary() -> tuple[str, dict[str, Any]]:
            return await simple_completion(
                provider,
                api_key,
                request.model,
                system_prompt=(
                    "你是会话压缩器。只总结明确内容，不推断隐私，不输出Markdown代码块。"
                    "请只返回 JSON：{\"summary\":\"不超过1200字的中文摘要\","
                    "\"open_threads\":[\"仍待继续的话题，最多12条\"]}。"
                    "open_threads 只能记录可选话题，不能写成用户必须完成的承诺。"
                ),
                user_prompt=prompt,
                max_tokens=1200,
                client=self.provider_client,
            )

        # Summaries use the same global 4-active/8-queued budget as chat and
        # arrival generation. Otherwise a burst of browser-side checkpoints
        # could bypass the public generation cap entirely.
        content, usage = await self.gate.run(generate_summary)
        summary = str(content or "").strip()
        pending_topics: list[str] = []
        # Newer providers may follow the structured summary contract. Keep a
        # plain-text fallback so older models remain compatible.
        try:
            parsed = json.loads(summary)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            candidate_summary = parsed.get("summary")
            if isinstance(candidate_summary, str) and candidate_summary.strip():
                summary = candidate_summary.strip()
            candidate_topics = parsed.get("open_threads") or parsed.get("pending_topics")
            if isinstance(candidate_topics, list):
                pending_topics = [
                    str(topic).strip()[:240]
                    for topic in candidate_topics
                    if str(topic).strip()
                ][:12]
        return {
            "request_id": str(request.request_id),
            "summary": summary[:6000],
            "pending_topics": pending_topics,
            "usage": usage,
        }
