"""Registration-free public API. It intentionally exposes no internal /api/v1 routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import PublicSettings, Settings
from .public_contracts import (
    ByokSessionRequest,
    ChatRequest,
    FeedbackRequest,
    ModelDiscoveryRequest,
    PresenceArrivalRequest,
    PresenceResolveRequest,
    PresenceTransitionRequest,
    SummarizeRequest,
)
from .public_providers import (
    PROVIDERS,
    ProviderHTTPPool,
    ProviderRequestError,
    discover_models,
    provider_spec,
)
from .public_security import (
    PublicSecurityError,
    daily_ip_fingerprint,
    encrypt_qq,
    input_safety_category,
    is_valid_anonymous_id,
    issue_byok_credential,
    new_anonymous_id,
    normalized_text,
    open_byok_credential,
    redact_sensitive_text,
    subject_hash,
    verify_turnstile,
)
from .mvp_service import MVPProviderError
from .public_service import CharacterUnavailable, GenerationBusy, PublicChatService
from .public_store import (
    DuplicateFeedback,
    PublicStore,
    PublicStoreUnavailable,
    RateLimitExceeded,
)
ANONYMOUS_COOKIE = "__Host-snow_anon"
DEVELOPMENT_ANONYMOUS_COOKIE = "snow_anon_dev"
MAX_BODY_BYTES = 64 * 1024
REQUEST_BODY_TIMEOUT_SECONDS = 10
LOGGER = logging.getLogger("snow.public")

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


def _client_ip(request: Request) -> str:
    settings = request.app.state.public_settings
    cloudflare_ip = str(request.headers.get("CF-Connecting-IP") or "").strip()
    if settings.trust_proxy_headers and cloudflare_ip:
        return cloudflare_ip[:64]
    return str(request.client.host if request.client else "unknown")[:64]


def _error(code: str, http_status: int, message: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message or code},
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    return "credential_invalid", 401


def create_app(
    public_settings: PublicSettings | None = None,
    internal_settings: Settings | None = None,
    store: PublicStore | None = None,
    chat_service: PublicChatService | None = None,
) -> FastAPI:
    public_settings = public_settings or PublicSettings.from_environment()
    internal_settings = internal_settings or Settings.from_environment()
    store = store or PublicStore(public_settings.database_url)
    provider_http = ProviderHTTPPool()
    chat_service = chat_service or PublicChatService(
        internal_settings,
        public_settings,
        provider_client=provider_http,
    )
    if chat_service is not None and getattr(chat_service, "provider_client", None) is None:
        chat_service.provider_client = provider_http
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
            store.create_schema()
            store.cleanup()
        try:
            yield
        finally:
            chat_service.close()
            await provider_http.close()

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
    app.state.provider_http = provider_http
    chat_jobs: dict[str, asyncio.Task[dict[str, Any]]] = {}
    active_subject_requests: dict[str, str] = {}
    active_subject_lock = asyncio.Lock()
    app.state.chat_jobs = chat_jobs
    app.state.active_subject_requests = active_subject_requests

    async def acquire_subject_generation(subject: str, operation_id: str) -> bool:
        """Reserve the subject unless this is a reconnect to the same operation."""

        async with active_subject_lock:
            owner = active_subject_requests.get(subject)
            if owner and owner != operation_id:
                raise HTTPException(
                    status_code=429,
                    detail={"code": "subject_generation_busy"},
                    headers={"Retry-After": "2"},
                )
            if owner == operation_id:
                return False
            active_subject_requests[subject] = operation_id
            return True

    async def release_subject_generation(subject: str, operation_id: str) -> None:
        async with active_subject_lock:
            if active_subject_requests.get(subject) == operation_id:
                active_subject_requests.pop(subject, None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(public_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Accept"],
    )

    @app.middleware("http")
    async def public_boundary(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            return JSONResponse(status_code=404, content={"detail": {"code": "route_not_public"}})
        request_host = str(request.url.hostname or "").casefold().rstrip(".")
        if request_host not in _allowed_hosts(public_settings):
            return JSONResponse(status_code=400, content={"detail": {"code": "host_rejected"}})
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
                return JSONResponse(status_code=403, content={"detail": {"code": "origin_rejected"}})
            content_type = str(request.headers.get("Content-Type") or "").split(";", 1)[0].casefold()
            if content_type != "application/json":
                return JSONResponse(status_code=415, content={"detail": {"code": "json_required"}})
            try:
                content_length = int(request.headers.get("Content-Length") or 0)
            except ValueError:
                content_length = MAX_BODY_BYTES + 1
            if content_length > MAX_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": {"code": "request_too_large"}})
            chunks: list[bytes] = []
            received = 0
            try:
                async with asyncio.timeout(REQUEST_BODY_TIMEOUT_SECONDS):
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > MAX_BODY_BYTES:
                            return JSONResponse(
                                status_code=413,
                                content={"detail": {"code": "request_too_large"}},
                            )
                        chunks.append(chunk)
            except TimeoutError:
                return JSONResponse(
                    status_code=408,
                    content={"detail": {"code": "request_read_timeout"}},
                )
            # Starlette dependencies may read the body after this middleware.
            # Cache only the bounded content so they do not consume the stream.
            request._body = b"".join(chunks)  # type: ignore[attr-defined]
            ip_subject = daily_ip_fingerprint(public_settings, _client_ip(request))
            if path == "/public/v1/feedback":
                ip_limits = [("feedback_ip_hour", "hour", 20), ("feedback_ip_day", "day", 50)]
            elif path.startswith("/public/v1/byok/"):
                ip_limits = [("byok_ip_hour", "hour", 30), ("byok_ip_day", "day", 100)]
            else:
                ip_limits = [("write_ip_hour", "hour", 240), ("write_ip_day", "day", 1000)]
            try:
                store.consume_limits(ip_subject, ip_limits)
            except RateLimitExceeded as exc:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": {
                            "code": "rate_limit_exceeded",
                            "scope": exc.scope,
                            "limit": exc.limit,
                        }
                    },
                    headers={"Retry-After": "60"},
                )
        response = await call_next(request)
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

    @app.exception_handler(PublicStoreUnavailable)
    async def database_unavailable(_request: Request, _exc: PublicStoreUnavailable):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "public_database_unavailable"}},
        )

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
                    }
                )
        return {
            "app_version": public_settings.app_version,
            "data_version": public_settings.data_version,
            "language": "zh-CN",
            "providers": enabled,
            "turnstile_site_key": public_settings.turnstile_site_key,
            "limits": {
                "input_characters": 2000,
                "output_characters": 1200,
                "chat_hour": 50,
                "chat_day": 200,
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
            "movement_catalog": chat_service.movement_catalog(),
            "feature_flags": {
                "joint_movement": True,
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
                "scope": "shared_daily",
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
        store.consume_limits(
            request.state.subject_hash,
            [("byok_session_hour", "hour", 10), ("byok_session_day", "day", 30)],
        )
        verification_required = store.verification_required(request.state.subject_hash, "byok")
        if verification_required:
            verified = await verify_turnstile(
                public_settings,
                payload.turnstile_token,
                _client_ip(request),
                action="byok-session",
            )
            if not verified:
                raise _error("turnstile_required", 403)
            store.mark_verified(request.state.subject_hash, "byok")
        credential, expires_at = issue_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            provider=spec.provider_id,
            api_key=payload.api_key,
        )
        return {
            "provider": spec.provider_id,
            "credential": credential,
            "expires_at": expires_at.isoformat(),
            "stored": False,
        }

    @app.post("/public/v1/byok/models")
    async def models(request: Request, payload: ModelDiscoveryRequest) -> dict[str, Any]:
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        claims = open_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            token=payload.credential,
            expected_provider=spec.provider_id,
        )
        store.consume_limits(request.state.subject_hash, [("model_discovery_hour", "hour", 20)])
        try:
            discovered = await discover_models(
                spec,
                str(claims["api_key"]),
                client=provider_http,
            )
        except ProviderRequestError as exc:
            raise _error(exc.code, exc.status_code) from exc
        return {"provider": spec.provider_id, "models": discovered, "manual_model_allowed": True}

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
            claim_status, cached = store.claim_request(
                cache_id,
                subject,
                _request_hash(request_body),
            )
            request_claimed = claim_status == "claimed"
            if claim_status == "conflict":
                raise _error("request_id_conflict", 409)
            if claim_status == "processing":
                raise _error("request_in_progress", 409)
            if claim_status == "completed" and cached is not None:
                return {**cached, "idempotent_replay": True}
            result = chat_service.transition_presence(payload, subject)
            store.complete_request(cache_id, result)
            return result
        except PublicSecurityError:
            if request_claimed:
                store.release_request(cache_id)
            raise
        except ValueError as exc:
            if request_claimed:
                store.release_request(cache_id)
            raise _error("invalid_presence_transition", 422) from exc
        except Exception:
            if request_claimed:
                store.release_request(cache_id)
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v1/presence/arrival")
    async def presence_arrival(request: Request, payload: PresenceArrivalRequest) -> dict[str, Any]:
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        claims = open_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            token=payload.credential,
            expected_provider=spec.provider_id,
        )
        cache_id = "presence-arrival:" + str(payload.arrival_id)
        request_body = payload.model_dump(mode="json", exclude={"credential"})
        subject = request.state.subject_hash
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        try:
            claim_status, cached = store.claim_request(
                cache_id,
                subject,
                _request_hash(request_body),
            )
            request_claimed = claim_status == "claimed"
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
                try:
                    store.consume_limits(
                        request.state.subject_hash,
                        [("chat_hour", "hour", 50), ("chat_day", "day", 200)],
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
                    result = await chat_service.finish_presence_arrival(
                        prepared,
                        payload,
                        subject,
                        spec,
                        str(claims["api_key"]),
                    )
            store.complete_request(cache_id, result)
            return result
        except PublicSecurityError:
            if request_claimed:
                store.release_request(cache_id)
            raise
        except ValueError as exc:
            if request_claimed:
                store.release_request(cache_id)
            raise _error("invalid_presence_request", 422) from exc
        except Exception:
            if request_claimed:
                store.release_request(cache_id)
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v1/chat/stream")
    async def chat_stream(request: Request, payload: ChatRequest):
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        public_model = redact_sensitive_text(payload.model, 200)
        chat_service.ensure_character_available(payload.character_id)
        try:
            canonical_blocks = chat_service.validate_content_blocks(
                payload.content_blocks,
                payload.communication_channel,
            )
        except ValueError as exc:
            raise _error("sticker_unavailable", 422) from exc
        for original, canonical in zip(payload.content_blocks, canonical_blocks):
            if canonical.get("type") == "sticker":
                original.caption = str(canonical.get("caption") or "")
        claims = open_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            token=payload.credential,
            expected_provider=spec.provider_id,
        )
        request_body = payload.model_dump(mode="json", exclude={"credential"})
        request_id = str(payload.request_id)
        subject = request.state.subject_hash
        owns_subject = await acquire_subject_generation(subject, request_id)
        try:
            claim_status, cached = store.claim_request(
                request_id,
                subject,
                _request_hash(request_body),
            )
        except Exception:
            if owns_subject:
                await release_subject_generation(subject, request_id)
            raise
        if claim_status == "conflict":
            if owns_subject:
                await release_subject_generation(subject, request_id)
            raise _error("request_id_conflict", 409)
        idempotent_replay = claim_status in {"processing", "completed"}
        result: dict[str, Any] | None = None
        job: asyncio.Task[dict[str, Any]] | None = None
        if claim_status == "completed" and cached is not None:
            result = {**cached, "idempotent_replay": True}
            if owns_subject:
                await release_subject_generation(subject, request_id)
        elif claim_status == "processing":
            job = chat_jobs.get(request_id)
            if job is None:
                # The original worker may be running in another process (or
                # the process may have restarted).  The durable claim still
                # prevents a second provider call; the client can retry the
                # same UUID until the cached terminal result is available.
                if owns_subject:
                    await release_subject_generation(subject, request_id)
                raise _error("request_in_progress", 409)
        else:
            try:
                store.consume_limits(
                    request.state.subject_hash,
                    [("chat_hour", "hour", 50), ("chat_day", "day", 200)],
                )
            except Exception:
                store.release_request(request_id)
                await release_subject_generation(subject, request_id)
                raise

            unsafe_category = input_safety_category(payload.message)
            generation_reserved = False
            if not unsafe_category:
                try:
                    await chat_service.gate.acquire()
                    generation_reserved = True
                except BaseException:
                    # Admission happens before the StreamingResponse exists so
                    # queue saturation can carry an HTTP Retry-After header.
                    # A client disconnect while waiting must also release the
                    # durable claim and per-subject owner.
                    store.release_request(request_id)
                    await release_subject_generation(subject, request_id)
                    raise

            async def run_generation() -> dict[str, Any]:
                started_at = time.monotonic()
                exception_type = ""
                terminal_stage = "complete"
                try:
                    if unsafe_category:
                        generated = chat_service.policy_rejection(
                            payload,
                            request.state.subject_hash,
                            spec,
                            unsafe_category,
                        )
                    else:
                        generated = await chat_service.chat(
                            payload,
                            request.state.subject_hash,
                            spec,
                            str(claims["api_key"]),
                            gate_reserved=generation_reserved,
                        )
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
                    terminal_stage = "generation"
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
                        "diagnostics": {"error_stage": "generation"},
                    }
                try:
                    store.complete_request(request_id, generated)
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
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                    )
                )
                return generated

            async def run_reserved_generation() -> dict[str, Any]:
                try:
                    return await run_generation()
                finally:
                    if generation_reserved:
                        chat_service.gate.release()

            try:
                job = asyncio.create_task(
                    run_reserved_generation(), name=f"public-chat:{request_id}"
                )
            except Exception:
                if generation_reserved:
                    chat_service.gate.release()
                store.release_request(request_id)
                await release_subject_generation(subject, request_id)
                raise
            chat_jobs[request_id] = job

            def forget_job(completed: asyncio.Task[dict[str, Any]]) -> None:
                chat_jobs.pop(request_id, None)
                if active_subject_requests.get(subject) == request_id:
                    active_subject_requests.pop(subject, None)
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
            resolved = result
            while resolved is None and job is not None:
                try:
                    resolved = await asyncio.wait_for(asyncio.shield(job), timeout=4.0)
                except TimeoutError:
                    yield ": heartbeat\n\n"
            if resolved is None:
                yield _sse("error", {"code": "generation_failed", "retryable": True})
                return
            result_payload = resolved
            if result_payload.get("terminal_error"):
                terminal_error = str(result_payload["terminal_error"])
                yield _sse(
                    "error",
                    {
                        "code": terminal_error,
                        "retryable": True,
                        "idempotent_replay": idempotent_replay,
                        **(
                            {"retry_after_seconds": 2}
                            if terminal_error.startswith("generation_queue_")
                            else {}
                        ),
                    },
                )
                return
            content_blocks = list(result_payload.get("content_blocks") or [])
            if not content_blocks and str(result_payload.get("answer") or "").strip():
                content_blocks = [
                    {
                        "type": (
                            "message"
                            if payload.communication_channel == "text"
                            else "speech"
                        ),
                        "text": str(result_payload["answer"]),
                    }
                ]
            for block_index, block in enumerate(content_blocks):
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
                if result_payload.get("scene_state"):
                    state_event_payload["scene_state"] = result_payload["scene_state"]
                if result_payload.get("state_event"):
                    state_event_payload["state_event"] = result_payload["state_event"]
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
                min(public_settings.max_provider_calls_per_action, provider_calls)
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
                    "content_blocks": content_blocks,
                    "validation_disposition": str(
                        result_payload.get("validation_disposition")
                        or internal_diagnostics.get("validation_disposition")
                        or "accepted"
                    ),
                    "movement_status": (
                        result_payload.get("movement_status")
                        if isinstance(result_payload.get("movement_status"), dict)
                        else {
                            "status": str(
                                internal_diagnostics.get("state_update_status") or "none"
                            )
                        }
                    ),
                    "recovery_action": str(
                        result_payload.get("recovery_action") or "none"
                    ),
                },
            )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/public/v1/chat/summarize")
    async def summarize(request: Request, payload: SummarizeRequest) -> dict[str, Any]:
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        claims = open_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            token=payload.credential,
            expected_provider=spec.provider_id,
        )
        cache_id = "chat-summary:" + str(payload.request_id)
        request_body = payload.model_dump(mode="json", exclude={"credential"})
        subject = request.state.subject_hash
        owns_subject = await acquire_subject_generation(subject, cache_id)
        request_claimed = False
        try:
            claim_status, cached = store.claim_request(
                cache_id,
                subject,
                _request_hash(request_body),
            )
            request_claimed = claim_status == "claimed"
            if claim_status == "conflict":
                raise _error("request_id_conflict", 409)
            if claim_status == "processing":
                raise _error("request_in_progress", 409)
            if claim_status == "completed" and cached is not None:
                return {**cached, "idempotent_replay": True}
            # Summaries are extra model calls and therefore consume the same
            # daily 200-call budget as chat, but deliberately do not touch the
            # hourly 50-round bucket.
            store.consume_limits(subject, [("chat_day", "day", 200)])
            result = await chat_service.summarize(payload, spec, str(claims["api_key"]))
            store.complete_request(cache_id, result)
            return {**result, "idempotent_replay": False}
        except ProviderRequestError as exc:
            if request_claimed:
                store.release_request(cache_id)
            raise _error(exc.code, exc.status_code) from exc
        except Exception:
            if request_claimed:
                store.release_request(cache_id)
            raise
        finally:
            if owns_subject:
                await release_subject_generation(subject, cache_id)

    @app.post("/public/v1/feedback")
    async def feedback(request: Request, payload: FeedbackRequest) -> dict[str, Any]:
        body = redact_sensitive_text(payload.body, 1000)
        if not body:
            raise _error("feedback_empty", 422)
        ip_fingerprint = daily_ip_fingerprint(public_settings, _client_ip(request))
        risk = store.record_feedback_attempt(request.state.subject_hash, ip_fingerprint)
        needs_turnstile = (
            store.verification_required(request.state.subject_hash, "feedback")
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
            store.mark_verified(request.state.subject_hash, "feedback")
        store.consume_limits(
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
            "degraded_services": [redact_sensitive_text(item, 80) for item in payload.degraded_services],
            "ui_surface": redact_sensitive_text(payload.ui_surface, 80),
        }
        if payload.chat_request_id:
            chat_result = store.request_result(
                str(payload.chat_request_id), request.state.subject_hash
            )
            context["chat_request_id"] = str(payload.chat_request_id)
            if chat_result:
                context["generation_diagnostics"] = chat_result.get("diagnostics") or {}
                context["generation_outcome"] = chat_result.get("generation_outcome") or ""
                context["response_adjustments"] = chat_result.get("response_adjustments") or []
                context["chat_error_code"] = chat_result.get("terminal_error") or ""
        try:
            public_code = store.insert_feedback(
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
    def live() -> dict[str, Any]:
        return {"status": "ok", "version": public_settings.app_version}

    @app.get("/public/v1/health/ready")
    def ready(response: Response) -> dict[str, Any]:
        missing = public_settings.missing_production_secrets()
        database_ok = store.health()
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
    def full(response: Response) -> dict[str, Any]:
        dependencies = chat_service.repository.dependency_health()
        database_ok = store.health()
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
            "feedback_email": store.feedback_email_status() if database_ok else {},
        }

    @app.exception_handler(PublicSecurityError)
    async def security_error(_request: Request, exc: PublicSecurityError):
        code, http_status = _public_security_disposition(exc)
        return JSONResponse(
            status_code=http_status,
            content={"detail": {"code": code}},
        )

    @app.exception_handler(ProviderRequestError)
    async def provider_error(_request: Request, exc: ProviderRequestError):
        return JSONResponse(status_code=exc.status_code, content={"detail": {"code": exc.code}})

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_error(_request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": {"code": "rate_limit_exceeded", "scope": exc.scope, "limit": exc.limit}},
            headers={"Retry-After": "60"},
        )

    @app.exception_handler(GenerationBusy)
    async def generation_busy(_request: Request, exc: GenerationBusy):
        return JSONResponse(
            status_code=429,
            content={"detail": {"code": str(exc), "retry_after_seconds": 2}},
            headers={"Retry-After": "2"},
        )

    @app.exception_handler(CharacterUnavailable)
    async def character_unavailable(_request: Request, _exc: CharacterUnavailable):
        return JSONResponse(status_code=503, content={"detail": {"code": "character_unavailable"}})

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(_request: Request, _exc: Exception):
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request"}})

    @app.exception_handler(MVPProviderError)
    async def mvp_provider_error(_request: Request, _exc: MVPProviderError):
        return JSONResponse(status_code=502, content={"detail": {"code": "provider_request_failed"}})

    app_root = Path(__file__).resolve().parents[2]
    frontend_path = app_root / "public_frontend"
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
