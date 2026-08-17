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
from .mvp_service import MVPService, _SESSION_STATES, _SESSION_LOCK, _WORLD_STATES, _WORLD_STATE_LOCK
from .public_contracts import (
    ChatRequest,
    HistoryTurn,
    PresenceArrivalRequest,
    PresenceResolveRequest,
    PresenceTransitionRequest,
    StateEvent,
    StatePayload,
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


_PUBLIC_WHOLE_ANSWER_FALLBACKS = frozenset(
    {
        "immersive_boundary_fallback",
        "live_scene_guard",
        "companion_social_guard",
        "fenny_voice_guard",
        "communication_guard",
        "analyst_premise_guard",
        "session_premise_guard",
        "casual_state_guard",
        "current_food_guard",
        "shared_meal_guard",
        "routine_activity_guard",
        "open_invitation_guard",
        "signature_frequency_guard",
        "cross_character_guard",
        "dual_persona_guard",
        "direct_answer_guard",
        "scene_privacy_guard",
        "continuity_guard",
        "interaction_hint_guard",
        "natural_dialogue_guard",
        "plot_recap_guard",
        "mechanical_dialogue_guard",
        "visit_location_guard",
        "repetition_guard",
        "logistics_evidence_fallback",
        "relationship_roster_guard",
        "empty_model_output_guard",
    }
)

# Diagnostics exposed through the public idempotency record describe the
# answer actually shown to the user. State transitions, style-profile lookup,
# and other bookkeeping adjustments must not make an otherwise untouched
# provider response look "normalized".
_PUBLIC_REWRITE_ADJUSTMENTS = frozenset({
    "answer_guardrail_retry",
    "in_person_block_rewrite",
})
_PUBLIC_NORMALIZATION_ADJUSTMENTS = frozenset({
    "explicit_relationship_guard",
    "latest_state_guard",
    "in_person_blocks_reclassified",
    "address_alias_normalized",
    "relationship_address_normalized",
    "unsupported_quote_sanitized",
})


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

    async def run(self, callback):
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
        try:
            return await callback()
        finally:
            self.semaphore.release()


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
    limit: int = 1200,
) -> tuple[list[dict[str, str]], bool]:
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


def _strip_sticker_filenames(value: str) -> str:
    """Prevent a provider from leaking local media filenames into dialogue."""

    return _STICKER_FILENAME_PATTERN.sub("", str(value or "")).strip()


