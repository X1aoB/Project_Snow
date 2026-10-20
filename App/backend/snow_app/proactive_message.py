"""Opt-in rendezvous messages: one paid attempt, immutable recovery, no scene writes."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from .mvp_policy import MVP_CHARACTERS
from .public_contracts import ProactiveConsentRequest, ProactiveMessageRequest, StatePayload
from .public_providers import ProviderRequestError, provider_spec, simple_completion
from .public_security import PublicSecurityError, open_byok_credential, verify_state
from .public_store import PublicStoreUnavailable
from .public_subscription import PROVIDER as SUBSCRIPTION_PROVIDER, SubscriptionError
from .session_context import (_sign, _verify, verify_session_context,
                              session_context_transfer_size, CONTEXT_TRANSFER_LIMIT)

NOTICE_VERSION = "proactive-cost-1"
_CONSENT_DOMAIN = b"project-snow-proactive-cost-consent-1\x00"


def proactive_config(settings):
    return {"enabled": bool(settings.proactive_messages_enabled and settings.scene_actions_v2),
            "notice_version": NOTICE_VERSION, "max_per_day": 3, "max_per_rendezvous": 1,
            "recovery_only": True}


def _error(code, status=422):
    return HTTPException(status, detail={"code": code, "retryable": False})


def _day():
    return datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()


def _retention(day):
    # Day-end + 48 hours; unlike response text, these rows contain only safe
    # public IDs, irreversible subject identity, hashes and outcome states.
    return datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Asia/Hong_Kong")) + timedelta(days=3)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_hash(payload):
    body = payload.model_dump(mode="json", exclude={"credential", "consent_token"})
    if body.get("reasoning_effort") is None:
        body.pop("reasoning_effort", None)
    return _digest(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def authority_change(settings, subject, result, previous_token, character_id):
    """Project only a newly committed transition; replays cannot register IDs."""
    token = result.get("state_package")
    if not token or "." not in token:
        return None
    current = verify_state(settings, token)
    previous = verify_state(settings, previous_token)
    if current.get("subject_binding") != subject:
        raise PublicSecurityError("Rendezvous authority subject mismatch")
    day = current.get("schedule_date", "")
    if day != _day():
        return None
    old = (previous.get("pending_rendezvous") or {}).get(character_id) or {}
    new = (current.get("pending_rendezvous") or {}).get(character_id) or {}
    old_id, new_id = old.get("rendezvous_id"), new.get("rendezvous_id")
    status = (result.get("scene_action") or {}).get("status")
    created = new_id if status == "character_waiting" and new_id != old_id and not result.get("terminal_error") else None
    closed = [old_id] if old_id and old_id != new_id else []
    if not created and not closed:
        return None
    return {"subject": subject, "character": character_id, "day": day,
            "revision": int(current.get("revision") or 0), "created_id": created,
            "closed_ids": closed, "expires": _retention(day)}


class ProactiveMessageService:
    def __init__(self, settings, chat_service, dialogue_service, provider_http):
        self.settings, self.chat = settings, chat_service
        self.dialogue_service, self.http = dialogue_service, provider_http

    def prepare(self, payload, subject):
        if session_context_transfer_size(segment_id=payload.segment_id,
                session_context_package=payload.session_context_package) > CONTEXT_TRANSFER_LIMIT:
            raise PublicSecurityError("Proactive context transfer exceeds its size limit")
        self.chat.ensure_character_available(payload.character_id)
        world = StatePayload.model_validate(verify_state(self.settings, payload.state_package))
        context = verify_session_context(self.settings, payload.session_context_package,
            subject_hash=subject, character_id=payload.character_id, segment_id=payload.segment_id,
            catalog_version=self.dialogue_service().catalog_version)
        day = _day()
        pending = world.pending_rendezvous.get(payload.character_id)
        if (world.subject_binding != subject or world.data_version != self.settings.data_version
                or world.schedule_date != day or not pending or pending.schedule_date != day
                or pending.rendezvous_id != payload.rendezvous_id or not context
                or not context.scene_overlay or context.scene_overlay.schedule_date != day
                or context.scene_overlay.pending_rendezvous != pending
                or context.scene_overlay.communication_channel != "text"
                or context.scene_overlay.world_revision > world.revision):
            raise PublicSecurityError("Rendezvous package scope mismatch")
        expiry = datetime.fromisoformat(world.expires_at)
        if not expiry.tzinfo or expiry <= datetime.now(UTC):
            raise PublicSecurityError("Rendezvous package expired")
        return pending

    async def generate(self, payload, pending, spec, api_key, *, on_publication):
        character = next(item for item in MVP_CHARACTERS if item.character_id == payload.character_id)
        # No retrieval/embedding call or chat turn is created. Reviewed persona
        # source may shape tone, but cannot authorize a new event or movement.
        persona = await asyncio.to_thread(self.chat.repository.get_persona, payload.character_id)
        approved = persona if isinstance(persona, dict) and persona.get("review_status") == "verified" else {}
        profile = json.dumps({key: approved[key] for key in
            ("character_name", "summary", "persona_summary", "speaking_style", "personality") if key in approved},
            ensure_ascii=False)[:4000]
        on_publication()
        content, raw_usage = await simple_completion(spec, api_key, payload.model,
            system_prompt=(f"你是《尘白禁区》的{character.display_name}。只输出你通过通讯器主动发给分析员的一条简短中文消息。"
                "这是提醒，不是用户说过的话。不要代写分析员台词，不要行动描写，不移动任何人，不创建新约定、"
                "共同经历或原作事实。不声称分析员已到场。不输出JSON、标签或角色名前缀。"
                "只温和提醒已经成立的当天约定，不催促、不施压。以下已审阅资料仅供语气参考：" + profile),
            user_prompt=f"系统提醒事件：你今天已在{pending.location_name}等待约定。请发一条自然、简短的提醒。",
            max_tokens=400, timeout=120, client=self.http, reasoning_effort=payload.reasoning_effort)
        blocks, _answer, _truncated, safety = self.chat._public_generation_content(
            {"content_blocks": [{"type": "message", "text": content[:1200]}]}, "text")
        usage = {"provider_calls": 1, **{key: value for key, value in raw_usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000000}}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage.setdefault(key, None)
        if safety or not blocks:
            return {"status": "failed", "content_blocks": [], "usage": usage,
                    "model_called": True, "terminal_error": "role_guard_rejected"}
        return {"status": "completed", "content_blocks": blocks, "usage": usage,
                "model_called": True, "terminal_error": ""}


def install_proactive_routes(app, settings, async_store, chat_service, get_dialogue_service,
                             provider_http, broker, acquire_subject, release_subject,
                             active_leases):
    service = ProactiveMessageService(settings, chat_service, get_dialogue_service, provider_http)
    app.state.proactive_message_service = service

    async def session(payload, request):
        spec = provider_spec(payload.provider, settings.enabled_providers)
        if spec.provider_id != SUBSCRIPTION_PROVIDER and payload.reasoning_effort is not None:
            raise _error("reasoning_effort_not_supported")
        claims = open_byok_credential(settings, anonymous_id=request.state.anonymous_id,
            token=payload.credential, expected_provider=spec.provider_id,
            revoked_before=await async_store.call("credential_revoked_before", request.state.subject_hash))
        effort = payload.reasoning_effort
        if spec.provider_id == SUBSCRIPTION_PROVIDER:
            effort = broker.preflight(str(claims["api_key"]), payload.model, payload.reasoning_effort)
        return spec, claims, effort

    def consent_scope(payload, subject):
        return {"subject_binding": subject, "credential_sha256": _digest(payload.credential),
                "provider": payload.provider, "model": payload.model,
                "reasoning_effort": payload.reasoning_effort,
                "model_session_id": str(payload.model_session_id), "notice_version": payload.notice_version}

    @app.post("/public/v2/rendezvous/consent")
    async def proactive_consent(request: Request, payload: ProactiveConsentRequest):
        if not proactive_config(settings)["enabled"]:
            raise _error("proactive_messages_disabled", 503)
        if payload.accepted_cost_notice is not True:
            raise _error("proactive_cost_notice_required")
        _spec, claims, effort = await session(payload, request)
        now = int(datetime.now(UTC).timestamp())
        expires = min(now + 12 * 3600, int(claims["exp"]))
        token = _sign(settings, {"schema_version": "public-proactive-consent-1",
            **consent_scope(payload, request.state.subject_hash), "effective_effort": effort,
            "issued_at": now, "expires_at": expires},
            _CONSENT_DOMAIN, 2048)
        return {"consent_token": token, "notice_version": NOTICE_VERSION,
                "model_session_id": str(payload.model_session_id),
                "expires_at": datetime.fromtimestamp(expires, UTC).isoformat()}

    def response(payload, value=None, *, code="", replay=False):
        base = {"request_id": str(payload.request_id), "character_id": payload.character_id,
            "source": "proactive_reminder", "status": "unknown", "message_id": "",
            "content_blocks": [], "usage": {"provider_calls": None, "prompt_tokens": None,
                "completion_tokens": None, "total_tokens": None}, "model_called": None,
            "terminal_error": code or "proactive_result_unknown", "idempotent_replay": replay}
        if value:
            base.update({key: value[key] for key in base if key in value})
            if value.get("terminal_error") == "generation_interrupted":
                base.update(status="unknown", terminal_error="proactive_result_unknown")
        for key in ("provider_calls", "prompt_tokens", "completion_tokens", "total_tokens"):
            base["usage"].setdefault(key, None)
        base["idempotent_replay"] = replay
        return base

    @app.post("/public/v2/rendezvous/message")
    async def proactive_message(request: Request, payload: ProactiveMessageRequest):
        recovery = request.headers.get("X-Snow-Recovery-Only")
        if recovery not in {None, "1"}:
            raise _error("invalid_request")
        request.state.public_request_id = str(payload.request_id)
        subject, cache_id = request.state.subject_hash, "proactive:" + str(payload.request_id)
        digest = request_hash(payload)

        async def lookup():
            status, cached = await async_store.call("lookup_proactive", cache_id, subject, digest)
            if status == "conflict":
                raise _error("request_id_conflict", 409)
            if status == "processing":
                raise _error("request_in_progress", 409)
            if status == "completed":
                return response(payload, cached, replay=True)
            if status == "unknown":
                return response(payload, code="proactive_result_unavailable", replay=True)
            return None

        existing = await lookup()
        if existing is not None:
            return existing
        if recovery == "1":
            raise _error("request_result_unavailable", 410)
        if not proactive_config(settings)["enabled"]:
            raise _error("proactive_messages_disabled", 503)
        try:
            spec, claims, effort = await session(payload, request)
            raw = _verify(settings, payload.consent_token, _CONSENT_DOMAIN, 2048)
            scope = consent_scope(payload, subject)
            if (raw.get("schema_version") != "public-proactive-consent-1"
                    or raw.get("effective_effort") != effort
                    or any(not hmac.compare_digest(json.dumps(raw.get(key), sort_keys=True), json.dumps(value, sort_keys=True))
                           for key, value in scope.items())
                    or type(raw.get("expires_at")) is not int or raw["expires_at"] <= int(datetime.now(UTC).timestamp())):
                raise PublicSecurityError("Proactive consent scope mismatch")
            pending = service.prepare(payload, subject)
        except (PublicSecurityError, ValueError) as exc:
            raise _error("invalid_proactive_authorization") from exc
        owns_subject = await acquire_subject(subject, cache_id)
        reserved = False
        gate_reserved = False
        task = asyncio.current_task()
        try:
            await chat_service.gate.acquire()
            gate_reserved = True
            # Queueing can cross midnight or credential/consent expiry. Check
            # the exact same signed scope again immediately before admission.
            try:
                spec, claims, effort = await session(payload, request)
                if raw["expires_at"] <= int(datetime.now(UTC).timestamp()) or raw.get("effective_effort") != effort:
                    raise PublicSecurityError("Proactive consent expired while queued")
                pending = service.prepare(payload, subject)
            except (PublicSecurityError, ValueError) as exc:
                raise _error("invalid_proactive_authorization") from exc
            status, _ = await async_store.call("claim_proactive", cache_id, subject, digest,
                day=pending.schedule_date, rendezvous_id=payload.rendezvous_id,
                character_id=payload.character_id, expires=_retention(pending.schedule_date))
            if status == "existing":
                existing = await lookup()
                if existing is not None:
                    return existing
                raise _error("request_in_progress", 409)
            if status != "claimed":
                raise _error("request_id_conflict" if status == "conflict" else status,
                             429 if status == "proactive_daily_limit" else 409)
            reserved = True
            active_leases[cache_id] = task
            publication_started = False
            def mark_publication():
                nonlocal publication_started
                publication_started = True
            try:
                generated = await service.generate(payload.model_copy(update={"reasoning_effort": effort}),
                    pending, spec, str(claims["api_key"]), on_publication=mark_publication)
            except SubscriptionError as exc:
                generated = {"status": "unknown" if exc.submitted else "failed", "content_blocks": [],
                    "usage": {"provider_calls": 1 if exc.submitted else 0, "total_tokens": None},
                    "model_called": exc.submitted, "terminal_error": exc.code}
            except Exception:
                # Once publication may have started, neither a provider error
                # nor a local exception gives permission for another paid try.
                generated = {"status": "unknown" if publication_started else "failed",
                    "usage": {"provider_calls": 1 if publication_started else 0, "total_tokens": None},
                    "model_called": publication_started,
                    "terminal_error": "proactive_result_unknown" if publication_started else "proactive_preparation_failed"}
            result = response(payload, generated)
            if result["status"] == "completed":
                result["message_id"] = "proactive:" + str(payload.request_id)
            try:
                await async_store.call("complete_proactive", cache_id, result)
            except PublicStoreUnavailable:
                # A lost commit ACK may already contain the original answer.
                # This is a SELECT only; never overwrite it with an error.
                try:
                    saved = await lookup()
                    if saved is not None:
                        return saved
                except (PublicStoreUnavailable, HTTPException):
                    pass
                return response(payload, code="proactive_result_unknown")
            return result
        finally:
            # Canceled HTTP callers cannot release a reserved paid attempt.
            # The ordinary lease expiry makes its next lookup uncertain.
            if reserved and active_leases.get(cache_id) is task:
                active_leases.pop(cache_id, None)
            if gate_reserved:
                chat_service.gate.release()
            if owns_subject:
                await release_subject(subject, cache_id)
