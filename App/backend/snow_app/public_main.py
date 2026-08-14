"""Registration-free public API. It intentionally exposes no internal /api/v1 routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

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
    SummarizeRequest,
)
from .public_providers import PROVIDERS, ProviderRequestError, discover_models, provider_spec
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


ANONYMOUS_COOKIE = "snow_anon"
MAX_BODY_BYTES = 64 * 1024


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


def create_app(
    public_settings: PublicSettings | None = None,
    internal_settings: Settings | None = None,
    store: PublicStore | None = None,
    chat_service: PublicChatService | None = None,
) -> FastAPI:
    public_settings = public_settings or PublicSettings.from_environment()
    internal_settings = internal_settings or Settings.from_environment()
    store = store or PublicStore(public_settings.database_url)
    chat_service = chat_service or PublicChatService(internal_settings, public_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if store.engine is not None and public_settings.auto_create_schema:
            store.create_schema()
            store.cleanup()
        yield

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
        anonymous_id = str(request.cookies.get(ANONYMOUS_COOKIE) or "").strip()
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
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse(status_code=413, content={"detail": {"code": "request_too_large"}})
        response = await call_next(request)
        if new_cookie:
            response.set_cookie(
                ANONYMOUS_COOKIE,
                anonymous_id,
                max_age=30 * 24 * 60 * 60,
                httponly=True,
                secure=not public_settings.allow_insecure_dev,
                samesite="lax",
                path="/",
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.exception_handler(PublicStoreUnavailable)
    async def database_unavailable(_request: Request, _exc: PublicStoreUnavailable):
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": "public_database_unavailable"}},
        )

    @app.get("/public/v1/config")
    def config() -> dict[str, Any]:
        enabled = []
        for provider_id in public_settings.enabled_providers:
            spec = PROVIDERS.get(provider_id)
            if spec:
                enabled.append(
                    {
                        "provider_id": spec.provider_id,
                        "display_name": spec.display_name,
                        "documentation_url": spec.documentation_url,
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
            "source_links": {
                "project_snow": "https://github.com/X1aoB/Project_Snow",
                "mywebsite": "https://github.com/X1aoB/MyWebsite",
                "releases": "https://github.com/X1aoB/Project_Snow/releases",
            },
        }

    @app.get("/public/v1/characters")
    def characters() -> dict[str, Any]:
        return {"characters": chat_service.characters(), "count": 22}

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
            discovered = await discover_models(spec, str(claims["api_key"]))
        except ProviderRequestError as exc:
            raise _error(exc.code, exc.status_code) from exc
        return {"provider": spec.provider_id, "models": discovered, "manual_model_allowed": True}

    @app.post("/public/v1/chat/stream")
    async def chat_stream(request: Request, payload: ChatRequest):
        spec = provider_spec(payload.provider, public_settings.enabled_providers)
        claims = open_byok_credential(
            public_settings,
            anonymous_id=request.state.anonymous_id,
            token=payload.credential,
            expected_provider=spec.provider_id,
        )
        request_body = payload.model_dump(mode="json", exclude={"credential"})
        request_id = str(payload.request_id)
        claim_status, cached = store.claim_request(
            request_id,
            request.state.subject_hash,
            _request_hash(request_body),
        )
        if claim_status == "conflict":
            raise _error("request_id_conflict", 409)
        if claim_status == "processing":
            raise _error("request_in_progress", 409)
        if claim_status == "completed" and cached is not None:
            result = {**cached, "idempotent_replay": True}
        else:
            try:
                store.consume_limits(
                    request.state.subject_hash,
                    [("chat_hour", "hour", 50), ("chat_day", "day", 200)],
                )
                unsafe_category = input_safety_category(payload.message)
                if unsafe_category:
                    # The rejection follows the same credential, idempotency,
                    # and quota path, so blocked input cannot bypass controls.
                    result = {
                        "request_id": str(payload.request_id),
                        "character_id": payload.character_id,
                        "provider": spec.provider_id,
                        "model": payload.model,
                        "answer": "抱歉，这个方向我不能继续提供具体指导。我们可以换成安全、合法且不伤害他人的话题。",
                        "truncated": False,
                        "state_package": payload.state_package,
                        "degraded_services": [],
                        "retrieval": {},
                        "usage": {},
                        "safety_category": unsafe_category,
                    }
                else:
                    result = await chat_service.chat(
                        payload,
                        request.state.subject_hash,
                        spec,
                        str(claims["api_key"]),
                    )
                store.complete_request(request_id, result)
            except Exception:
                store.release_request(request_id)
                raise

        async def events():
            yield _sse(
                "meta",
                {
                    "request_id": result["request_id"],
                    "character_id": result["character_id"],
                    "provider": result["provider"],
                    "model": result["model"],
                    "data_version": public_settings.data_version,
                    "idempotent_replay": bool(result.get("idempotent_replay")),
                },
            )
            answer = str(result.get("answer") or "")
            for start in range(0, len(answer), 24):
                yield _sse("delta", {"text": answer[start : start + 24]})
            if result.get("state_package"):
                yield _sse("state", {"state_package": result["state_package"]})
            yield _sse(
                "done",
                {
                    "truncated": bool(result.get("truncated")),
                    "degraded_services": result.get("degraded_services") or [],
                    "usage": result.get("usage") or {},
                    "safety_category": result.get("safety_category"),
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
        store.consume_limits(request.state.subject_hash, [("chat_summary_day", "day", 200)])
        try:
            return await chat_service.summarize(payload, spec, str(claims["api_key"]))
        except ProviderRequestError as exc:
            raise _error(exc.code, exc.status_code) from exc

    @app.post("/public/v1/feedback")
    async def feedback(request: Request, payload: FeedbackRequest) -> dict[str, Any]:
        body = normalized_text(payload.body, 1000)
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
        context = {
            "request_id": str(payload.request_id),
            "character_id": payload.character_id,
            "provider": payload.provider,
            "model": payload.model,
            "app_version": public_settings.app_version,
            "data_version": public_settings.data_version,
            "user_message": normalized_text(payload.user_message, 2000),
            "assistant_answer": normalized_text(payload.assistant_answer, 1200),
            "request_stage": payload.request_stage,
            "error_code": payload.error_code,
            "degraded_services": payload.degraded_services,
            "ui_surface": payload.ui_surface,
        }
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
        ready_ok = database_ok and data_ok and not missing
        if not ready_ok:
            response.status_code = 503
        return {
            "status": "ok" if ready_ok else "not_ready",
            "database": "ok" if database_ok else "unavailable",
            "data": "ok" if data_ok else "unavailable",
            "data_artifacts": data_status,
            "data_version": public_settings.data_version,
            "manifest_version": manifest_version,
            "missing_configuration": missing,
        }

    @app.get("/public/v1/health/full")
    def full(response: Response) -> dict[str, Any]:
        dependencies = chat_service.repository.dependency_health()
        database_ok = store.health()
        degraded = sorted(service for service, service_status in dependencies.items() if service_status != "ok")
        if not database_ok:
            response.status_code = 503
        return {
            "status": "ok" if database_ok and not degraded else "degraded" if database_ok else "not_ready",
            "database": "ok" if database_ok else "unavailable",
            "dependencies": dependencies,
            "degraded_services": degraded,
            "data_version": public_settings.data_version,
        }

    @app.exception_handler(PublicSecurityError)
    async def security_error(_request: Request, exc: PublicSecurityError):
        return JSONResponse(
            status_code=401,
            content={"detail": {"code": "credential_invalid", "message": str(exc)}},
        )

    @app.exception_handler(ProviderRequestError)
    async def provider_error(_request: Request, exc: ProviderRequestError):
        return JSONResponse(status_code=exc.status_code, content={"detail": {"code": exc.code}})

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_error(_request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": {"code": "rate_limit_exceeded", "scope": exc.scope, "limit": exc.limit}},
        )

    @app.exception_handler(GenerationBusy)
    async def generation_busy(_request: Request, exc: GenerationBusy):
        return JSONResponse(status_code=503, content={"detail": {"code": str(exc)}})

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

    frontend_path = Path(__file__).resolve().parents[2] / "public_frontend"
    if frontend_path.is_dir():
        # The public frontend contains no Wiki-derived image assets. Portraits
        # fall back to text until each asset has a separately verified license.
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
