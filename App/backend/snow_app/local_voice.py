"""Fail-closed local routing for the selected Beijing Qwen VC voices."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import time
import unicodedata
import uuid
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

PROFILE_SCHEMA = "project-snow-private-local-voice-runtime-profile-1"
PROFILE_STATUS = "offline_local_runtime_profile_ready"
PROFILE_DIRECTORY = "tts_runtime_profiles"
MODEL = "qwen3-tts-vc-realtime-2026-01-15"
REGION = "cn-beijing"
WEBSOCKET_ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
RESPONSE_FORMAT = "pcm"
LANGUAGE_TYPE = "Chinese"
MODE = "commit"
MAX_TEXT_CHARACTERS = 8_000
MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENT_COUNT = 20_000
EVENT_TIMEOUT_SECONDS = 60.0
WHOLE_EXCHANGE_TIMEOUT_SECONDS = 180.0

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
PROFILE_ID_PATTERN = re.compile(r"voice-runtime-profile-[0-9a-f]{20}\Z")
WORKSPACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{2,127}\Z")
VOICE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
EXPECTED_CHARACTERS = {
    "5157b8972632": ("vidya", "薇蒂雅"),
    "98322bd505f4": ("chenxing", "辰星"),
}
EXPECTED_STYLES = ("neutral", "breathy", "heightened")

BREATHY_CUES = (
    "轻轻",
    "轻声",
    "小声",
    "悄悄",
    "耳边",
    "靠近",
    "贴近",
    "别出声",
    "嘘",
    "呼吸",
    "放轻",
    "不必惊动",
)
HEIGHTENED_CUES = (
    "看着我",
    "别闭眼",
    "醒醒",
    "抓住",
    "快点",
    "立刻",
    "马上",
    "不许",
    "危险",
    "终于",
    "一定",
)


class LocalVoiceError(ValueError):
    """Raised when local voice routing or synthesis cannot proceed safely."""


class LocalVoiceSlotPaused(LocalVoiceError):
    """Raised before network access when a selected style has no qualified voice."""

    def __init__(self, *, style: str, case_id: str) -> None:
        super().__init__(f"语态槽位暂不可用：{case_id}")
        self.style = style
        self.case_id = case_id


@dataclass(frozen=True)
class VoiceRoute:
    character_id: str
    character_slug: str
    character_name: str
    case_id: str
    style: str
    status: str
    provider_voice_id: str | None = None


@dataclass(frozen=True)
class VoiceReplyDecision:
    should_synthesize: bool
    auto_play: bool
    reason: str
    probability: float
    stable_sample: float | None


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise LocalVoiceError(f"本地语音配置无效：{label}")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalVoiceError(f"本地语音配置无效：{label}")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise LocalVoiceError(f"本地语音配置无效：{label}")
    return value


def _string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LocalVoiceError(f"本地语音配置无效：{label}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise LocalVoiceError("本地语音配置包含重复字段。")
        value[key] = item
    return value


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LocalVoiceError("本地语音配置文件不存在。") from error
    if not resolved.is_file() or resolved.is_symlink():
        raise LocalVoiceError("本地语音配置必须是普通文件。")
    before = resolved.stat()
    if before.st_size > 1024 * 1024:
        raise LocalVoiceError("本地语音配置文件过大。")
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise LocalVoiceError("本地语音配置在读取期间发生变化。")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalVoiceError("本地语音配置不是有效的 UTF-8 JSON。") from error
    return _object(document, label="根对象")


def discover_profile_path(data_root: Path, explicit_path: str = "") -> Path:
    """Find one compatible private profile without selecting ambiguous state."""

    voice_root = (Path(data_root).resolve() / "Voice").resolve()
    profile_root = voice_root / PROFILE_DIRECTORY
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve(strict=True)
        try:
            path.relative_to(profile_root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise LocalVoiceError("本地语音配置必须位于私有 Voice 数据目录。") from error
        return path
    if not profile_root.is_dir():
        raise LocalVoiceError("没有找到本地语音运行配置。")
    compatible: list[Path] = []
    for path in sorted(profile_root.glob("voice-runtime-profile-*/manifest.json")):
        try:
            document = _read_profile(path)
        except LocalVoiceError:
            continue
        provider = document.get("provider_contract")
        if (
            document.get("schema_version") == PROFILE_SCHEMA
            and isinstance(provider, dict)
            and isinstance(provider.get("workspace_id"), str)
        ):
            compatible.append(path.resolve())
    if not compatible:
        raise LocalVoiceError("没有找到包含北京 Workspace 绑定的本地语音配置。")
    if len(compatible) != 1:
        raise LocalVoiceError("检测到多个本地语音配置，请显式指定一个配置文件。")
    return compatible[0]


def classify_style(text: str) -> str:
    """Conservatively classify lexical delivery; non-lexical events stay excluded."""

    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not normalized:
        raise LocalVoiceError("语音文本不能为空。")
    exclamations = normalized.count("!")
    heightened_cue = any(cue in normalized for cue in HEIGHTENED_CUES)
    if exclamations >= 2 or (exclamations >= 1 and heightened_cue):
        return "heightened"
    if any(cue in normalized for cue in BREATHY_CUES):
        return "breathy"
    return "neutral"


def _bounded_probability(value: Any, *, fallback: float) -> float:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return fallback
    if probability != probability or probability in (float("inf"), float("-inf")):
        return fallback
    return max(0.0, min(1.0, probability))


def decide_voice_reply(
    *,
    enabled_by_user: bool,
    communication_channel: str,
    character_id: str,
    message_id: str,
    text: str,
    text_probability: float = 0.25,
    emotion_probability: float = 0.45,
) -> VoiceReplyDecision:
    """Make an idempotent channel decision without opening a Provider connection."""

    if not enabled_by_user:
        return VoiceReplyDecision(False, False, "user_voice_preference_disabled", 0.0, None)
    if communication_channel == "in_person":
        return VoiceReplyDecision(True, True, "in_person_auto_voice", 1.0, None)
    if communication_channel != "text":
        return VoiceReplyDecision(False, False, "unsupported_communication_channel", 0.0, None)
    strong_emotion = classify_style(text) == "heightened"
    probability = _bounded_probability(
        emotion_probability if strong_emotion else text_probability,
        fallback=0.45 if strong_emotion else 0.25,
    )
    identity = "\x1f".join(
        (
            "project-snow-local-voice-offer-1",
            str(character_id),
            str(message_id),
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    ).encode("utf-8")
    sample = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big") / float(1 << 64)
    return VoiceReplyDecision(
        sample < probability,
        False,
        "text_strong_emotion_probability" if strong_emotion else "text_daily_probability",
        probability,
        sample,
    )


def _load_routes(document: dict[str, Any]) -> tuple[str, str, dict[tuple[str, str], VoiceRoute]]:
    _expect(document.get("schema_version"), PROFILE_SCHEMA, label="schema")
    _expect(document.get("status"), PROFILE_STATUS, label="状态")
    profile_id = _string(document.get("profile_id"), label="配置 ID")
    if not PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise LocalVoiceError("本地语音配置无效：配置 ID")
    digest = _string(document.get("manifest_sha256"), label="配置摘要")
    if not SHA256_PATTERN.fullmatch(digest):
        raise LocalVoiceError("本地语音配置无效：配置摘要")
    basis = dict(document)
    basis.pop("manifest_sha256", None)
    _expect(_canonical_sha256(basis), digest, label="配置摘要校验")

    provider = _object(document.get("provider_contract"), label="Provider 合同")
    for key, expected in (
        ("region", REGION),
        ("target_model", MODEL),
        ("websocket_endpoint", WEBSOCKET_ENDPOINT),
        ("mode", MODE),
        ("language_type", LANGUAGE_TYPE),
        ("response_format", RESPONSE_FORMAT),
        ("sample_rate_hz", SAMPLE_RATE_HZ),
        ("channels", CHANNELS),
        ("sample_width_bytes", SAMPLE_WIDTH_BYTES),
        ("instruction_control_supported_by_target_model", False),
        ("instructions_sent", False),
    ):
        _expect(provider.get(key), expected, label=f"Provider {key}")
    workspace_id = _string(provider.get("workspace_id"), label="Workspace 绑定").lower()
    if not WORKSPACE_PATTERN.fullmatch(workspace_id):
        raise LocalVoiceError("本地语音配置无效：Workspace 绑定")
    _expect(
        provider.get("workspace_id_sha256"),
        _sha256_text(workspace_id),
        label="Workspace 摘要",
    )
    routing = _object(document.get("routing_contract"), label="路由合同")
    for key in (
        "paused_slot_fallback_allowed",
        "cross_character_fallback_allowed",
        "cross_style_fallback_allowed",
        "provider_voice_id_exposed_to_client",
        "user_supplied_voice_id_allowed",
        "user_supplied_model_allowed",
        "user_supplied_websocket_endpoint_allowed",
        "paralinguistic_ordinals_2_and_3_included",
    ):
        _expect(routing.get(key), False, label=f"路由 {key}")

    routes: dict[tuple[str, str], VoiceRoute] = {}
    characters = _array(document.get("characters"), label="角色路由")
    _expect(len(characters), len(EXPECTED_CHARACTERS), label="角色数量")
    for raw_character in characters:
        character = _object(raw_character, label="角色")
        character_id = _string(character.get("runtime_character_id"), label="角色 ID")
        expected_identity = EXPECTED_CHARACTERS.get(character_id)
        if expected_identity is None:
            raise LocalVoiceError("本地语音配置包含未授权角色。")
        slug, name = expected_identity
        _expect(character.get("character_slug"), slug, label="角色 slug")
        _expect(character.get("runtime_character_name"), name, label="角色名称")
        raw_routes = _array(character.get("routes"), label=f"{slug} 语态路由")
        _expect(
            [item.get("style") if isinstance(item, dict) else None for item in raw_routes],
            list(EXPECTED_STYLES),
            label=f"{slug} 语态顺序",
        )
        for raw_route in raw_routes:
            item = _object(raw_route, label=f"{slug} 语态")
            style = _string(item.get("style"), label=f"{slug} 语态")
            case_id = _string(item.get("case_id"), label=f"{slug} case")
            route_status = _string(item.get("status"), label=f"{case_id} 状态")
            voice_id: str | None = None
            if route_status == "locked":
                voice_id = _string(item.get("provider_voice_id"), label=f"{case_id} 音色")
                if not VOICE_PATTERN.fullmatch(voice_id):
                    raise LocalVoiceError(f"本地语音配置无效：{case_id} 音色")
                _expect(
                    item.get("provider_voice_id_sha256"),
                    _sha256_text(voice_id),
                    label=f"{case_id} 音色摘要",
                )
            elif route_status == "paused":
                if "provider_voice_id" in item or "provider_voice_id_sha256" in item:
                    raise LocalVoiceError(f"暂停槽位 {case_id} 不得绑定音色。")
            else:
                raise LocalVoiceError(f"本地语音配置无效：{case_id} 状态")
            key = (character_id, style)
            if key in routes:
                raise LocalVoiceError("本地语音配置包含重复路由。")
            routes[key] = VoiceRoute(
                character_id=character_id,
                character_slug=slug,
                character_name=name,
                case_id=case_id,
                style=style,
                status=route_status,
                provider_voice_id=voice_id,
            )
    expected_keys = {
        (character_id, style) for character_id in EXPECTED_CHARACTERS for style in EXPECTED_STYLES
    }
    _expect(set(routes), expected_keys, label="完整角色语态矩阵")
    return profile_id, workspace_id, routes


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"event_id": f"event_{uuid.uuid4().hex}", "type": event_type, **fields}


def _send_event(connection: Any, event_type: str, **fields: Any) -> None:
    connection.send(
        json.dumps(
            _event(event_type, **fields),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _receive_event(connection: Any, *, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LocalVoiceError("语音合成超过整体超时。")
    raw = connection.recv(timeout=min(EVENT_TIMEOUT_SECONDS, remaining))
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
        raise LocalVoiceError("语音服务返回了无效事件。")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as error:
        raise LocalVoiceError("语音服务返回了非 JSON 事件。") from error
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise LocalVoiceError("语音服务事件缺少类型。")
    if event["type"] == "error":
        details = event.get("error") if isinstance(event.get("error"), dict) else {}
        code = re.sub(r"[^A-Za-z0-9_.-]", "_", str(details.get("code") or "unknown"))[:80]
        raise LocalVoiceError(f"语音服务返回错误（{code}）。")
    return event


def _receive_until(connection: Any, expected: str, *, deadline: float) -> dict[str, Any]:
    for _ in range(MAX_EVENT_COUNT):
        event = _receive_event(connection, deadline=deadline)
        if event["type"] == expected:
            return event
    raise LocalVoiceError("语音服务事件数量超过安全上限。")


def provider_synthesize_pcm(
    *,
    api_key: str,
    workspace_id: str,
    voice_id: str,
    text: str,
    websocket_factory: Callable[..., Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Synthesize one PCM response with the pinned Beijing realtime protocol."""

    normalized_workspace = workspace_id.strip().lower()
    if not WORKSPACE_PATTERN.fullmatch(normalized_workspace):
        raise LocalVoiceError("北京 Workspace 绑定无效。")
    if not api_key or any(character.isspace() for character in api_key):
        raise LocalVoiceError("百炼 API 凭据不可用。")
    if not VOICE_PATTERN.fullmatch(voice_id):
        raise LocalVoiceError("私有音色绑定无效。")
    if not text or "\x00" in text or len(text) > MAX_TEXT_CHARACTERS:
        raise LocalVoiceError("语音文本为空或超过长度上限。")
    if websocket_factory is None:
        from websockets.sync.client import connect

        websocket_factory = connect
    endpoint = f"{WEBSOCKET_ENDPOINT}?{urlencode({'model': MODEL})}"
    deadline = time.monotonic() + WHOLE_EXCHANGE_TIMEOUT_SECONDS
    pcm = bytearray()
    event_count = 0
    audio_done = False
    response_done = False
    usage_characters: int | None = None
    with websocket_factory(
        endpoint,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "X-DashScope-WorkSpace": normalized_workspace,
            "User-Agent": "Project-Snow-local-voice/1.0",
        },
        compression=None,
        open_timeout=15,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=MAX_EVENT_BYTES,
    ) as connection:
        created = _receive_until(connection, "session.created", deadline=deadline)
        created_session = _object(created.get("session"), label="创建会话")
        _expect(created_session.get("model"), MODEL, label="会话模型")
        session_id = _string(created_session.get("id"), label="会话 ID")
        _send_event(
            connection,
            "session.update",
            session={
                "voice": voice_id,
                "mode": MODE,
                "language_type": LANGUAGE_TYPE,
                "response_format": RESPONSE_FORMAT,
                "sample_rate": SAMPLE_RATE_HZ,
            },
        )
        updated = _receive_until(connection, "session.updated", deadline=deadline)
        updated_session = _object(updated.get("session"), label="更新会话")
        _expect(updated_session.get("id"), session_id, label="更新会话 ID")
        _expect(updated_session.get("model"), MODEL, label="更新会话模型")
        _expect(updated_session.get("voice"), voice_id, label="更新会话音色")
        _expect(updated_session.get("mode"), MODE, label="更新会话模式")
        _expect(
            str(updated_session.get("language_type") or "").casefold(),
            LANGUAGE_TYPE.casefold(),
            label="更新会话语言",
        )
        _expect(updated_session.get("response_format"), RESPONSE_FORMAT, label="更新会话格式")
        _expect(updated_session.get("sample_rate"), SAMPLE_RATE_HZ, label="更新会话采样率")
        _send_event(connection, "input_text_buffer.append", text=text)
        _send_event(connection, "input_text_buffer.commit")
        while not (audio_done and response_done):
            event_count += 1
            if event_count > MAX_EVENT_COUNT:
                raise LocalVoiceError("语音服务事件数量超过安全上限。")
            event = _receive_event(connection, deadline=deadline)
            event_type = event["type"]
            if event_type == "response.audio.delta":
                delta = event.get("delta")
                if not isinstance(delta, str) or len(delta) > MAX_AUDIO_BYTES * 2:
                    raise LocalVoiceError("语音服务返回了无效音频分片。")
                try:
                    chunk = base64.b64decode(delta, validate=True)
                except (binascii.Error, ValueError) as error:
                    raise LocalVoiceError("语音服务返回了损坏的音频分片。") from error
                if len(pcm) + len(chunk) > MAX_AUDIO_BYTES:
                    raise LocalVoiceError("语音服务音频超过安全上限。")
                pcm.extend(chunk)
            elif event_type == "response.audio.done":
                audio_done = True
            elif event_type == "response.done":
                response = _object(event.get("response"), label="完成响应")
                if response.get("status") not in ("completed", "done"):
                    raise LocalVoiceError("语音服务未成功完成响应。")
                if response.get("voice") is not None:
                    _expect(response.get("voice"), voice_id, label="完成响应音色")
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                raw_usage = usage.get("characters")
                if isinstance(raw_usage, int) and not isinstance(raw_usage, bool) and raw_usage >= 0:
                    usage_characters = raw_usage
                response_done = True
        if not pcm:
            raise LocalVoiceError("语音服务没有返回音频。")
        _send_event(connection, "session.finish")
        _receive_until(connection, "session.finished", deadline=deadline)
    return bytes(pcm), {
        "provider_usage_characters": usage_characters,
        "received_event_count": event_count,
    }


