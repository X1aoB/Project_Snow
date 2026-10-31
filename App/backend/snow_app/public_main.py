"""Registration-free public API. It intentionally exposes no internal /api/v1 routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .async_store import AsyncPublicStore
from .arrival_ticket import ArrivalTicketExpired, verify_arrival_ticket
from .config import PublicSettings, Settings
from .frontend_identity import read_frontend_identity
from .generation_stream import GenerationObserver, observe_generation
from .model_capabilities import immersive_model_policy, public_model_profiles
from .public_logging import configure_public_logger
from .public_statistics import install_statistics_config
from .proactive_message import authority_change, install_proactive_routes, proactive_config
from .mvp_service import MVPProviderError, _normalize_expression_state, _normalize_stage_motion
from .public_contracts import (
    ByokSessionRequest,
    ChatRequest,
    DialogueV2ChatRequest,
    FeedbackRequest,
    ModelDiscoveryRequest,
    PresenceArrivalRequest,
    PresenceArrivalV2Request,
    PresenceResolveRequest,
    PresenceTransitionRequest,
    SceneActionRequest,
    SessionMemoryCandidate,
    SummarizeRequest,
)
from .public_delivery import DeliveryJournal, latency_metrics
from .public_providers import (
    PROVIDERS,
    ProviderHTTPPool,
    ProviderRequestError,
    discover_models,
    provider_spec,
    relay_provider_spec,
)
from .relay_url import RelayUrlError, normalise_relay_base_url
from .public_security import (
    BYOK_RETENTION_HOURS,
    BYOK_RETENTION_NOTICE_VERSION,
    PublicSecurityError,
    daily_ip_fingerprint,
    encrypt_qq,
    input_safety_category,
    is_valid_anonymous_id,
    issue_byok_credential,
    new_anonymous_id,
    open_byok_credential,
    redact_sensitive_text,
    subject_hash,
    verify_turnstile,
)
from .public_service import (
    CharacterUnavailable,
    GenerationBusy,
    PublicChatService,
    _normalize_public_immersive_punctuation,
)
from .session_context import validate_session_context_transfer
from .public_store import (
    DuplicateFeedback,
    PublicStore,
    PublicStoreUnavailable,
    RateLimitExceeded,
)
from .public_subscription import (
    EFFORT as SUBSCRIPTION_EFFORT,
    MODEL as SUBSCRIPTION_MODEL,
    PROTOCOL_VERSION as SUBSCRIPTION_PROTOCOL_VERSION,
    PROVIDER as SUBSCRIPTION_PROVIDER,
    SubscriptionBroker,
    SubscriptionError,
    SubscriptionSyncClient,
    install_subscription_routes,
)

# Minimal production images do not necessarily ship a mime.types entry for
# WebP. StaticFiles inherits Python's process-wide mapping, so register the
# public format before any FileResponse is constructed.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("font/woff2", ".woff2")

ANONYMOUS_COOKIE = "__Host-snow_anon"
DEVELOPMENT_ANONYMOUS_COOKIE = "snow_anon_dev"
MAX_BODY_BYTES = 64 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10
PUBLIC_DATABASE_RETRY_AFTER_SECONDS = 2
# Per-client cap on edge-relayed health probes, far above any uptime monitor.
HEALTH_PROBE_LIMIT = 60
HEALTH_PROBE_WINDOW_SECONDS = 60
HEALTH_PROBE_MAX_CLIENTS = 4096
LOGGER = configure_public_logger()

_ERROR_STAGES = frozenset(
    {
        "content_validation",
        "state_validation",
        "character_data",
        "generation_queue",
        "retrieval",
        "provider",
        "generation_validation",
        "idempotency_store",
        "publication_store",
        "transport",
        "unknown",
    }
)
_ERROR_STAGE_BY_CODE = {
    "invalid_request": "content_validation",
    "sticker_unavailable": "content_validation",
    "invalid_movement_location": "content_validation",
    "invalid_presence_request": "content_validation",
    "request_too_large": "content_validation",
    "request_read_timeout": "transport",
    "request_timeout": "transport",
    "request_failed": "transport",
    "stream_disconnected": "transport",
    "service_draining": "generation_queue",
    "subject_generation_busy": "generation_queue",
    "generation_queue_full": "generation_queue",
    "generation_queue_timeout": "generation_queue",
    # A catch-all generation failure has no more precise public stage. Keep
    # the internal exception detail private and expose the controlled
    # ``unknown`` stage to clients.
    "generation_interrupted": "unknown",
    "generation_failed": "unknown",
    "generation_partial": "generation_validation",
    "provider_not_enabled": "provider",
    "provider_credential_rejected": "provider",
    "credential_revoked": "provider",
    "provider_model_discovery_failed": "provider",
    "provider_network_error": "provider",
    "provider_timeout": "provider",
    "provider_rate_limited": "provider",
    "provider_request_failed": "provider",
    "upstream_invalid_response": "generation_validation",
    "role_guard_rejected": "generation_validation",
    "public_database_unavailable": "idempotency_store",
    "request_id_conflict": "idempotency_store",
    "request_in_progress": "idempotency_store",
    "request_result_unavailable": "idempotency_store",
    "state_invalid": "state_validation",
    "state_subject_mismatch": "state_validation",
    "invalid_presence_transition": "state_validation",
    "session_context_invalid": "state_validation",
    "session_context_subject_mismatch": "state_validation",
    "session_context_conflict": "state_validation",
    "invalid_dialogue_context": "content_validation",
    "invalid_scene_action": "state_validation",
    "character_unavailable": "character_data",
}
_NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "sticker_unavailable",
        "invalid_movement_location",
        "invalid_presence_request",
        "request_too_large",
        "request_id_conflict",
        "request_result_unavailable",
        "session_context_invalid",
        "session_context_subject_mismatch",
        "session_context_conflict",
        "invalid_dialogue_context",
        "invalid_scene_action",
        "generation_interrupted",
        "generation_partial",
        "provider_not_enabled",
        "provider_credential_rejected",
        "credential_revoked",
        "character_unavailable",
        "state_invalid",
        "state_subject_mismatch",
        "invalid_presence_transition",
        "role_guard_rejected",
        "upstream_invalid_response",
        "turnstile_required",
        "turnstile_expired",
        "turnstile_timeout",
        "turnstile_unavailable",
    }
)


def _safe_request_id(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return ""


def _request_id_from_request(request: Request) -> str:
    state_value = getattr(getattr(request, "state", None), "public_request_id", "")
    if state_value:
        return _safe_request_id(state_value)
    raw_body = getattr(request, "_body", b"")
    if not raw_body:
        return ""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    request_id = payload.get("request_id") or payload.get("arrival_id")
    if not request_id and request.url.path == "/public/v2/chat/stream" and isinstance(payload.get("chat"), dict):
        request_id = payload["chat"].get("request_id")
    return _safe_request_id(request_id)


def _error_stage(code: str, explicit: Any = "") -> str:
    candidate = str(explicit or "").strip()
    if candidate in _ERROR_STAGES:
        return candidate
    return _ERROR_STAGE_BY_CODE.get(code, "unknown")


def _error_retryable(code: str, http_status: int, explicit: Any = None) -> bool:
    if isinstance(explicit, bool):
        return explicit
    if code in _NON_RETRYABLE_ERROR_CODES:
        return False
    return http_status >= 500 or http_status in {408, 409, 429}


def _error_detail(
    code: str,
    http_status: int,
    message: str | None = None,
    *,
    request_id: str = "",
    stage: str = "",
    retryable: bool | None = None,
    retry_after_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "code": code,
        "message": message or code,
        "retryable": _error_retryable(code, http_status, retryable),
        "stage": _error_stage(code, stage),
    }
    safe_id = _safe_request_id(request_id)
    if safe_id:
        detail["request_id"] = safe_id
    if isinstance(retry_after_seconds, int) and not isinstance(retry_after_seconds, bool) and retry_after_seconds > 0:
        detail["retry_after_seconds"] = min(retry_after_seconds, 300)
    if extra:
        detail.update(extra)
    return detail


def _error_response(
    request: Request,
    code: str,
    http_status: int,
    *,
    message: str | None = None,
    headers: dict[str, str] | None = None,
    stage: str = "",
    retryable: bool | None = None,
    retry_after_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={
            "detail": _error_detail(
                code,
                http_status,
                message,
                request_id=_request_id_from_request(request),
                stage=stage,
                retryable=retryable,
                retry_after_seconds=retry_after_seconds,
                extra=extra,
            )
        },
        headers=headers,
    )


def _rate_limit_response(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """A site frequency cap (not the user's model quota), retryable when its window ends."""
    retry_after = 60
    extra: dict[str, Any] = {"scope": exc.scope, "limit": exc.limit}
    if exc.reset_at is not None:
        retry_after = max(1, int((exc.reset_at - datetime.now(exc.reset_at.tzinfo)).total_seconds()) + 1)
        extra["reset_at"] = exc.reset_at.isoformat()
    return _error_response(
        request,
        "rate_limit_exceeded",
        429,
        headers={"Retry-After": str(retry_after)},
        retry_after_seconds=retry_after,
        extra=extra,
    )

def _anonymous_cookie_name(settings: PublicSettings) -> str:
    return DEVELOPMENT_ANONYMOUS_COOKIE if settings.allow_insecure_dev else ANONYMOUS_COOKIE


def _allowed_hosts(settings: PublicSettings) -> frozenset[str]:
    hosts = {
        str(urlsplit(origin).hostname or "").casefold()
        for origin in settings.allowed_origins
    }
    # Candidate health and acceptance are intentionally loopback-only.
    hosts.update({"127.0.0.1", "localhost", "::1"})
    return frozenset(host for host in hosts if host)


def _edge_client_ip(request: Request) -> str:
    """The visitor address Cloudflare reported, or "" when the request bypassed the edge."""
    if not request.app.state.public_settings.trust_proxy_headers:
        return ""
    return str(request.headers.get("CF-Connecting-IP") or "").strip()[:64]


def _client_ip(request: Request) -> str:
    return _edge_client_ip(request) or str(request.client.host if request.client else "unknown")[:64]


class HealthProbeLimiter:
    """Per-client fixed-window cap on health probes relayed by the edge.

    State stays in process memory, so a probe flood never reaches PostgreSQL.
    Clients are keyed by a per-process keyed digest rather than the raw
    address, and the least recently seen client is forgotten once full.
    """

    def __init__(
        self,
        limit: int = HEALTH_PROBE_LIMIT,
        window_seconds: float = HEALTH_PROBE_WINDOW_SECONDS,
        max_clients: int = HEALTH_PROBE_MAX_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._windows: OrderedDict[bytes, tuple[float, int]] = OrderedDict()

    def retry_after(self, client_ip: str) -> float | None:
        """Count one probe; past the cap, return the seconds left in the client's window."""
        client = hashlib.blake2b(client_ip.encode(), key=self._key, digest_size=16).digest()
        now = self._clock()
        started, count = self._windows.pop(client, (now, 0))
        if now - started >= self.window_seconds:
            started, count = now, 0
        # Re-inserting keeps the table ordered from least to most recently seen.
        self._windows[client] = (started, count + 1)
        if len(self._windows) > self.max_clients:
            self._windows.popitem(last=False)
        return None if count < self.limit else started + self.window_seconds - now


def _error(
    code: str,
    http_status: int,
    message: str | None = None,
    *,
    request_id: str = "",
    stage: str = "",
    retryable: bool | None = None,
    retry_after_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail=_error_detail(
            code,
            http_status,
            message,
            request_id=request_id,
            stage=stage,
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
            extra=extra,
        ),
        headers=headers,
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _public_scene_state_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    projected: dict[str, Any] = {}
    for key, limit in (
        ("analyst_location", 120),
        ("character_location", 120),
        ("character_activity", 240),
        ("visual_key", 120),
        ("state_scope", 40),
    ):
        if key in value:
            raw = value.get(key)
            projected[key] = (
                None if raw is None else redact_sensitive_text(str(raw), limit)
            )
    if "co_located" in value:
        projected["co_located"] = bool(value.get("co_located"))
    return projected


def _public_state_event_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    projected: dict[str, Any] = {}
    for key, limit in (
        ("event_id", 160),
        ("event_type", 40),
        ("character_id", 32),
        ("communication_channel", 16),
        ("location", 120),
        ("arrival_decision", 16),
        ("location_id", 64),
        ("activity_id", 64),
        ("target_character_id", 32),
    ):
        if key in value:
            raw = value.get(key)
            projected[key] = (
                None if raw is None else redact_sensitive_text(str(raw), limit)
            )
    return projected


def _public_pending_rendezvous_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    projected: dict[str, Any] = {}
    for key, limit in (
        ("rendezvous_id", 160),
        ("character_id", 32),
        ("location_id", 64),
        ("location_name", 120),
        ("activity_id", 64),
        ("waiting_activity", 240),
        ("joined_activity", 240),
        ("created_at", 64),
        ("schedule_date", 16),
    ):
        if key in value:
            raw = value.get(key)
            projected[key] = (
                None if raw is None else redact_sensitive_text(str(raw), limit)
            )
    return projected


def _public_movement_status_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "none"}
    projected: dict[str, Any] = {}
    for key, limit in (
        ("status", 40),
        ("location_id", 64),
        ("location_name", 120),
        ("display_name", 120),
        ("activity_id", 64),
        ("target_character_id", 32),
        ("character_id", 32),
        ("schedule_date", 16),
    ):
        if key in value:
            raw = value.get(key)
            projected[key] = (
                None if raw is None else redact_sensitive_text(str(raw), limit)
            )
    raw_revision = value.get("schedule_revision")
    if isinstance(raw_revision, int) and not isinstance(raw_revision, bool):
        projected["schedule_revision"] = max(0, raw_revision)
    pending = _public_pending_rendezvous_payload(value.get("pending_rendezvous"))
    if pending is not None:
        projected["pending_rendezvous"] = pending
    if not projected.get("status"):
        projected["status"] = "none"
    return projected


def _public_content_blocks_payload(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projected_blocks: list[dict[str, Any]] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "").casefold()
        if block_type not in {"message", "speech", "action", "sticker"}:
            continue
        block: dict[str, Any] = {"type": block_type}
        if block_type == "sticker":
            for key, limit in (
                ("asset_id", 64),
                ("caption", 120),
                ("src", 500),
                ("thumbnail_src", 500),
                ("display_src", 500),
                ("display_mime_type", 80),
            ):
                if key in item:
                    block[key] = redact_sensitive_text(str(item.get(key) or ""), limit)
            block["display_animated"] = bool(item.get("display_animated"))
            block["animated"] = bool(item.get("animated"))
        else:
            text_value = _normalize_public_immersive_punctuation(
                redact_sensitive_text(str(item.get("text") or ""), 1200)
            )
            if not text_value:
                continue
            block["text"] = text_value
        projected_blocks.append(block)
    return projected_blocks


def _request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _model_request_body(payload: ChatRequest | PresenceArrivalRequest | SummarizeRequest) -> dict[str, Any]:
    body = payload.model_dump(mode="json", exclude={"credential"})
    # Preserve hashes of request snapshots created before effort selection was
    # introduced. Explicit efforts remain part of the idempotency contract.
    if body.get("reasoning_effort") is None:
        body.pop("reasoning_effort", None)
    return body


def _dialogue_v2_request_body(payload: DialogueV2ChatRequest) -> dict[str, Any]:
    body = payload.model_dump(mode="json")
    body["chat"] = _model_request_body(payload.chat)
    return body


def _dialogue_v2_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Project only the bounded final metadata supplied by the v2 adapter."""
    metadata: dict[str, Any] = {}
    if isinstance(result.get("evidence_sources"), list):
        metadata["evidence_sources"] = [
            {key: str(item.get(key) or "")[:160] for key in ("document_id", "title", "source_type")}
            for item in result["evidence_sources"][:8] if isinstance(item, dict)
        ]
    package = result.get("session_context_package")
    if package is not None:
        if not isinstance(package, str) or len(package) > 8192:
            raise ValueError("invalid session context response")
        metadata["session_context_package"] = package
    if "session_memories" in result:
        candidates = result["session_memories"]
        if not isinstance(candidates, list) or len(candidates) > 12:
            raise ValueError("invalid session memory response")
        metadata["session_memories"] = [
            SessionMemoryCandidate.model_validate(item).model_dump(mode="json") for item in candidates
        ]
    if isinstance(result.get("scene_action"), dict):
        action = result["scene_action"]
        metadata["scene_action"] = {
            key: value for key, value in action.items()
            if key in {
                "status", "type", "action", "action_id", "scene_id", "scene_epoch",
                "location_id", "location_name", "activity_id", "rendezvous_id", "communication_channel",
            } and ((isinstance(value, str) and len(value) <= 160)
                   or (key == "scene_epoch" and isinstance(value, int) and not isinstance(value, bool) and value >= 0))
        }
    return metadata


def _feedback_blocks(blocks: list[Any], limit: int) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    remaining = limit
    for block in blocks[:8]:
        block_type = str(getattr(block, "type", "") or "")
        if block_type == "sticker":
            asset_id = str(getattr(block, "asset_id", "") or "")
            caption = redact_sensitive_text(str(getattr(block, "caption", "") or ""), 120)
            if asset_id:
                normalized.append({"type": "sticker", "asset_id": asset_id, "caption": caption})
            continue
        text_value = redact_sensitive_text(str(getattr(block, "text", "") or ""), remaining)
        if not block_type or not text_value:
            continue
        normalized.append({"type": block_type, "text": text_value})
        remaining -= len(text_value)
        if remaining <= 0:
            break
    return normalized


def _spoken_feedback_text(blocks: list[dict[str, str]], fallback: str, limit: int) -> str:
    spoken = "\n".join(
        block["text"]
        for block in blocks
        if block["type"] in {"message", "speech"}
    ).strip()
    return redact_sensitive_text(spoken or fallback, limit)


def _public_security_disposition(exc: PublicSecurityError) -> tuple[str, int]:
    message = str(exc)
    if message == "Public state belongs to another anonymous session":
        return "state_subject_mismatch", 409
    if message.startswith("State package") or message.startswith("Public state schema"):
        return "state_invalid", 422
    if message.startswith("PUBLIC_"):
        return "security_configuration_unavailable", 503
    if message == "Session context belongs to another anonymous session":
        return "session_context_subject_mismatch", 409
    if message == "Session context scope does not match request":
        return "session_context_conflict", 409
    if message.startswith("Session context"):
        return "session_context_invalid", 422
    if message == "BYOK credential has been revoked":
        return "credential_revoked", 401
    return "credential_invalid", 401


def create_app(
    public_settings: PublicSettings | None = None,
    internal_settings: Settings | None = None,
    store: PublicStore | None = None,
    chat_service: PublicChatService | None = None,
    dialogue_v2_service: Any | None = None,
) -> FastAPI:
    public_settings = public_settings or PublicSettings.from_environment()
    subscription_broker = SubscriptionBroker(public_settings.subscription_enabled)
    enabled_providers = tuple(provider for provider in public_settings.enabled_providers
                              if provider != SUBSCRIPTION_PROVIDER)
    if subscription_broker.enabled:
        enabled_providers += (SUBSCRIPTION_PROVIDER,)
    public_settings = replace(public_settings, enabled_providers=enabled_providers,
                              subscription_enabled=subscription_broker.enabled)
    internal_settings = internal_settings or Settings.from_environment()
    image_revision = os.getenv("APP_REVISION", "").strip()
    revision = os.getenv("APP_REVISION", os.getenv("GIT_SHA", "")).strip()
    build_time = os.getenv("APP_BUILD_TIME", "").strip()
    frontend_identity = read_frontend_identity(required=bool(image_revision))
    store = store or PublicStore(public_settings.database_url)
    provider_http = ProviderHTTPPool(subscription_broker)
    async_store = AsyncPublicStore(store)
    chat_service = chat_service or PublicChatService(
        internal_settings,
        public_settings,
        provider_client=provider_http,
    )
    if chat_service is not None and getattr(chat_service, "provider_client", None) is None:
        chat_service.provider_client = provider_http
    if not isinstance(chat_service.mvp._model_http_client, SubscriptionSyncClient):
        chat_service.mvp._model_http_client = SubscriptionSyncClient(
            subscription_broker, chat_service.mvp._model_http_client,
        )
    # Verify immutable packages before any URL is mounted.  Subsequent calls
    # return the in-memory snapshot and never re-hash runtime media.
    media_startup_status = chat_service.media.verify(force=True)
    sticker_startup_status = chat_service.stickers.verify(force=True)
    knowledge_review_path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "public_knowledge"
        / "data_license_review.json"
    )
    knowledge_review: dict[str, Any] | None = None
    try:
        candidate_review = json.loads(knowledge_review_path.read_text(encoding="utf-8"))
        candidate_sources = candidate_review.get("sources")
        if (
            candidate_review.get("schema_version") == "project-snow-data-license-review-1"
            and candidate_review.get("review_status")
            == "verified_against_bwiki_source_declaration"
            and candidate_review.get("license") == "CC BY-NC-SA 4.0"
            and isinstance(candidate_sources, list)
            and int(candidate_review.get("source_count") or 0) == len(candidate_sources)
            and candidate_sources
            and isinstance(candidate_review.get("modifications"), list)
            and candidate_review.get("modifications")
        ):
            knowledge_review = candidate_review
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        knowledge_review = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if store.engine is not None and public_settings.auto_create_schema:
            await async_store.call("create_schema")
            await async_store.call("cleanup")
        async def renew_owned_leases() -> None:
            while True:
                await asyncio.sleep(10)
                try:
                    live_requests = tuple(
                        request_id for request_id, task in active_request_leases.items() if not task.done()
                    )
                    await async_store.call("renew_leases", async_store.owner_token, live_requests)
                except PublicStoreUnavailable:
                    LOGGER.warning("public_lease_renewal_unavailable")
        renewal = asyncio.create_task(renew_owned_leases(), name="public-request-leases")
        try:
            yield
        finally:
            app.state.draining = True
            pending = [task for task in chat_jobs.values() if not task.done()]
            if pending:
                # Threads performing model calls cannot be canceled safely. Give
                # accepted jobs time to durably record a result before closing.
                _, unfinished = await asyncio.wait(pending, timeout=300)
                if unfinished:
                    LOGGER.warning("public_shutdown_unfinished", extra={"count": len(unfinished)})
                    try:
                        await async_store.call("interrupt_owned_requests", async_store.owner_token)
                    except PublicStoreUnavailable:
                        # Expiring ownership also fences these jobs after a DB
                        # outage; cleanup must still proceed while it is down.
                        LOGGER.warning("public_shutdown_store_unavailable")
                    for task in unfinished:
                        task.cancel()
                    await asyncio.gather(*unfinished, return_exceptions=True)
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            try:
                subscription_broker.close()
                await asyncio.to_thread(chat_service.close)
            finally:
                try:
                    await provider_http.close()
                finally:
                    await async_store.close()

    app = FastAPI(
        title="Project Snow Public Immersive API",
        version=public_settings.app_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.public_settings = public_settings
    app.state.public_store = store
    app.state.chat_service = chat_service
    app.state.dialogue_v2_service = dialogue_v2_service
    app.state.provider_http = provider_http
    app.state.subscription_broker = subscription_broker
    app.state.async_store = async_store
    app.state.draining = False
    install_subscription_routes(app, public_settings, subscription_broker, async_store)
    chat_jobs: dict[str, asyncio.Task[dict[str, Any]]] = {}
    delivery_journals: dict[str, DeliveryJournal] = {}
    active_request_leases: dict[str, asyncio.Task[Any]] = {}
    active_subject_requests: dict[str, str] = {}
    active_subject_tasks: dict[str, asyncio.Task[Any]] = {}
    background_summaries: dict[str, dict[str, Any]] = {}
    active_subject_lock = asyncio.Lock()
    app.state.chat_jobs = chat_jobs
    app.state.active_request_leases = active_request_leases
    app.state.active_subject_requests = active_subject_requests

    async def open_request_credential(request: Request, token: str, expected_provider: str):
        revoked_before = await async_store.call(
            "credential_revoked_before", request.state.subject_hash
        )
        return open_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            token=token,
            expected_provider=expected_provider,
            revoked_before=revoked_before,
        )

    def relay_aware_spec(spec, claims):
        """Return the spec a route must use for the opened credential.

        For a relay credential the real base URL is the one sealed into the
        claim (never the request body); official providers keep their fixed
        spec. Every model-calling route runs its opened claims through this so
        discovery, chat, scene, arrival, summary and proactive all route to the
        sealed relay host identically.
        """
        if spec.provider_id == "relay":
            sealed = str((claims or {}).get("relay_base") or "")
            if not sealed:
                raise PublicSecurityError("Relay credential is missing its sealed base URL")
            return relay_provider_spec(sealed)
        return spec

    async def claim_owned_request(request_id: str, subject: str, request_hash: str):
        result = await async_store.call("claim_request", request_id, subject, request_hash)
        if result[0] == "claimed":
            task = asyncio.current_task()
            assert task is not None
            active_request_leases[request_id] = task
            if active_subject_requests.get(subject) == request_id:
                # A pre-generation release may finish in the DB before its
                # old coroutine resumes. A same-UUID reclaim owns a new task.
                active_subject_tasks[subject] = task

            def forget_lease(completed):
                if active_request_leases.get(request_id) is completed:
                    active_request_leases.pop(request_id, None)
                if (
                    active_subject_requests.get(subject) == request_id
                    and active_subject_tasks.get(subject) is completed
                ):
                    active_subject_requests.pop(subject, None)
                    active_subject_tasks.pop(subject, None)

            # Covers cancellation between admission steps as well as ordinary
            # completion. Chat transfers ownership to its detached durable job.
            task.add_done_callback(forget_lease)
        return result

    async def complete_owned_request(request_id: str, result: dict[str, Any], *, rendezvous_change=None) -> None:
        task = asyncio.current_task()
        try:
            if rendezvous_change:
                await async_store.call("complete_request", request_id, result, rendezvous_change=rendezvous_change)
            else:
                await async_store.call("complete_request", request_id, result)
        finally:
            # A failed/canceled write has an uncertain outcome. Do not keep its
            # lease alive after the task stops; recovery will fence it safely.
            if active_request_leases.get(request_id) is task:
                active_request_leases.pop(request_id, None)

    async def release_owned_request(request_id: str) -> None:
        task = asyncio.current_task()
        try:
            await async_store.call("release_request", request_id)
        finally:
            if active_request_leases.get(request_id) is task:
                active_request_leases.pop(request_id, None)

    async def acquire_subject_generation(subject: str, operation_id: str) -> bool:
        """Reserve the subject unless this is a reconnect to the same operation."""

        if app.state.draining:
            raise _error(
                "service_draining",
                503,
                request_id=operation_id,
                retry_after_seconds=5,
                headers={"Retry-After": "5"},
            )
        async with active_subject_lock:
            owner = active_subject_requests.get(subject)
            if owner and owner != operation_id:
                summary = background_summaries.get(subject)
                if summary and summary["operation_id"] == owner and not operation_id.startswith("chat-summary:"):
                    # Foreground actions take priority even if the old browser
                    # disconnect has not yet reached the summary route.
                    summary["cancel"].set()
                    if summary["task"] is not None:
                        summary["task"].cancel()
                else:
                    raise _error(
                        "subject_generation_busy",
                        429,
                        request_id=operation_id,
                        retry_after_seconds=2,
                        headers={"Retry-After": "2"},
                    )
            if owner == operation_id:
                return False
            active_subject_requests[subject] = operation_id
            task = asyncio.current_task()
            assert task is not None
            active_subject_tasks[subject] = task

            def forget_subject(completed):
                if (
                    active_subject_requests.get(subject) == operation_id
                    and active_subject_tasks.get(subject) is completed
                ):
                    active_subject_requests.pop(subject, None)
                    active_subject_tasks.pop(subject, None)

            # Admission can be canceled while a shielded DB claim is still
            # finishing. The later lease expires, but the subject must unlock.
            task.add_done_callback(forget_subject)
            return True

    async def release_subject_generation(subject: str, operation_id: str) -> None:
        async with active_subject_lock:
            if (
                active_subject_requests.get(subject) == operation_id
                and active_subject_tasks.get(subject) is asyncio.current_task()
            ):
                active_subject_requests.pop(subject, None)
                active_subject_tasks.pop(subject, None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(public_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Accept", "X-Snow-Stream-Version", "X-Snow-Recovery-Only"],
    )
    health_probes = HealthProbeLimiter()

    @app.middleware("http")
    async def public_boundary(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            return _error_response(request, "route_not_public", 404)
        request_host = str(request.url.hostname or "").casefold().rstrip(".")
        if request_host not in _allowed_hosts(public_settings):
            return _error_response(request, "host_rejected", 400)
        if path.startswith("/public/v1/health/"):
            # Only edge-relayed probes count. Container, release-script and
            # monitor-internal probes reach the API without an edge client.
            edge_ip = _edge_client_ip(request)
            wait = health_probes.retry_after(edge_ip) if edge_ip else None
            if wait is not None:
                return _rate_limit_response(request, RateLimitExceeded(
                    "health_ip_minute", health_probes.limit, datetime.now(UTC) + timedelta(seconds=wait),
                ))
        cookie_name = _anonymous_cookie_name(public_settings)
        anonymous_id = str(request.cookies.get(cookie_name) or "").strip()
        new_cookie = not is_valid_anonymous_id(anonymous_id)
        if new_cookie:
            anonymous_id = new_anonymous_id()
        request.state.anonymous_id = anonymous_id
        request.state.subject_hash = subject_hash(anonymous_id)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = str(request.headers.get("Origin") or "").rstrip("/")
            if origin not in public_settings.allowed_origins:
                return _error_response(request, "origin_rejected", 403)
            content_type = str(request.headers.get("Content-Type") or "").split(";", 1)[0].casefold()
            if content_type != "application/json":
                return _error_response(request, "json_required", 415)
            try:
                content_length = int(request.headers.get("Content-Length") or 0)
            except ValueError:
                content_length = MAX_BODY_BYTES + 1
            if content_length > MAX_BODY_BYTES:
                return _error_response(request, "request_too_large", 413)
            chunks: list[bytes] = []
            received = 0
            try:
                async with asyncio.timeout(REQUEST_BODY_TIMEOUT_SECONDS):
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > MAX_BODY_BYTES:
                            return _error_response(request, "request_too_large", 413)
                        chunks.append(chunk)
            except TimeoutError:
                return _error_response(request, "request_read_timeout", 408)
            # Starlette dependencies may read the body after this middleware.
            # Cache only the bounded content so they do not consume the stream.
            request._body = b"".join(chunks)  # type: ignore[attr-defined]
            ip_subject = daily_ip_fingerprint(public_settings, _client_ip(request))
            if path in {"/public/v1/subscription/connector/poll", "/public/v1/subscription/connector/result"}:
                try:
                    body = json.loads(request._body)
                    token = body.get("connector_token") if isinstance(body, dict) else None
                    if not isinstance(token, str) or len(token) != 43:
                        raise SubscriptionError("credential_invalid", 401)
                    connector_hash = subscription_broker.authenticate(token)
                except (ValueError, SubscriptionError) as exc:
                    code = exc.code if isinstance(exc, SubscriptionError) else "invalid_request"
                    status = exc.status_code if isinstance(exc, SubscriptionError) else 422
                    return JSONResponse(status_code=status, content={"detail": {"code": code}})
                # Authenticate before granting the heartbeat allowance. A personal
                # connector never drains the website's ordinary IP/chat buckets.
                # Keep the namespaced identity within PostgreSQL's VARCHAR(64)
                # contract, independently of browser/IP rate-limit subjects.
                ip_subject = hashlib.sha256(("subscription-connector:" + connector_hash).encode()).hexdigest()
                ip_limits = [("subscription_connector_hour", "hour", 1800),
                             ("subscription_connector_day", "day", 43200)]
            elif path.startswith("/public/v1/subscription/"):
                ip_limits = [("subscription_setup_ip_hour", "hour", 60),
                             ("subscription_setup_ip_day", "day", 300)]
            elif path == "/public/v1/feedback":
                ip_limits = [("feedback_ip_hour", "hour", 20), ("feedback_ip_day", "day", 50)]
            elif path.startswith("/public/v1/byok/"):
                ip_limits = [("byok_ip_hour", "hour", 30), ("byok_ip_day", "day", 100)]
            else:
                ip_limits = [("write_ip_hour", "hour", 600), ("write_ip_day", "day", 3000)]
            try:
                await async_store.call("consume_limits", ip_subject, ip_limits)
            except PublicStoreUnavailable:
                return _error_response(
                    request,
                    "public_database_unavailable",
                    503,
                    headers={"Retry-After": str(PUBLIC_DATABASE_RETRY_AFTER_SECONDS)},
                    retry_after_seconds=PUBLIC_DATABASE_RETRY_AFTER_SECONDS,
                )
            except RateLimitExceeded as exc:
                return _rate_limit_response(request, exc)
        response = await call_next(request)
        if path.startswith(("/public/v1/subscription/", "/public/v1/byok/")):
            response.headers["Cache-Control"] = "no-store"
        if new_cookie and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            response.set_cookie(
                cookie_name,
                anonymous_id,
                max_age=30 * 24 * 60 * 60,
                httponly=True,
                secure=not public_settings.allow_insecure_dev,
                samesite="lax",
                path="/",
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if not public_settings.allow_insecure_dev:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        raw_detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = str(raw_detail.get("code") or "request_failed")
        raw_request_id = str(raw_detail.get("request_id") or "")
        request_id = _safe_request_id(raw_request_id) or _request_id_from_request(request)
        extras = {
            key: value
            for key, value in raw_detail.items()
            if key not in {"code", "message", "retryable", "stage", "request_id", "retry_after_seconds"}
        }
        retry_after = raw_detail.get("retry_after_seconds")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": _error_detail(
                    code,
                    exc.status_code,
                    str(raw_detail.get("message") or code),
                    request_id=request_id,
                    stage=str(raw_detail.get("stage") or ""),
                    retryable=raw_detail.get("retryable"),
                    retry_after_seconds=retry_after if isinstance(retry_after, int) else None,
                    extra=extras,
                )
            },
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_public_error(request: Request, _exc: Exception):
        # Route-level failures that happen before the SSE stream is created
        # still need the same safe reconciliation envelope. Do not serialize
        # exception text: it may contain provider or user supplied data.
        LOGGER.error(
            "public_unhandled_exception",
            extra={"path": str(request.url.path)[:160]},
        )
        return _error_response(request, "request_failed", 503, stage="unknown")

    @app.exception_handler(PublicStoreUnavailable)
    async def database_unavailable(request: Request, _exc: PublicStoreUnavailable):
        return _error_response(
            request,
            "public_database_unavailable",
            503,
            headers={"Retry-After": str(PUBLIC_DATABASE_RETRY_AFTER_SECONDS)},
            retry_after_seconds=PUBLIC_DATABASE_RETRY_AFTER_SECONDS,
        )

    @app.get("/public/v1/build-info")
    def build_info() -> dict[str, Any]:
        parsed_build_time = build_time
        try:
            if (
                len(build_time) > 64
                or datetime.fromisoformat(build_time.replace("Z", "+00:00")).tzinfo is None
            ):
                parsed_build_time = ""
        except ValueError:
            parsed_build_time = ""
        return {
            "app_version": public_settings.app_version,
            "data_version": public_settings.data_version,
            "revision": (
                revision
                if 7 <= len(revision) <= 64 and all(c in "0123456789abcdef" for c in revision.lower())
                else None
            ),
            "build_time": parsed_build_time or None,
            "frontend": frontend_identity.public_value() if frontend_identity else None,
            "api_schema": "public-v1",
            "state_schema": "public-state-2",
            "generation_limits": {
                "active": 4, "queued": 8,
                "max_provider_calls": public_settings.max_provider_calls_per_action,
            },
        }

    @app.get("/public/v1/config")
    def config() -> dict[str, Any]:
        sticker_status = chat_service.stickers.verify()
        enabled = []
        for provider_id in public_settings.enabled_providers:
            spec = PROVIDERS.get(provider_id)
            if spec:
                enabled.append(
                    {
                        "provider_id": spec.provider_id,
                        "display_name": spec.display_name,
                        "documentation_url": spec.documentation_url,
                        "privacy_url": spec.privacy_url,
                        "console_url": spec.console_url,
                        "kind": spec.kind,
                        "caveat": spec.caveat,
                    }
                )
        return {
            "app_version": public_settings.app_version,
            "data_version": public_settings.data_version,
            "language": "zh-CN",
            "features": {
                "chat_latency_v2": public_settings.chat_latency_v2,
                "verified_block_stream": public_settings.verified_block_stream,
                "verified_block_replay": True,
                "motion_v2": public_settings.motion_v2,
                "dialogue_domains_v2": public_settings.dialogue_domains_v2,
                "scene_actions_v2": public_settings.scene_actions_v2,
                "session_memory_v2": public_settings.session_memory_v2,
                "ui_snow_adv": public_settings.ui_snow_adv,
            },
            "capabilities": {
                "dialogue_protocol": "public-dialogue-v2",
                "scene_action_protocol": "public-scene-action-v2",
                "session_context_schema": "public-session-context-1",
                "recovery_only": True,
            },
            "providers": enabled,
            "model_profiles": public_model_profiles(),
            "subscription": {"enabled": subscription_broker.enabled, "model": SUBSCRIPTION_MODEL,
                             "effort": SUBSCRIPTION_EFFORT, "protocol_version": SUBSCRIPTION_PROTOCOL_VERSION},
            "proactive_messages": proactive_config(public_settings),
            "turnstile_site_key": public_settings.turnstile_site_key,
            "limits": {
                "input_characters": 2000,
                "output_characters": 1200,
                "chat_hour": 120,
                "chat_day": 600,
                "feedback_hour": 10,
                "feedback_day": 30,
                "history_rounds_per_request": 12,
            },
            "history_policy": "browser_indexeddb_plaintext",
            "history_schema": "indexeddb-v4",
            "state_schema": "public-state-2",
            "media_version": public_settings.media_version,
            "media_manifest_url": (
                f"/media/{public_settings.media_version}/manifest.json"
            ),
            "analyst_avatar": chat_service.analyst_avatar(),
            "experience_notice_version": public_settings.experience_notice_version,
            "sticker_version": public_settings.sticker_version,
            "sticker_count": int(sticker_status.get("sticker_count") or 0),
            "privacy_policy": {
                "version": public_settings.privacy_policy_version,
                "effective_at": public_settings.privacy_effective_at,
                "url": "/privacy/",
            },
            "attribution_url": public_settings.attribution_url,
            "max_provider_calls_per_action": public_settings.max_provider_calls_per_action,
            "credential_policy": {
                "lifetime_hours": public_settings.byok_lifetime_hours,
                "storage": "sessionStorage",
                "renewal": "none",
                "retention": ({
                    "enabled": True,
                    "notice_version": BYOK_RETENTION_NOTICE_VERSION,
                    "options": [value for value in BYOK_RETENTION_HOURS if value != "tab"],
                    "storage": "localStorage",
                } if public_settings.byok_retention_enabled else {"enabled": False}),
            },
            "movement_catalog": chat_service.movement_catalog(),
            # The generalized stage adapter stays off until an approved,
            # immutable 22-character release is supplied.
            "stage_release": {"enabled": False, "manifest_url": None, "sha256": None},
            "feature_flags": {
                "joint_movement": True,
                "rendezvous_actions": True,
                "request_recovery": True,
                "sticker_display_derivatives": True,
                "indexeddb_v4": True,
                "feedback_context_choice": True,
            },
            # Keep an unreviewed/private package out of the public URL space.
            "sticker_manifest_url": (
                f"/media/{public_settings.sticker_version}/manifest.json"
                if sticker_status.get("status") == "ok"
                else None
            ),
            "state_schedule": {
                "scope": "subject_daily",
                "timezone": "Asia/Hong_Kong",
                "update": "00:00",
            },
            "arrival_reaction_probability": public_settings.arrival_probability,
            "automatic_summary": {
                "default_enabled": True,
                "successful_round_interval": 12,
                "counts_toward_daily_limit": True,
                "counts_toward_hourly_limit": False,
            },
            "communication_channels": ["text", "in_person"],
            "content_block_types": ["message", "speech", "action", "sticker"],
            "source_links": {
                "project_snow": "https://github.com/X1aoB/Project_Snow",
                "mywebsite": "https://github.com/X1aoB/MyWebsite",
                "releases": "https://github.com/X1aoB/Project_Snow/releases",
            },
        }

    @app.get("/public/v1/characters")
    def characters() -> dict[str, Any]:
        return {"characters": chat_service.characters(), "count": 22}

    @app.get("/public/v1/stickers")
    def stickers(
        section: str = "",
        character_id: str = "",
        emotion_tag: str = "",
        candidate_scope: str = "",
        q: str = "",
        cursor: int = 0,
        limit: int = 60,
    ) -> dict[str, Any]:
        if candidate_scope not in {"", "generic", "character"}:
            raise _error("invalid_sticker_candidate_scope", 422)
        return chat_service.stickers.list(
            section=section,
            character_id=character_id,
            emotion_tag=emotion_tag,
            candidate_scope=candidate_scope,
            q=q,
            cursor=cursor,
            limit=limit,
        )

    @app.get("/public/v1/attributions")
    def attributions() -> dict[str, Any]:
        return {
            "schema_version": "project-snow-attribution-1",
            "license": "CC BY-NC-SA 4.0",
            "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
            "license_source_page": "https://wiki.biligame.com/sonw/%E9%A6%96%E9%A1%B5",
            "license_source_revision_id": "21546",
            "noncommercial": True,
            "avatars": chat_service.media.attributions(),
            "stickers": chat_service.stickers.attributions(),
            "knowledge_data": {
                "version": public_settings.data_version,
                "notice": "Source-page revisions and transformations are recorded in the immutable data release manifest.",
                "project_source": "https://github.com/X1aoB/Project_Snow",
                "url": (
                    "/public/v1/attributions/data" if knowledge_review is not None else None
                ),
                "source_count": (
                    int(knowledge_review.get("source_count") or 0)
                    if knowledge_review is not None
                    else 0
                ),
                "modifications": (
                    list(knowledge_review.get("modifications") or [])
                    if knowledge_review is not None
                    else []
                ),
            },
        }

    @app.get("/public/v1/attributions/data")
    def attribution_data() -> JSONResponse:
        if knowledge_review is None:
            raise _error("knowledge_attribution_unavailable", 404)
        return JSONResponse(
            content=knowledge_review,
            headers={"Cache-Control": "public, max-age=300, stale-while-revalidate=60"},
        )

    @app.get("/public/v1/byok/verification")
    async def byok_verification(request: Request) -> dict[str, bool]:
        # This hint avoids a redundant client challenge; session creation still
        # independently checks the current subject's verification below.
        required = await async_store.call(
            "verification_required", request.state.subject_hash, "byok"
        )
        return {"verification_required": required}

    @app.post("/public/v1/byok/session")
    async def byok_session(request: Request, payload: ByokSessionRequest) -> dict[str, Any]:
        if not all(
            (
                payload.accepted_transit_notice,
                payload.accepted_cost_notice,
                payload.accepted_local_history_notice,
            )
        ):
            raise _error("byok_notices_required", 422)
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        if spec.provider_id == SUBSCRIPTION_PROVIDER:
            raise _error("subscription_pairing_required", 422)
        relay_base: str | None = None
        relay_host = ""
        if spec.provider_id == "relay":
            try:
                relay_base, relay_host = normalise_relay_base_url(payload.base_url)
            except RelayUrlError as exc:
                raise _error("relay_url_invalid", 422) from exc
        if payload.retention != "tab":
            if not public_settings.byok_retention_enabled:
                raise _error("credential_retention_unavailable", 422)
            if (payload.retention_notice_version != BYOK_RETENTION_NOTICE_VERSION
                    or payload.accepted_retention_notice is not True):
                raise _error("credential_retention_notice_required", 422)
        await async_store.call("consume_limits",
            request.state.subject_hash,
            [("byok_session_hour", "hour", 10), ("byok_session_day", "day", 30)],
        )
        verification_required = await async_store.call("verification_required", request.state.subject_hash, "byok")
        if verification_required:
            verified = await verify_turnstile(
                public_settings,
                payload.turnstile_token,
                _client_ip(request),
                action="byok-session",
            )
            if not verified:
                raise _error("turnstile_required", 403)
            await async_store.call("mark_verified", request.state.subject_hash, "byok")
        credential_kwargs = {"retention": payload.retention}
        if payload.retention == "tab":
            # Preserve the pre-retention deployment knob for the legacy
            # session-scoped envelope. Retained credentials use exact terms.
            credential_kwargs["lifetime"] = timedelta(hours=public_settings.byok_lifetime_hours)
        credential, expires_at = issue_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            provider=spec.provider_id,
            api_key=payload.api_key,
            relay_base=relay_base,
            **credential_kwargs,
        )
        result = {
            "provider": spec.provider_id,
            "credential": credential,
            "expires_at": expires_at.isoformat(),
            "stored": False,
            "retention": payload.retention,
        }
        if spec.provider_id == "relay":
            # Host only — never the sealed path, query or the key.
            result["relay_host"] = relay_host
        return result

    @app.post("/public/v1/byok/revoke")
    async def byok_revoke(request: Request) -> dict[str, Any]:
        await async_store.call("consume_limits", request.state.subject_hash,
            [("byok_revoke_hour", "hour", 10), ("byok_revoke_day", "day", 30)])
        revoked_before = await async_store.call("revoke_credentials", request.state.subject_hash)
        return {"revoked": True, "revoked_before": revoked_before.isoformat()}

    @app.post("/public/v1/byok/models")
    async def models(request: Request, payload: ModelDiscoveryRequest) -> dict[str, Any]:
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        claims = await open_request_credential(request, payload.credential, spec.provider_id)
        spec = relay_aware_spec(spec, claims)
        await async_store.call("consume_limits", request.state.subject_hash, [("model_discovery_hour", "hour", 20)])
        try:
            discovered = await discover_models(
                spec,
                str(claims["api_key"]),
                client=provider_http,
            )
        except ProviderRequestError as exc:
            raise _error(exc.code, exc.status_code) from exc
        return {"provider": spec.provider_id, "models": discovered,
                "manual_model_allowed": spec.provider_id != SUBSCRIPTION_PROVIDER}

    @app.post("/public/v1/presence/resolve")
    def presence_resolve(request: Request, payload: PresenceResolveRequest) -> dict[str, Any]:
        return chat_service.resolve_presence(payload, request.state.subject_hash)

    @app.post("/public/v1/presence/transition")
    async def presence_transition(
        request: Request, payload: PresenceTransitionRequest
    ) -> dict[str, Any]:
        cache_id = "presence-transition:" + str(payload.request_id)
        request_body = payload.model_dump(mode="json")
        subject = request.state.subject_hash
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        try:
            claim_status, cached = await claim_owned_request(
                cache_id,
                subject,
                _request_hash(request_body),
            )
            request_claimed = claim_status == "claimed"
            owns_subject = owns_subject or request_claimed
            if claim_status == "conflict":
                raise _error("request_id_conflict", 409)
            if claim_status == "processing":
                raise _error("request_in_progress", 409)
            if claim_status == "completed" and cached is not None:
                return {**cached, "idempotent_replay": True}
            result = chat_service.transition_presence(payload, subject)
            change = authority_change(public_settings, subject, result, payload.state_package, payload.character_id)
            await complete_owned_request(cache_id, result, rendezvous_change=change)
            return result
        except PublicSecurityError:
            if request_claimed:
                await release_owned_request(cache_id)
            raise
        except ValueError as exc:
            if request_claimed:
                await release_owned_request(cache_id)
            raise _error("invalid_presence_transition", 422) from exc
        except Exception:
            if request_claimed:
                await release_owned_request(cache_id)
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v1/presence/arrival")
    async def presence_arrival(request: Request, payload: PresenceArrivalRequest) -> dict[str, Any]:
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        if spec.provider_id != SUBSCRIPTION_PROVIDER and payload.reasoning_effort is not None:
            raise _error("reasoning_effort_not_supported", 422)
        claims = await open_request_credential(request, payload.credential, spec.provider_id)
        spec = relay_aware_spec(spec, claims)
        cache_id = "presence-arrival:" + str(payload.arrival_id)
        request_body = _model_request_body(payload)
        subject = request.state.subject_hash
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        generation_started = False
        prepared: dict[str, Any] = {}
        try:
            claim_status, cached = await claim_owned_request(
                cache_id,
                subject,
                _request_hash(request_body),
            )
            request_claimed = claim_status == "claimed"
            owns_subject = owns_subject or request_claimed
            if claim_status == "conflict":
                raise _error("request_id_conflict", 409)
            if claim_status == "processing":
                raise _error("request_in_progress", 409)
            if claim_status == "completed" and cached is not None:
                return {**cached, "idempotent_replay": True}
            prepared = chat_service.prepare_presence_arrival(
                payload,
                subject,
            )
            if prepared["decision"] == "unnoticed":
                result = {key: value for key, value in prepared.items() if key != "state"}
            else:
                if spec.provider_id == SUBSCRIPTION_PROVIDER:
                    payload.reasoning_effort = subscription_broker.preflight(
                        str(claims["api_key"]), payload.model, payload.reasoning_effort,
                    )
                try:
                    await async_store.call("consume_limits",
                        request.state.subject_hash,
                        [("chat_hour", "hour", 120), ("chat_day", "day", 600)],
                    )
                except RateLimitExceeded:
                    result = chat_service.failed_presence_arrival(
                        prepared,
                        "rate_limit_exceeded",
                        model_called=False,
                    )
                else:
                    # Let GenerationBusy reach the shared 429 handler while
                    # no response body has started, so every model-backed
                    # endpoint carries the same Retry-After contract.
                    generation_started = True
                    try:
                        result = await chat_service.finish_presence_arrival(
                            prepared,
                            payload,
                            subject,
                            spec,
                            str(claims["api_key"]),
                        )
                    except GenerationBusy:
                        # The gate rejects before starting the provider thread.
                        generation_started = False
                        raise
            change = authority_change(public_settings, subject, result, payload.state_package, payload.character_id)
            await complete_owned_request(cache_id, result, rendezvous_change=change)
            return result
        except (Exception, asyncio.CancelledError) as exc:
            if request_claimed:
                if generation_started:
                    # Provider work may already have been charged, including
                    # when response validation or the final cache write fails.
                    # Never delete this claim and permit the same UUID to pay
                    # again. If this terminal write also fails, stop renewing
                    # and let the existing lease expire into an interruption.
                    interrupted = PublicChatService.failed_presence_arrival(
                        prepared, "generation_interrupted", model_called=True,
                    )
                    try:
                        await complete_owned_request(cache_id, interrupted)
                    except PublicStoreUnavailable:
                        LOGGER.warning("public_arrival_terminal_write_unavailable")
                else:
                    await release_owned_request(cache_id)
            if isinstance(exc, ValueError) and not isinstance(exc, PublicSecurityError):
                raise _error("invalid_presence_request", 422) from exc
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    def get_dialogue_v2_service():
        if app.state.dialogue_v2_service is None:
            from .dialogue_v2_service import DialogueV2Service
            app.state.dialogue_v2_service = DialogueV2Service(chat_service, public_settings)
        return app.state.dialogue_v2_service

    def recovery_only(request: Request) -> bool:
        value = request.headers.get("X-Snow-Recovery-Only")
        if value not in {None, "1"}:
            raise _error("invalid_request", 422, retryable=False)
        return value == "1"

    install_proactive_routes(app, public_settings, async_store, chat_service, get_dialogue_v2_service,
        provider_http, subscription_broker, acquire_subject_generation, release_subject_generation,
        active_request_leases)

    @app.post("/public/v2/presence/arrival")
    async def presence_arrival_v2(request: Request, payload: PresenceArrivalV2Request):
        replay_only = recovery_only(request)
        request.state.public_request_id = str(payload.arrival_id)
        subject = request.state.subject_hash
        cache_id = "presence-arrival-v2:" + str(payload.arrival_id)
        request_hash = _request_hash(_model_request_body(payload))

        def response(value):
            # A greeting cannot overwrite the already committed world/context,
            # including when its ACK arrives after the browser selects someone else.
            return {"arrival_id": str(payload.arrival_id), "character_id": payload.character_id,
                    "communication_channel": "in_person", "decision": "noticed",
                    "reaction": None, "model_called": value.get("terminal_error") == "generation_interrupted",
                    "terminal_error": "", **{key: value[key] for key in (
                        "reaction", "model_called", "terminal_error", "error", "diagnostics", "usage",
                    ) if key in value}}

        # Lookup precedes credentials, feature switches and ticket expiry. It
        # never claims a missing/expired UUID, even after a lost first response.
        status, cached = await async_store.call("lookup_request", cache_id, subject, request_hash)
        if status == "conflict":
            raise _error("request_id_conflict", 409)
        if status == "completed" and cached is not None:
            return {**response(cached), "idempotent_replay": True}
        if status == "processing":
            raise _error("request_in_progress", 409, request_id=str(payload.arrival_id))
        if replay_only:
            raise _error("request_result_unavailable", 410, request_id=str(payload.arrival_id), retryable=False)
        if not public_settings.scene_actions_v2:
            raise _error("scene_actions_v2_disabled", 503, retryable=False)
        try:
            joined = await async_store.call("request_result", "scene-action:" + str(payload.arrival_id), subject)
            prepared = get_dialogue_v2_service().prepare_arrival(payload, subject, joined)
        except ArrivalTicketExpired as exc:
            raise _error("arrival_ticket_expired", 410, retryable=False) from exc
        except (PublicSecurityError, ValueError) as exc:
            raise _error("invalid_arrival_ticket", 422, retryable=False) from exc
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        if spec.provider_id != SUBSCRIPTION_PROVIDER and payload.reasoning_effort is not None:
            raise _error("reasoning_effort_not_supported", 422)
        claims = await open_request_credential(request, payload.credential, spec.provider_id)
        spec = relay_aware_spec(spec, claims)
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        generation_started = False
        try:
            status, cached = await claim_owned_request(cache_id, subject, request_hash)
            request_claimed = status == "claimed"
            owns_subject = owns_subject or request_claimed
            if status == "conflict":
                raise _error("request_id_conflict", 409)
            if status == "processing":
                raise _error("request_in_progress", 409)
            if status == "completed" and cached is not None:
                return {**response(cached), "idempotent_replay": True}
            # Admission may have waited behind database work. Expiry is checked
            # again while owning the claim, immediately before paid admission.
            try:
                verify_arrival_ticket(public_settings, payload, subject)
            except ArrivalTicketExpired as exc:
                raise _error("arrival_ticket_expired", 410, retryable=False) from exc
            if spec.provider_id == SUBSCRIPTION_PROVIDER:
                payload.reasoning_effort = subscription_broker.preflight(
                    str(claims["api_key"]), payload.model, payload.reasoning_effort)
            try:
                await async_store.call("consume_limits", subject,
                    [("chat_hour", "hour", 120), ("chat_day", "day", 600)])
            except RateLimitExceeded:
                result = PublicChatService.failed_presence_arrival(prepared, "rate_limit_exceeded", model_called=False)
            else:
                generation_started = True
                try:
                    result = await chat_service.finish_presence_arrival(
                        prepared, payload, subject, spec, str(claims["api_key"]))
                except GenerationBusy:
                    generation_started = False
                    raise
            result = response(result)
            await complete_owned_request(cache_id, result)
            return result
        except (Exception, asyncio.CancelledError):
            if request_claimed:
                if generation_started:
                    interrupted = response(PublicChatService.failed_presence_arrival(
                        prepared, "generation_interrupted", model_called=True))
                    try:
                        await complete_owned_request(cache_id, interrupted)
                    except PublicStoreUnavailable:
                        LOGGER.warning("public_arrival_v2_terminal_write_unavailable")
                else:
                    await release_owned_request(cache_id)
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v2/scene/action")
    async def dialogue_v2_scene_action(request: Request, envelope: SceneActionRequest):
        replay_only = recovery_only(request)
        cache_id = "scene-action:" + str(envelope.request_id)
        request.state.public_request_id = str(envelope.request_id)
        subject = request.state.subject_hash
        request_hash = _request_hash(envelope.model_dump(mode="json"))
        if replay_only:
            status, cached = await async_store.call("lookup_request", cache_id, subject, request_hash)
            if status == "conflict":
                raise _error("request_id_conflict", 409)
            if status == "completed" and cached is not None:
                return {**cached, "idempotent_replay": True}
            if status == "processing":
                raise _error("request_in_progress", 409, request_id=str(envelope.request_id))
            raise _error("request_result_unavailable", 410,
                         request_id=str(envelope.request_id), retryable=False)
        if not public_settings.scene_actions_v2 and envelope.action != "resolve":
            raise _error("scene_actions_v2_disabled", 503, retryable=False)
        if not any((public_settings.dialogue_domains_v2, public_settings.scene_actions_v2,
                    public_settings.session_memory_v2)):
            raise _error("dialogue_v2_disabled", 503, retryable=False)
        try:
            validate_session_context_transfer(envelope)
        except PublicSecurityError as exc:
            code, http_status = _public_security_disposition(exc)
            raise _error(code, http_status, retryable=False) from exc
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        try:
            status, cached = await claim_owned_request(cache_id, subject, request_hash)
            request_claimed = status == "claimed"
            owns_subject = owns_subject or request_claimed
            if status == "conflict":
                raise _error("request_id_conflict", 409)
            if status == "processing":
                raise _error("request_in_progress", 409)
            if status == "completed" and cached is not None:
                return {**cached, "idempotent_replay": True}
            result = await asyncio.to_thread(get_dialogue_v2_service().scene_action, envelope, subject)
            _dialogue_v2_metadata(result)
            change = authority_change(public_settings, subject, result, envelope.state_package, envelope.character_id)
            await complete_owned_request(cache_id, result, rendezvous_change=change)
            return result
        except PublicSecurityError as exc:
            if request_claimed:
                await release_owned_request(cache_id)
            code, http_status = _public_security_disposition(exc)
            raise _error(code, http_status, retryable=False) from exc
        except ValueError as exc:
            if request_claimed:
                await release_owned_request(cache_id)
            raise _error("invalid_scene_action", 422, retryable=False) from exc
        except Exception:
            if request_claimed:
                await release_owned_request(cache_id)
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v1/chat/stream")
    async def chat_stream(request: Request, payload: ChatRequest):
        return await chat_stream_impl(request, payload)

    @app.post("/public/v2/chat/stream")
    async def dialogue_v2_chat_stream(request: Request, envelope: DialogueV2ChatRequest):
        request.state.public_request_id = str(envelope.chat.request_id)
        replay_only = recovery_only(request)
        if not replay_only:
            if not any((public_settings.dialogue_domains_v2, public_settings.scene_actions_v2,
                        public_settings.session_memory_v2)):
                raise _error("dialogue_v2_disabled", 503, retryable=False)
            try:
                validate_session_context_transfer(envelope)
                get_dialogue_v2_service().validate_chat(envelope, request.state.subject_hash)
            except PublicSecurityError as exc:
                code, status = _public_security_disposition(exc)
                raise _error(code, status, retryable=False) from exc
            except ValueError as exc:
                raise _error("invalid_dialogue_context", 422, retryable=False) from exc

        async def generate(subject, spec, api_key, *, gate_reserved=False):
            result = await get_dialogue_v2_service().chat(
                envelope, subject, spec, api_key, gate_reserved=gate_reserved,
            )
            result["_proactive_authority_change"] = authority_change(public_settings, subject,
                result, envelope.chat.state_package, envelope.chat.character_id)
            return result

        return await chat_stream_impl(
            request, envelope.chat, business_body=_dialogue_v2_request_body(envelope),
            generate_callback=generate, recovery=replay_only, dialogue_v2=True,
        )

    async def chat_stream_impl(
        request: Request, payload: ChatRequest, *, business_body=None,
        generate_callback=None, recovery=False, dialogue_v2=False,
    ):
        request_started = time.monotonic()
        # The rollout switch controls new upstream publication, never whether
        # an opted-in client can recover an already durable prefix.
        stream_v2 = any(
            key.lower() == b"x-snow-stream-version" and value == b"2"
            for key, value in request.scope.get("headers", [])
        )
        # Scene/context mutations require complete validation and one durable
        # result ACK. Existing v1 verified publication remains unchanged.
        publish_v2 = stream_v2 and public_settings.verified_block_stream and not dialogue_v2
        spec = provider_spec(payload.provider, tuple(PROVIDERS) if recovery else public_settings.enabled_providers)
        if spec.provider_id != SUBSCRIPTION_PROVIDER and payload.reasoning_effort is not None:
            raise _error("reasoning_effort_not_supported", 422)
        public_model = redact_sensitive_text(payload.model, 200)
        if not recovery:
            chat_service.ensure_character_available(payload.character_id)
        try:
            # Validate a structured invitation before opening BYOK, claiming
            # idempotency, or reserving provider budget. Hidden, forged, and
            # message-mismatched destinations therefore cost no model call.
            if not recovery and not dialogue_v2:
                chat_service.validate_movement_request(payload)
        except ValueError as exc:
            raise _error("invalid_movement_location", 422) from exc
        try:
            canonical_blocks = (
                [block.model_dump() for block in payload.content_blocks] if recovery
                else chat_service.validate_content_blocks(payload.content_blocks, payload.communication_channel)
            )
        except ValueError as exc:
            raise _error("sticker_unavailable", 422) from exc
        for original, canonical in zip(payload.content_blocks, canonical_blocks):
            if canonical.get("type") == "sticker":
                original.caption = str(canonical.get("caption") or "")
        claims = ({"api_key": ""} if recovery else
                  await open_request_credential(request, payload.credential, spec.provider_id))
        if not recovery:
            spec = relay_aware_spec(spec, claims)
        request_body = business_body if business_body is not None else _model_request_body(payload)
        request_id = str(payload.request_id)
        request.state.public_request_id = request_id
        subject = request.state.subject_hash
        owns_subject = False if recovery else await acquire_subject_generation(subject, request_id)
        try:
            claim_status, cached = (
                await async_store.call("lookup_request", request_id, subject, _request_hash(request_body))
                if recovery else await claim_owned_request(request_id, subject, _request_hash(request_body))
            )
        except Exception:
            if owns_subject:
                await release_subject_generation(subject, request_id)
            raise
        if claim_status == "missing":
            raise _error("request_result_unavailable", 410, request_id=request_id, retryable=False)
        owns_subject = owns_subject or claim_status == "claimed"
        if claim_status == "conflict":
            if owns_subject:
                await release_subject_generation(subject, request_id)
            raise _error("request_id_conflict", 409)
        idempotent_replay = claim_status in {"processing", "completed"}
        result: dict[str, Any] | None = None
        job: asyncio.Task[dict[str, Any]] | None = None
        journal: DeliveryJournal | None = None
        remote_processing = False
        if claim_status == "completed" and cached is not None:
            result = {**cached, "idempotent_replay": True}
            if owns_subject:
                await release_subject_generation(subject, request_id)
        elif claim_status == "processing":
            job = chat_jobs.get(request_id)
            journal = delivery_journals.get(request_id) if stream_v2 else None
            if job is None:
                # The original worker may be running in another process (or
                # the process may have restarted).  The durable claim still
                # prevents a second provider call; the client can retry the
                # same UUID until the cached terminal result is available.
                if owns_subject:
                    await release_subject_generation(subject, request_id)
                if stream_v2 or dialogue_v2:
                    remote_processing = True
                else:
                    raise _error("request_in_progress", 409)
        else:
            try:
                if spec.provider_id == SUBSCRIPTION_PROVIDER:
                    payload.reasoning_effort = subscription_broker.preflight(
                        str(claims["api_key"]), payload.model, payload.reasoning_effort,
                    )
                await async_store.call("consume_limits",
                    request.state.subject_hash,
                    [("chat_hour", "hour", 120), ("chat_day", "day", 600)],
                )
            except Exception:
                await release_owned_request(request_id)
                await release_subject_generation(subject, request_id)
                raise

            unsafe_category = input_safety_category(payload.message)
            generation_reserved = False
            queue_started = time.monotonic()
            queue_ms = 0
            if stream_v2:
                journal = DeliveryJournal(request_id, payload.character_id)
                journal.phase("queued")
                delivery_journals[request_id] = journal
            if not unsafe_category and not stream_v2:
                try:
                    await chat_service.gate.acquire()
                    generation_reserved = True
                    queue_ms = max(0, int((time.monotonic() - queue_started) * 1000))
                except BaseException:
                    # Admission happens before the StreamingResponse exists so
                    # queue saturation can carry an HTTP Retry-After header.
                    # A client disconnect while waiting must also release the
                    # durable claim and per-subject owner.
                    await release_owned_request(request_id)
                    await release_subject_generation(subject, request_id)
                    raise

            async def run_generation() -> dict[str, Any]:
                started_at = time.monotonic()
                exception_type = ""
                terminal_stage = "complete"
                loop = asyncio.get_running_loop()
                first_committed_ms: int | None = None

                async def commit_blocks(blocks: list[dict[str, Any]]) -> bool:
                    nonlocal first_committed_ms
                    if _public_content_blocks_payload(blocks) != blocks:
                        return False
                    records = await async_store.call("commit_request_blocks", request_id, subject, blocks)
                    if journal is not None:
                        journal.committed(records)
                    if records and first_committed_ms is None:
                        first_committed_ms = max(0, int((time.monotonic() - request_started) * 1000))
                    return True

                def publish_blocks(blocks: list[dict[str, Any]]) -> bool:
                    # The generator runs on a worker thread. Waiting for the
                    # transaction ACK prevents an uncommitted preview escaping.
                    # A timeout never cancels/retries an uncertain transaction.
                    future = asyncio.run_coroutine_threadsafe(commit_blocks(blocks), loop)
                    return future.result(timeout=30)

                observer = GenerationObserver(
                    on_phase=(lambda name, elapsed: loop.call_soon_threadsafe(journal.phase, name, elapsed))
                    if journal is not None else None,
                    on_verified_blocks=publish_blocks if publish_v2 else None,
                )
                try:
                    if unsafe_category:
                        generated = chat_service.policy_rejection(
                            payload,
                            request.state.subject_hash,
                            spec,
                            unsafe_category,
                        )
                    else:
                        with observe_generation(observer):
                            if generate_callback is not None:
                                generated = await generate_callback(
                                    request.state.subject_hash, spec, str(claims["api_key"]),
                                    gate_reserved=generation_reserved,
                                )
                            else:
                                generated = await chat_service.chat(
                                    payload, request.state.subject_hash, spec,
                                    str(claims["api_key"]), gate_reserved=generation_reserved,
                                )
                            if dialogue_v2:
                                _dialogue_v2_metadata(generated)
                except GenerationBusy as exc:
                    exception_type = type(exc).__name__
                    terminal_stage = "generation_queue"
                    generated = {
                        "request_id": request_id,
                        "character_id": payload.character_id,
                        "provider": spec.provider_id,
                        "model": public_model,
                        "communication_channel": payload.communication_channel,
                        "answer": "",
                        "content_blocks": [],
                        "state_package": payload.state_package,
                        "terminal_error": str(exc),
                        "diagnostics": {"error_stage": "generation_queue"},
                    }
                except CharacterUnavailable:
                    exception_type = "CharacterUnavailable"
                    terminal_stage = "character_data"
                    generated = {
                        "request_id": request_id,
                        "character_id": payload.character_id,
                        "provider": spec.provider_id,
                        "model": public_model,
                        "communication_channel": payload.communication_channel,
                        "answer": "",
                        "content_blocks": [],
                        "state_package": payload.state_package,
                        "terminal_error": "character_unavailable",
                        "diagnostics": {"error_stage": "character_data"},
                    }
                except PublicSecurityError as exc:
                    exception_type = type(exc).__name__
                    terminal_stage = "state_validation"
                    security_code, _security_status = _public_security_disposition(exc)
                    generated = {
                        "request_id": request_id,
                        "character_id": payload.character_id,
                        "provider": spec.provider_id,
                        "model": public_model,
                        "communication_channel": payload.communication_channel,
                        "answer": "",
                        "content_blocks": [],
                        "state_package": "",
                        "terminal_error": security_code,
                        "diagnostics": {"error_stage": "state_validation"},
                    }
                except Exception as exc:
                    exception_type = type(exc).__name__
                    terminal_stage = "unknown"
                    generated = {
                        "request_id": request_id,
                        "character_id": payload.character_id,
                        "provider": spec.provider_id,
                        "model": public_model,
                        "communication_channel": payload.communication_channel,
                        "answer": "",
                        "content_blocks": [],
                        "state_package": payload.state_package,
                        "terminal_error": "generation_failed",
                        "diagnostics": {"error_stage": "unknown"},
                    }
                diagnostics = generated.setdefault("diagnostics", {})
                timings = diagnostics.setdefault("timings_ms", {})
                timings.update(observer.timings_ms)
                timings["queue_ms"] = queue_ms
                if first_committed_ms is not None:
                    timings["first_committed_block_ms"] = first_committed_ms
                durable_started = time.monotonic()
                if journal is not None:
                    journal.phase("saving", int((durable_started - started_at) * 1000))
                try:
                    change = generated.pop("_proactive_authority_change", None)
                    if not dialogue_v2:
                        change = authority_change(public_settings, subject, generated,
                            payload.state_package, payload.character_id)
                    await complete_owned_request(request_id, generated, rendezvous_change=change)
                except PublicStoreUnavailable as exc:
                    exception_type = type(exc).__name__
                    terminal_stage = "idempotency_store"
                    # Do not stream a successful answer when the durable
                    # idempotency record cannot be written. Public chat is
                    # intentionally fail-closed while PostgreSQL is down.
                    generated = {
                        **generated,
                        "answer": "",
                        "content_blocks": [],
                        "terminal_error": "public_database_unavailable",
                        "diagnostics": {"error_stage": "idempotency_store"},
                    }
                timings["durable_commit_ms"] = max(0, int((time.monotonic() - durable_started) * 1000))
                timings["total"] = max(0, int((time.monotonic() - request_started) * 1000))
                diagnostics = (
                    generated.get("diagnostics")
                    if isinstance(generated.get("diagnostics"), dict)
                    else {}
                )
                timings = diagnostics.get("timings_ms")
                provider_calls = (
                    timings.get("provider_http_calls")
                    if isinstance(timings, dict)
                    else 0
                )
                LOGGER.info(
                    json.dumps(
                        {
                            "event": "public_generation_complete",
                            "request_id": request_id,
                            "character_id": payload.character_id,
                            "stage": terminal_stage,
                            "exception_type": exception_type or None,
                            "terminal_error": str(generated.get("terminal_error") or ""),
                            "elapsed_ms": max(
                                0, int((time.monotonic() - started_at) * 1000)
                            ),
                            "provider_calls": (
                                provider_calls
                                if isinstance(provider_calls, int)
                                and not isinstance(provider_calls, bool)
                                and provider_calls >= 0
                                else 0
                            ),
                            "provider": spec.provider_id,
                            "model_profile": payload.model.casefold()
                            if immersive_model_policy(spec.provider_id, payload.model).get("capability_verified")
                            else "other",
                            "channel": payload.communication_channel,
                            "history_band": "long" if len(payload.recent_history) > 16
                            else "medium" if len(payload.recent_history) > 8 else "short",
                            "generation_class": str(diagnostics.get("generation_class") or "unknown"),
                            "stream_version": 2 if stream_v2 else 1,
                            "timings_ms": latency_metrics({"timings_ms": timings}),
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                )
                return generated

            async def run_reserved_generation() -> dict[str, Any]:
                nonlocal generation_reserved, queue_ms
                try:
                    if stream_v2 and not unsafe_category:
                        try:
                            await chat_service.gate.acquire()
                            generation_reserved = True
                            queue_ms = max(0, int((time.monotonic() - queue_started) * 1000))
                        except GenerationBusy as exc:
                            rejected = {"terminal_error": str(exc), "answer": "", "content_blocks": []}
                            # Admission failed before any provider call. A
                            # retry of this UUID must be able to enter later,
                            # just as it can with the original v1 contract.
                            await release_owned_request(request_id)
                            return rejected
                    if journal is not None:
                        journal.phase("preparing", queue_ms)
                    return await run_generation()
                finally:
                    if generation_reserved:
                        chat_service.gate.release()

            try:
                job = asyncio.create_task(
                    run_reserved_generation(), name=f"public-chat:{request_id}"
                )
            except Exception:
                delivery_journals.pop(request_id, None)
                if generation_reserved:
                    chat_service.gate.release()
                await release_owned_request(request_id)
                await release_subject_generation(subject, request_id)
                raise
            chat_jobs[request_id] = job
            active_request_leases[request_id] = job
            active_subject_tasks[subject] = job

            def forget_job(completed: asyncio.Task[dict[str, Any]]) -> None:
                chat_jobs.pop(request_id, None)
                delivery_journals.pop(request_id, None)
                if journal is not None:
                    journal.changed.set()
                if active_request_leases.get(request_id) is completed:
                    active_request_leases.pop(request_id, None)
                if (
                    active_subject_requests.get(subject) == request_id
                    and active_subject_tasks.get(subject) is completed
                ):
                    active_subject_requests.pop(subject, None)
                    active_subject_tasks.pop(subject, None)
                # Retrieve a possible storage failure so a disconnected SSE
                # client cannot leave an unobserved task exception behind.
                if not completed.cancelled():
                    completed.exception()

            job.add_done_callback(forget_job)

        async def events():
            yield _sse(
                "meta",
                {
                    "request_id": request_id,
                    "character_id": payload.character_id,
                    "provider": spec.provider_id,
                    "model": public_model,
                    "communication_channel": payload.communication_channel,
                    "data_version": public_settings.data_version,
                    "idempotent_replay": idempotent_replay,
                },
            )
            sent_sequences: set[int] = set()
            journal_cursor = 0

            async def stored_blocks():
                if not stream_v2:
                    return []
                return await async_store.call("request_blocks", request_id, subject)

            def block_events(records):
                for record in records:
                    sequence = record["sequence"]
                    if sequence not in sent_sequences:
                        sent_sequences.add(sequence)
                        yield _sse("committed_block", {
                            "request_id": request_id,
                            "character_id": payload.character_id,
                            **record,
                        })

            if stream_v2:
                for packet in block_events(await stored_blocks()):
                    yield packet
            resolved = result
            while resolved is None and job is not None:
                if stream_v2 and journal is not None:
                    journal.changed.clear()
                    while journal_cursor < len(journal.events):
                        event, event_payload = journal.events[journal_cursor]
                        journal_cursor += 1
                        if event == "committed_block":
                            if event_payload["sequence"] in sent_sequences:
                                continue
                            sent_sequences.add(event_payload["sequence"])
                        yield _sse(event, event_payload)
                    if job.done():
                        resolved = job.result()
                        break
                    wake = asyncio.create_task(journal.changed.wait())
                    try:
                        ready, _ = await asyncio.wait((job, wake), timeout=4.0, return_when=asyncio.FIRST_COMPLETED)
                        if not ready:
                            yield ": heartbeat\n\n"
                    finally:
                        wake.cancel()
                        await asyncio.gather(wake, return_exceptions=True)
                    continue
                try:
                    resolved = await asyncio.wait_for(asyncio.shield(job), timeout=4.0)
                except TimeoutError:
                    yield ": heartbeat\n\n"
            # Another owner is still producing this UUID. Poll only durable
            # state; this connection never acquires a generation slot.
            while resolved is None and remote_processing:
                for packet in block_events(await stored_blocks()):
                    yield packet
                resolved = await async_store.call("request_result", request_id, subject)
                if resolved is not None:
                    break
                if time.monotonic() - request_started >= 300:
                    yield _sse("error", {"code": "request_in_progress", "retryable": True,
                                         "partial": bool(sent_sequences)})
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
            if stream_v2:
                # Covers completed replays, a lost transaction ACK and a job
                # finishing between the final wake and journal consumption.
                for packet in block_events(await stored_blocks()):
                    yield packet
            if resolved is None:
                yield _sse(
                    "error",
                    {
                        "code": "generation_failed",
                        "retryable": True,
                        "request_id": request_id,
                        "stage": "unknown",
                    },
                )
                return
            result_payload = resolved
            if result_payload.get("terminal_error"):
                terminal_error = str(result_payload["terminal_error"])
                internal_diagnostics = (
                    result_payload.get("diagnostics")
                    if isinstance(result_payload.get("diagnostics"), dict)
                    else {}
                )
                service_error = (
                    result_payload.get("error")
                    if isinstance(result_payload.get("error"), dict)
                    else {}
                )
                error_stage = _error_stage(
                    terminal_error,
                    service_error.get("stage") or internal_diagnostics.get("error_stage"),
                )
                retryable = _error_retryable(
                    terminal_error,
                    500,
                    service_error.get("retryable"),
                )
                retry_after_seconds = service_error.get("retry_after_seconds")
                yield _sse(
                    "error",
                    {
                        "code": terminal_error,
                        "retryable": retryable,
                        "request_id": request_id,
                        "stage": error_stage,
                        "idempotent_replay": idempotent_replay,
                        **(
                            {
                                "retry_after_seconds": min(int(retry_after_seconds), 300)
                            }
                            if isinstance(retry_after_seconds, int)
                            and not isinstance(retry_after_seconds, bool)
                            and retry_after_seconds > 0
                            else {"retry_after_seconds": 2}
                            if terminal_error.startswith("generation_queue_")
                            or terminal_error == "public_database_unavailable"
                            else {}
                        ),
                        **({"partial": bool(sent_sequences), "retryable": False}
                           if stream_v2 and sent_sequences else {}),
                    },
                )
                return
            raw_content_blocks = list(result_payload.get("content_blocks") or [])
            if not raw_content_blocks and str(result_payload.get("answer") or "").strip():
                raw_content_blocks = [
                    {
                        "type": (
                            "message"
                            if payload.communication_channel == "text"
                            else "speech"
                        ),
                        "text": str(result_payload["answer"]),
                    }
                ]
            content_blocks = _public_content_blocks_payload(raw_content_blocks)
            for block_index, block in enumerate(content_blocks):
                if stream_v2 and block_index in sent_sequences:
                    continue
                block_type = str(block.get("type") or "")
                block_text = str(block.get("text") or "")
                if block_type == "sticker":
                    # Metadata is sent in a delta so older clients can ignore
                    # it while 0.8.4 clients render the asset after speech.
                    yield _sse(
                        "delta",
                        {
                            "text": "",
                            "block_index": block_index,
                            "block_type": "sticker",
                            "asset_id": str(block.get("asset_id") or ""),
                            "caption": str(block.get("caption") or ""),
                            "src": str(block.get("src") or ""),
                            "thumbnail_src": str(block.get("thumbnail_src") or ""),
                            "display_src": str(block.get("display_src") or ""),
                            "display_mime_type": str(block.get("display_mime_type") or ""),
                            "display_animated": bool(block.get("display_animated")),
                            "animated": bool(block.get("animated")),
                        },
                    )
                    continue
                for start in range(0, len(block_text), 24):
                    yield _sse(
                        "delta",
                        {
                            "text": block_text[start : start + 24],
                            "block_index": block_index,
                            "block_type": block_type,
                        },
                    )
            if result_payload.get("state_package"):
                state_event_payload: dict[str, Any] = {
                    "state_package": result_payload["state_package"],
                }
                safe_scene_state = _public_scene_state_payload(
                    result_payload.get("scene_state")
                )
                if safe_scene_state:
                    state_event_payload["scene_state"] = safe_scene_state
                safe_state_event = _public_state_event_payload(
                    result_payload.get("state_event")
                )
                if safe_state_event:
                    state_event_payload["state_event"] = safe_state_event
                if dialogue_v2:
                    state_event_payload.update(_dialogue_v2_metadata(result_payload))
                yield _sse("state", state_event_payload)
            internal_diagnostics = (
                result_payload.get("diagnostics")
                if isinstance(result_payload.get("diagnostics"), dict)
                else {}
            )
            raw_usage = (
                result_payload.get("usage")
                if isinstance(result_payload.get("usage"), dict)
                else {}
            )
            safe_usage: dict[str, int] = {}
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "prompt_tokens",
                "completion_tokens",
            ):
                value = raw_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    safe_usage[key] = value
            timings = internal_diagnostics.get("timings_ms")
            provider_calls = (
                timings.get("provider_http_calls")
                if isinstance(timings, dict)
                else 0
            )
            safe_usage["provider_calls"] = (
                provider_calls
                if isinstance(provider_calls, int)
                and not isinstance(provider_calls, bool)
                and provider_calls >= 0
                else 0
            )
            yield _sse(
                "done",
                {
                    "truncated": bool(result_payload.get("truncated")),
                    "usage": safe_usage,
                    "safety_category": result_payload.get("safety_category"),
                    "communication_channel": result_payload.get("communication_channel", "text"),
                    "expression_state": _normalize_expression_state(
                        result_payload.get("expression_state"),
                        str(result_payload.get("communication_channel") or "text"),
                    ),
                    "performance_id": (
                        result_payload.get("performance_id", "")
                        if result_payload.get("communication_channel") == "in_person" else ""
                    ),
                    "stage_motion": _normalize_stage_motion(
                        result_payload.get("stage_motion"),
                        str(result_payload.get("communication_channel") or "text"),
                    ),
                    "content_blocks": content_blocks,
                    "validation_disposition": str(
                        result_payload.get("validation_disposition")
                        or internal_diagnostics.get("validation_disposition")
                        or "accepted"
                    ),
                    "movement_status": _public_movement_status_payload(
                        result_payload.get("movement_status")
                        if isinstance(result_payload.get("movement_status"), dict)
                        else {
                            "status": str(
                                internal_diagnostics.get("state_update_status") or "none"
                            )
                        }
                    ),
                    "pending_rendezvous": _public_pending_rendezvous_payload(
                        result_payload.get("pending_rendezvous")
                    ),
                    "recovery_action": str(
                        result_payload.get("recovery_action") or "none"
                    ),
                    **({"timings_ms": latency_metrics(internal_diagnostics)} if stream_v2 else {}),
                    **(_dialogue_v2_metadata(result_payload) if dialogue_v2 else {}),
                },
            )

        async def guarded_events():
            has_committed = False
            try:
                async for packet in events():
                    has_committed = has_committed or packet.startswith("event: committed_block\n")
                    yield packet
            except PublicStoreUnavailable:
                yield _sse("error", {
                    "code": "public_database_unavailable", "retryable": not has_committed,
                    **({"partial": has_committed} if stream_v2 else {}),
                })

        return StreamingResponse(
            guarded_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/public/v1/chat/summarize")
    async def summarize(request: Request, payload: SummarizeRequest) -> dict[str, Any]:
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        if spec.provider_id != SUBSCRIPTION_PROVIDER and payload.reasoning_effort is not None:
            raise _error("reasoning_effort_not_supported", 422)
        claims = await open_request_credential(request, payload.credential, spec.provider_id)
        spec = relay_aware_spec(spec, claims)
        cache_id = "chat-summary:" + str(payload.request_id)
        request_body = _model_request_body(payload)
        subject = request.state.subject_hash
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        summary_control = None
        if owns_subject:
            summary_control = {"operation_id": cache_id, "cancel": asyncio.Event(), "task": None}
            background_summaries[subject] = summary_control
        try:
            claim_status, cached = await claim_owned_request(
                cache_id,
                subject,
                _request_hash(request_body),
            )
            request_claimed = claim_status == "claimed"
            owns_subject = owns_subject or request_claimed
            if claim_status == "conflict":
                raise _error("request_id_conflict", 409)
            if claim_status == "processing":
                raise _error("request_in_progress", 409)
            if claim_status == "completed" and cached is not None:
                if cached.get("terminal_error"):
                    raise _error(str(cached["terminal_error"]), 409)
                return {**cached, "idempotent_replay": True}
            if spec.provider_id == SUBSCRIPTION_PROVIDER:
                payload.reasoning_effort = subscription_broker.preflight(
                    str(claims["api_key"]), payload.model, payload.reasoning_effort,
                )
            # Summaries are extra model calls and therefore consume the same
            # daily 600-call budget as chat, but deliberately do not touch the
            # hourly 120-round bucket.
            await async_store.call("consume_limits", subject, [("chat_day", "day", 600)])
            generation = asyncio.create_task(chat_service.summarize(payload, spec, str(claims["api_key"])))
            if summary_control is not None:
                summary_control["task"] = generation
            started = time.monotonic()
            interrupted_code = (
                "subscription_result_unknown" if spec.provider_id == SUBSCRIPTION_PROVIDER
                else "generation_interrupted"
            )
            try:
                while not generation.done():
                    if (
                        (summary_control is not None and summary_control["cancel"].is_set())
                        or await request.is_disconnected()
                        or time.monotonic() - started >= 60
                    ):
                        generation.cancel()
                        await asyncio.gather(generation, return_exceptions=True)
                        request_claimed = False
                        await complete_owned_request(cache_id, {"terminal_error": interrupted_code})
                        raise _error(interrupted_code, 409)
                    await asyncio.wait({generation}, timeout=0.25)
                result = await generation
            except BaseException as exc:
                generation.cancel()
                await asyncio.gather(generation, return_exceptions=True)
                if request_claimed:
                    request_claimed = False
                    terminal_code = exc.code if isinstance(exc, SubscriptionError) else interrupted_code
                    await complete_owned_request(cache_id, {"terminal_error": terminal_code})
                if isinstance(exc, asyncio.CancelledError) and not asyncio.current_task().cancelling():
                    raise _error(interrupted_code, 409) from exc
                raise
            request_claimed = False
            await complete_owned_request(cache_id, result)
            return {**result, "idempotent_replay": False}
        except ProviderRequestError as exc:
            if request_claimed:
                await release_owned_request(cache_id)
            raise _error(exc.code, exc.status_code) from exc
        except Exception:
            if request_claimed:
                await release_owned_request(cache_id)
            raise
        finally:
            if summary_control is not None and background_summaries.get(subject) is summary_control:
                background_summaries.pop(subject, None)
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v1/feedback")
    async def feedback(request: Request, payload: FeedbackRequest) -> dict[str, Any]:
        body = redact_sensitive_text(payload.body, 1000)
        if not body:
            raise _error("feedback_empty", 422)
        ip_fingerprint = daily_ip_fingerprint(public_settings, _client_ip(request))
        risk = await async_store.call("record_feedback_attempt", request.state.subject_hash, ip_fingerprint)
        needs_turnstile = (
            await async_store.call("verification_required", request.state.subject_hash, "feedback")
            or risk["subject_attempts"] > 3
            or risk["ip_identities"] > 3
        )
        if needs_turnstile:
            verified = await verify_turnstile(
                public_settings,
                payload.turnstile_token,
                _client_ip(request),
                action="feedback",
            )
            if not verified:
                raise _error("turnstile_required", 403)
            await async_store.call("mark_verified", request.state.subject_hash, "feedback")
        await async_store.call("consume_limits",
            request.state.subject_hash,
            [("feedback_hour", "hour", 10), ("feedback_day", "day", 30)],
        )
        user_blocks = _feedback_blocks(payload.user_content_blocks, 2000)
        assistant_blocks = _feedback_blocks(payload.assistant_content_blocks, 1200)
        user_message = redact_sensitive_text(payload.user_message, 2000)
        if not user_message and user_blocks:
            user_message = redact_sensitive_text(
                "\n".join(
                    str(block.get("text") or "")
                    for block in user_blocks
                    if str(block.get("text") or "").strip()
                ),
                2000,
            )
        context = {
            "request_id": str(payload.request_id),
            # These values normally come from our own controls, but the
            # public request is still attacker-controlled.  Apply the same
            # credential redaction boundary as feedback text so a pasted key
            # cannot reach PostgreSQL, the admin view, or the mail outbox via
            # a forged model/provider field.
            "character_id": redact_sensitive_text(payload.character_id, 32),
            "provider": redact_sensitive_text(payload.provider, 24),
            "model": redact_sensitive_text(payload.model, 200),
            "app_version": public_settings.app_version,
            "data_version": public_settings.data_version,
            "user_message": user_message,
            "assistant_answer": _spoken_feedback_text(
                assistant_blocks,
                payload.assistant_answer,
                1200,
            ),
            "user_content_blocks": user_blocks,
            "assistant_content_blocks": assistant_blocks,
            "request_stage": redact_sensitive_text(payload.request_stage, 80),
            "error_code": redact_sensitive_text(payload.error_code, 80),
            "error_stage": _error_stage(payload.error_code, payload.error_stage)
            if payload.error_code or payload.error_stage
            else "",
            "degraded_services": [redact_sensitive_text(item, 80) for item in payload.degraded_services],
            "ui_surface": redact_sensitive_text(payload.ui_surface, 80),
        }
        if payload.chat_request_id:
            reference_id = str(payload.chat_request_id)
            context["chat_request_id"] = reference_id
            chat_result = None
            # Scene and arrival actions keep the public UUID but use separate
            # cache namespaces. All retain the same subject ownership check;
            # feedback never regenerates or replays a provider request.
            for cache_id, request_kind in (
                (reference_id, "chat"),
                ("presence-arrival-v2:" + reference_id, "presence_arrival"),
                ("presence-arrival:" + reference_id, "presence_arrival"),
                ("scene-action:" + reference_id, "scene_action"),
            ):
                candidate = await async_store.call(
                    "request_result", cache_id, request.state.subject_hash
                )
                if not isinstance(candidate, dict) or not candidate:
                    continue
                reaction = candidate.get("reaction")
                reaction = reaction if isinstance(reaction, dict) else {}
                character_id = str(candidate.get("character_id") or reaction.get("character_id") or "")
                channel = (
                    "in_person" if request_kind == "presence_arrival"
                    else str(candidate.get("communication_channel") or "")
                )
                if payload.character_id and character_id and payload.character_id != character_id:
                    continue
                if payload.request_stage in {"text", "in_person"} and channel and payload.request_stage != channel:
                    continue
                chat_result = candidate
                context["related_request_kind"] = request_kind
                if character_id:
                    context["character_id"] = character_id
                if channel:
                    context["request_stage"] = channel
                break
            if chat_result:
                context["generation_diagnostics"] = chat_result.get("diagnostics") or {}
                retrieval = chat_result.get("retrieval")
                if isinstance(retrieval, dict):
                    safe_retrieval = retrieval.get("diagnostics")
                    if isinstance(safe_retrieval, dict):
                        context["retrieval_diagnostics"] = safe_retrieval
                diagnostics = context["generation_diagnostics"]
                context["generation_outcome"] = chat_result.get("generation_outcome") or (
                    diagnostics.get("generation_class", "")
                    if context.get("related_request_kind") == "presence_arrival" and isinstance(diagnostics, dict)
                    else ""
                )
                context["response_adjustments"] = chat_result.get("response_adjustments") or []
                context["chat_error_code"] = chat_result.get("terminal_error") or ""
        try:
            public_code = await async_store.call("insert_feedback",
                subject_hash=request.state.subject_hash,
                ip_fingerprint=ip_fingerprint,
                body_text=body,
                context=context,
                qq_cipher=encrypt_qq(public_settings, payload.qq) if payload.qq else None,
            )
        except DuplicateFeedback:
            # The content is intentionally not inserted again.  Return an
            # unguessable receipt so the public surface does not reveal the
            # existence or identifier of the prior report.
            return {
                "feedback_code": "snow-suppressed-" + secrets.token_urlsafe(9),
                "retention_days": 0,
                "suppressed": True,
            }
        return {"feedback_code": public_code, "retention_days": 30, "suppressed": False}

    @app.get("/public/v1/health/live")
    async def live() -> dict[str, Any]:
        # Answer on the event loop: a probe flood must not queue for the
        # worker threads that static files and sync routes share.
        return {"status": "ok", "version": public_settings.app_version}

    @app.get("/public/v1/health/ready")
    async def ready(response: Response) -> dict[str, Any]:
        missing = public_settings.missing_production_secrets()
        database_ok = await async_store.call("schema_ready")
        data_status = chat_service.repository.status()
        manifest_version = ""
        try:
            manifest_version = str(
                json.loads((internal_settings.runtime_root / "manifest.json").read_text(encoding="utf-8")).get(
                    "data_version"
                )
                or ""
            )
        except (OSError, json.JSONDecodeError):
            pass
        required_data = (
            "lakehouse",
            "lexical_index",
            "vector_index",
            "personas",
            "graph",
            "character_views",
            "question_bank",
            "dialogue_profiles",
        )
        data_ok = all(data_status.get(name) for name in required_data) and (
            public_settings.allow_insecure_dev or manifest_version == public_settings.data_version
        )
        media_ok = media_startup_status.get("status") == "ok"
        stickers_ok = sticker_startup_status.get("status") == "ok"
        knowledge_ok = knowledge_review is not None
        ready_ok = (
            database_ok
            and not app.state.draining
            and data_ok
            and media_ok
            and stickers_ok
            and knowledge_ok
            and not missing
        )
        if not ready_ok:
            response.status_code = 503
        return {
            "status": "ok" if ready_ok else "not_ready",
            "database": "ok" if database_ok else "unavailable",
            "data": "ok" if data_ok else "unavailable",
            "data_artifacts": data_status,
            "media": "ok" if media_ok else "unavailable",
            "stickers": "ok" if stickers_ok else "unavailable",
            "knowledge_attribution": "ok" if knowledge_ok else "unavailable",
            "data_version": public_settings.data_version,
            "manifest_version": manifest_version,
            "missing_configuration": missing,
        }

    @app.get("/public/v1/health/full")
    async def full(response: Response) -> dict[str, Any]:
        dependencies = await asyncio.to_thread(chat_service.repository.dependency_health)
        database_ok = await async_store.call("health")
        media = chat_service.media.verify()
        stickers_status = chat_service.stickers.verify()
        degraded = sorted(service for service, service_status in dependencies.items() if service_status != "ok")
        if media.get("status") != "ok":
            degraded.append("media")
        if stickers_status.get("status") != "ok":
            degraded.append("stickers")
        if not database_ok:
            response.status_code = 503
        return {
            "status": "ok" if database_ok and not degraded else "degraded" if database_ok else "not_ready",
            "database": "ok" if database_ok else "unavailable",
            "dependencies": dependencies,
            "degraded_services": degraded,
            "data_version": public_settings.data_version,
            "media_version": public_settings.media_version,
            "media": media,
            "sticker_version": public_settings.sticker_version,
            "stickers": stickers_status,
            "feedback_email": await async_store.call("feedback_email_status") if database_ok else {},
            "generation_queue": chat_service.gate.snapshot(),
            "draining": app.state.draining,
        }

    @app.exception_handler(PublicSecurityError)
    async def security_error(request: Request, exc: PublicSecurityError):
        code, http_status = _public_security_disposition(exc)
        return _error_response(request, code, http_status, stage="state_validation")

    @app.exception_handler(ProviderRequestError)
    async def provider_error(request: Request, exc: ProviderRequestError):
        return _error_response(request, exc.code, exc.status_code, stage="provider")

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_error(request: Request, exc: RateLimitExceeded):
        return _rate_limit_response(request, exc)

    @app.exception_handler(GenerationBusy)
    async def generation_busy(request: Request, exc: GenerationBusy):
        return _error_response(
            request,
            str(exc),
            429,
            headers={"Retry-After": "2"},
            retry_after_seconds=2,
        )

    @app.exception_handler(CharacterUnavailable)
    async def character_unavailable(request: Request, _exc: CharacterUnavailable):
        return _error_response(request, "character_unavailable", 503, stage="character_data")

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request: Request, _exc: Exception):
        return _error_response(request, "invalid_request", 422, stage="content_validation")

    @app.exception_handler(MVPProviderError)
    async def mvp_provider_error(request: Request, _exc: MVPProviderError):
        return _error_response(request, "provider_request_failed", 502, stage="provider")

    app_root = Path(__file__).resolve().parents[2]
    frontend_path = app_root / "public_frontend"
    install_statistics_config(app, frontend_path)
    shared_design_path = app_root / "frontend" / "shared"
    immersive_assets_path = app_root / "frontend" / "assets" / "immersive"
    if public_settings.media_root.is_dir() and media_startup_status.get("status") == "ok":
        app.mount(
            f"/media/{public_settings.media_version}",
            StaticFiles(directory=public_settings.media_root, html=False),
            name="public_media",
        )
    if (
        public_settings.sticker_root.is_dir()
        and sticker_startup_status.get("status") == "ok"
    ):
        app.mount(
            f"/media/{public_settings.sticker_version}",
            StaticFiles(directory=public_settings.sticker_root, html=False),
            name="public_sticker_media",
        )
    if shared_design_path.is_dir():
        app.mount(
            "/shared",
            StaticFiles(directory=shared_design_path, html=False),
            name="shared_immersive_design",
        )
    if immersive_assets_path.is_dir():
        app.mount(
            "/assets/immersive",
            StaticFiles(directory=immersive_assets_path, html=False),
            name="shared_immersive_assets",
        )
    if frontend_path.is_dir():
        # Wiki-derived portraits are never copied into this directory or the
        # application image; the versioned media mount above remains separate.
        app.mount("/", StaticFiles(directory=frontend_path, html=True), name="public_frontend")

    return app


app = create_app()


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(
        "backend.snow_app.public_main:app",
        host=os.getenv("PUBLIC_API_HOST", "0.0.0.0"),
        port=int(os.getenv("PUBLIC_API_PORT", "8000")),
    )