def _current_hong_kong_day() -> str:
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()


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
    ) -> list[dict[str, Any]]:
        """Return a deterministic candidate set for the optional 20% branch.

        The roll is derived from the request UUID, character and Hong Kong
        date.  Retrying the same idempotency key therefore cannot cause the
        character to alternate between sending and not sending a sticker.
        A candidate set is only exposed to the model when the roll succeeds;
        the model may still choose not to send one.
        """

        digest = sha256(
            f"sticker\x1f{request_id}\x1f{character_id}\x1f{_current_hong_kong_day()}".encode()
        ).digest()
        if digest[0] >= 51:  # 51/256 ~= 19.9%
            return []
        catalog = self.stickers.list(limit=500).get("stickers") or []
        if not catalog:
            return []
        start = int.from_bytes(digest[1:3], "big") % len(catalog)
        ordered = (catalog[start:] + catalog[:start])[:8]
        return [
            {
                "asset_id": str(item.get("asset_id") or ""),
                "caption": str(item.get("caption") or "")[:120],
                "tags": [str(item.get("section") or "未分类")],
            }
            for item in ordered
            if item.get("asset_id")
        ]

    def characters(self) -> list[dict[str, Any]]:
        result = []
        for character in MVP_CHARACTERS:
            avatar = self.media.avatar(character.character_id) or {
                "src": f"/media/{self.public_settings.media_version}/avatars/{character.character_id}-200.webp",
                "thumbnail_src": f"/media/{self.public_settings.media_version}/avatars/{character.character_id}-96.webp",
                "portrait_kind": "headshot",
                "portrait_scale": 1.0,
                "portrait_focus_x": 50,
                "portrait_focus_y": 50,
                "source_page": "",
                "license": "CC BY-NC-SA",
                "license_version": "version unspecified by source",
            }
            result.append(
                {
                    "character_id": character.character_id,
                    "display_name": character.display_name,
                    "aliases": list(character.aliases),
                    # The package is mounted separately from the GPL image and
                    # is verified before any URL is advertised to a client.
                    "avatar": avatar,
                }
            )
        return result

    def _default_state(self, subject_hash: str) -> dict[str, Any]:
        # Presence is intentionally shared across anonymous users for one
        # Hong Kong calendar day.  The analyst's location remains local to
        # the signed browser state and is not part of this shared schedule.
        hong_kong_now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        schedule_start = hong_kong_now.replace(hour=0, minute=0, second=0, microsecond=0)
        schedule_date = schedule_start.date().isoformat()
        schedule_expires = schedule_start + timedelta(days=1)
        schedule_key = self.public_settings.state_hmac_key or b"project-snow-public-schedule-dev"
        daily_seed = hmac.new(schedule_key, schedule_date.encode("ascii"), sha256).hexdigest()
        world_id = "public_shared_schedule_" + daily_seed[:32]
        world = self.mvp._world_snapshot(world_id)
        presence = {
            character_id: {
                **dict(scene),
                "state_scope": "shared_daily",
            }
            for character_id, scene in (world.get("presence") or {}).items()
        }
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=1,
            analyst_location=world.get("analyst_location"),
            presence=presence,
            relationships={},
            recent_events=[],
            schedule_date=schedule_date,
            schedule_revision=1,
            generated_at=schedule_start.isoformat(),
            expires_at=schedule_expires.isoformat(),
        ).model_dump()

    def _normalized_state(self, token: str, subject_hash: str) -> dict[str, Any]:
        raw = verify_state(self.public_settings, token)
        defaults = self._default_state(subject_hash)
        if not raw:
            return defaults

        schema_version = str(raw.get("schema_version") or "public-state-1")
        if schema_version not in {"public-state-1", "public-state-2"}:
            raise PublicSecurityError("Public state schema is not supported")
        incoming_schedule_date = str(raw.get("schedule_date") or "")
        if schema_version == "public-state-1":
            old_world = raw.get("world") if isinstance(raw.get("world"), dict) else {}
            incoming_presence = old_world.get("presence") or {}
            analyst_location = old_world.get("analyst_location")
            recent_events: list[dict[str, Any]] = []
            # Legacy state packages are user-authored conversation snapshots;
            # preserve their known scene while upgrading the envelope.
            incoming_schedule_date = str(defaults.get("schedule_date") or "")
        else:
            incoming_presence = raw.get("presence") or {}
            analyst_location = raw.get("analyst_location")
            recent_events = list(raw.get("recent_events") or [])[-4:]

        # A stale browser package must not keep yesterday's global schedule
        # alive.  Keep only the user's analyst location and relationship data.
        if incoming_schedule_date != str(defaults.get("schedule_date") or ""):
            incoming_presence = {}

        canonical = {character.character_id: character for character in MVP_CHARACTERS}
        presence: dict[str, dict[str, Any]] = {}
        for character_id, character in canonical.items():
            fallback = dict(defaults["presence"][character_id])
            candidate = (
                dict(incoming_presence.get(character_id) or {})
                if isinstance(incoming_presence, dict)
                else {}
            )
            presence[character_id] = {
                "character_id": character_id,
                "character_name": character.display_name,
                "location": str(candidate.get("location") or fallback["location"])[:120],
                "activity": str(candidate.get("activity") or fallback["activity"])[:240],
                "state_scope": (
                    "conversation_confirmed"
                    if candidate.get("state_scope") == "conversation_confirmed"
                    else "shared_daily"
                    if candidate.get("state_scope") == "shared_daily"
                    else fallback.get("state_scope", "shared_daily")
                ),
            }

        validated_events: list[dict[str, Any]] = []
        for event in recent_events:
            try:
                validated_events.append(StateEvent.model_validate(event).model_dump())
            except (TypeError, ValueError):
                continue
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=max(0, int(raw.get("revision") or 0)),
            analyst_location=str(analyst_location)[:120] if analyst_location else None,
            presence=presence,
            relationships=dict(raw.get("relationships") or {}),
            recent_events=validated_events[-4:],
            schedule_date=str(defaults.get("schedule_date") or ""),
            schedule_revision=int(defaults.get("schedule_revision") or 1),
            generated_at=str(defaults.get("generated_at") or ""),
            expires_at=str(defaults.get("expires_at") or ""),
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
            recent_events=events,
            schedule_date=str(state.get("schedule_date") or ""),
            schedule_revision=int(state.get("schedule_revision") or 1),
            generated_at=str(state.get("generated_at") or ""),
            expires_at=str(state.get("expires_at") or ""),
        ).model_dump()

    def resolve_presence(
        self,
        request: PresenceResolveRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "scene_state": self._scene_state(state, request.character_id),
            "state_package": sign_state(self.public_settings, state),
            "schema_version": "public-state-2",
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
        next_location = state.get("analyst_location")
        if request.target_channel == "in_person":
            next_location = before.get("character_location")
            if not next_location:
                raise ValueError("character scene has no location")
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
                    thinking_decision={
                        "requested": "off",
                        "effective": "off",
                        "reason": "public_immersive_policy",
                        "provider_kind": provider.provider_id,
                    },
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
    ) -> dict[str, Any]:
        async def generate():
            return await asyncio.to_thread(self._chat_sync, request, subject_hash, provider, api_key)

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
        sticker_candidates = (
            self.sticker_candidates(
                request_id=str(request.request_id),
                character_id=request.character_id,
            )
            if request.communication_channel == "text"
            else []
        )
        with self._request_state(request, subject_hash) as (session_id, world_id, prior_state):
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
                    thinking_decision={
                        "requested": "off",
                        "effective": "off",
                        "reason": "public_immersive_policy",
                        "provider_kind": provider.provider_id,
                    },
                    max_tokens_override=1600,
                    persist_exchange=False,
                    remember_session=False,
                    public_sticker_candidates=sticker_candidates,
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
            next_state = self._state_with_event(
                prior_state,
                event=StateEvent(
                    event_id=str(request.request_id),
                    event_type="communication",
                    character_id=request.character_id,
                    communication_channel=request.communication_channel,
                    location=world_snapshot.get("analyst_location"),
                ),
                world_snapshot=world_snapshot,
            )
            request_health = self.repository.request_health()
            degraded = sorted(service for service, status in request_health.items() if status != "ok")
            if activity_fallback or set(adjustments).intersection(_PUBLIC_REWRITE_ADJUSTMENTS):
                generation_outcome = "valid_rewrite"
            elif set(adjustments).intersection(_PUBLIC_NORMALIZATION_ADJUSTMENTS):
                generation_outcome = "normalized"
            else:
                generation_outcome = "valid_initial"
            return {
                "request_id": str(request.request_id),
                "character_id": request.character_id,
                "provider": provider.provider_id,
                "model": redact_sensitive_text(request.model, 200),
                "answer": answer,
                "communication_channel": request.communication_channel,
                "content_blocks": content_blocks,
                "truncated": truncated,
                "state_package": sign_state(self.public_settings, next_state),
                "degraded_services": degraded,
                "retrieval": result.get("retrieval") or {},
                "usage": result.get("usage") or {},
                "safety_category": safety_category,
                "generation_outcome": generation_outcome,
                "response_adjustments": adjustments,
                "terminal_error": "",
                "diagnostics": self._diagnostics(
                    total_started,
                    result,
                    generation_class=generation_outcome,
                ),
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
                if communication_channel == "text" and resolved and sticker_block is None:
                    sticker_block = {
                        "type": "sticker",
                        "asset_id": str(resolved.get("asset_id") or asset_id),
                        "caption": _strip_sticker_filenames(str(resolved.get("caption") or ""))[:120],
                        "src": str(resolved.get("src") or ""),
                        "thumbnail_src": str(resolved.get("thumbnail_src") or ""),
                        "animated": bool(resolved.get("animated")),
                    }
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
        content, usage = await simple_completion(
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