def pcm_to_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % (CHANNELS * SAMPLE_WIDTH_BYTES):
        raise LocalVoiceError("语音服务 PCM 数据为空或未按帧对齐。")
    output = io.BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(CHANNELS)
        destination.setsampwidth(SAMPLE_WIDTH_BYTES)
        destination.setframerate(SAMPLE_RATE_HZ)
        destination.writeframes(pcm)
    return output.getvalue()


class LocalVoiceRuntime:
    """Load private routes and synthesize without exposing Provider identifiers."""

    def __init__(
        self,
        profile_path: Path,
        *,
        api_key: str,
        provider: Callable[..., tuple[bytes, dict[str, Any]]] = provider_synthesize_pcm,
    ) -> None:
        document = _read_profile(Path(profile_path))
        profile_id, workspace_id, routes = _load_routes(document)
        self.profile_path = Path(profile_path).resolve()
        self.profile_id = profile_id
        self.workspace_id = workspace_id
        self.routes = routes
        self.api_key = str(api_key or "").strip()
        self.provider = provider

    @classmethod
    def from_data_root(
        cls,
        data_root: Path,
        *,
        api_key: str,
        explicit_profile_path: str = "",
        provider: Callable[..., tuple[bytes, dict[str, Any]]] = provider_synthesize_pcm,
    ) -> LocalVoiceRuntime:
        return cls(
            discover_profile_path(data_root, explicit_profile_path),
            api_key=api_key,
            provider=provider,
        )

    def supports(self, character_id: str) -> bool:
        return any(key[0] == character_id for key in self.routes)

    def route(self, character_id: str, text: str, *, style: str | None = None) -> VoiceRoute:
        selected_style = style or classify_style(text)
        if selected_style not in EXPECTED_STYLES:
            raise LocalVoiceError("请求的语态不在本地运行合同内。")
        route = self.routes.get((character_id, selected_style))
        if route is None:
            raise LocalVoiceError("角色没有本地语音运行路由。")
        if route.status == "paused":
            raise LocalVoiceSlotPaused(style=route.style, case_id=route.case_id)
        if route.provider_voice_id is None:
            raise LocalVoiceError("锁定语态缺少私有音色绑定。")
        return route

    def synthesize(self, character_id: str, text: str, *, style: str | None = None) -> dict[str, Any]:
        spoken = str(text or "").strip()
        if not spoken or "\x00" in spoken or len(spoken) > MAX_TEXT_CHARACTERS:
            raise LocalVoiceError("语音文本为空或超过长度上限。")
        route = self.route(character_id, spoken, style=style)
        if not self.api_key or any(character.isspace() for character in self.api_key):
            raise LocalVoiceError("百炼 API 凭据不可用。")
        pcm, provider_result = self.provider(
            api_key=self.api_key,
            workspace_id=self.workspace_id,
            voice_id=route.provider_voice_id,
            text=spoken,
        )
        wav = pcm_to_wav(pcm)
        usage = provider_result.get("provider_usage_characters")
        return {
            "status": "completed",
            "audio_bytes": wav,
            "content_type": "audio/wav",
            "filename": f"{route.character_slug}-{route.style}-reply.wav",
            "style": route.style,
            "case_id": route.case_id,
            "profile_id": self.profile_id,
            "model": {
                "provider": "qwen-vc-realtime",
                "model": MODEL,
                "region": REGION,
            },
            "provider_usage_characters": (
                usage if isinstance(usage, int) and not isinstance(usage, bool) and usage >= 0 else None
            ),
        }
