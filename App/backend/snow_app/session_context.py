"""Bounded, subject-bound dialogue sidecars without server session storage."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .config import PublicSettings
from .public_contracts import SceneOverlay, SessionContext, SessionMemory
from .public_security import PublicSecurityError

CONTEXT_TOKEN_LIMIT = 8192
MEMORY_PROOF_LIMIT = 1536
_CONTEXT_DOMAIN = b"project-snow-public-session-context-1\x00"
_MEMORY_DOMAIN = b"project-snow-public-session-memory-1\x00"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _encode(result) != value:
        raise ValueError("noncanonical sidecar encoding")
    return result


def _sign(settings: PublicSettings, payload: dict[str, Any], domain: bytes, limit: int) -> str:
    if not settings.state_hmac_key:
        raise PublicSecurityError("PUBLIC_STATE_HMAC_KEY is not configured")
    value = {**payload, "state_key_id": settings.state_key_id}
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(settings.state_hmac_key, domain + body, hashlib.sha256).digest()
    token = _encode(body) + "." + _encode(signature)
    if len(token) > limit:
        raise PublicSecurityError("Session context exceeds its size limit")
    return token


def _verify(settings: PublicSettings, token: str, domain: bytes, limit: int) -> dict[str, Any]:
    if not settings.state_hmac_key:
        raise PublicSecurityError("PUBLIC_STATE_HMAC_KEY is not configured")
    if not isinstance(token, str) or len(token) > limit:
        raise PublicSecurityError("Session context exceeds its size limit")
    try:
        encoded, signature_text = token.split(".")
        body, signature = _decode(encoded), _decode(signature_text)
        payload = json.loads(body)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise PublicSecurityError("Session context is malformed") from exc
    if not isinstance(payload, dict):
        raise PublicSecurityError("Session context must contain an object")
    key_id = str(payload.get("state_key_id") or "")
    keys = []
    if key_id == settings.state_key_id:
        keys.append(settings.state_hmac_key)
    if settings.state_hmac_previous_key and key_id == settings.state_previous_key_id:
        keys.append(settings.state_hmac_previous_key)
    if not any(
        hmac.compare_digest(signature, hmac.new(key, domain + body, hashlib.sha256).digest())
        for key in keys
    ):
        raise PublicSecurityError("Session context signature is invalid")
    return payload


def _check_binding(payload: dict[str, Any], subject: str, character_id: str) -> None:
    if not hmac.compare_digest(str(payload.get("subject_binding") or ""), subject):
        raise PublicSecurityError("Session context belongs to another anonymous session")
    if str(payload.get("character_id") or "") != character_id:
        raise PublicSecurityError("Session context scope does not match request")


def new_session_context(
    settings: PublicSettings, *, subject_hash: str, character_id: str,
    segment_id: UUID | str, catalog_version: str,
) -> SessionContext:
    return SessionContext(
        subject_binding=subject_hash, character_id=character_id, segment_id=segment_id,
        data_version=settings.data_version, catalog_version=catalog_version,
        issued_at=datetime.now(UTC).isoformat(), state_key_id=settings.state_key_id,
    )


def sign_session_context(settings: PublicSettings, value: SessionContext | dict[str, Any]) -> str:
    # model_copy(update=...) intentionally skips Pydantic validation. Reparse
    # a plain payload here so no unvalidated update can gain a signature.
    context = SessionContext.model_validate(value.model_dump(mode="json") if isinstance(value, SessionContext) else value)
    return _sign(settings, context.model_dump(mode="json"), _CONTEXT_DOMAIN, CONTEXT_TOKEN_LIMIT)


def verify_session_context(
    settings: PublicSettings, token: str, *, subject_hash: str, character_id: str,
    segment_id: UUID | str, data_version: str | None = None, catalog_version: str | None = None,
) -> SessionContext | None:
    if not token:
        return None
    payload = _verify(settings, token, _CONTEXT_DOMAIN, CONTEXT_TOKEN_LIMIT)
    _check_binding(payload, subject_hash, character_id)
    try:
        context = SessionContext.model_validate(payload)
    except ValidationError as exc:
        raise PublicSecurityError("Session context is malformed") from exc
    if str(context.segment_id) != str(segment_id):
        raise PublicSecurityError("Session context scope does not match request")
    if context.data_version != (data_version or settings.data_version):
        raise PublicSecurityError("Session context version is not supported")
    if catalog_version is not None and context.catalog_version != catalog_version:
        raise PublicSecurityError("Session context version is not supported")
    return context


def sign_session_memory(
    settings: PublicSettings, memory: SessionMemory | dict[str, Any], *, subject_hash: str,
    character_id: str,
) -> str:
    value = SessionMemory.model_validate(memory.model_dump(mode="json") if isinstance(memory, SessionMemory) else memory)
    return _sign(settings, {
        "schema_version": "public-session-memory-1", "subject_binding": subject_hash,
        "character_id": character_id, "memory": value.model_dump(mode="json"),
    }, _MEMORY_DOMAIN, MEMORY_PROOF_LIMIT)


def verify_session_memory(
    settings: PublicSettings, token: str, *, subject_hash: str, character_id: str,
) -> SessionMemory | None:
    if not token:
        return None
    payload = _verify(settings, token, _MEMORY_DOMAIN, MEMORY_PROOF_LIMIT)
    _check_binding(payload, subject_hash, character_id)
    if payload.get("schema_version") != "public-session-memory-1":
        raise PublicSecurityError("Session context version is not supported")
    try:
        return SessionMemory.model_validate(payload["memory"])
    except (ValidationError, KeyError) as exc:
        raise PublicSecurityError("Session context is malformed") from exc


__all__ = [
    "SessionContext", "SceneOverlay", "SessionMemory", "new_session_context",
    "sign_session_context", "verify_session_context", "sign_session_memory", "verify_session_memory",
]
