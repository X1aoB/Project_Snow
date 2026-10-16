"""Short-lived authority for one reaction to an already committed scene join.

The five-minute admission window is shorter than the ten-minute completed
request cache. An expired ticket can recover a result, never recreate a paid
claim after its cache entry is gone. Neither world nor sidecar schemas change.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError

from .public_contracts import PresenceArrivalV2Request, StrictModel
from .public_security import PublicSecurityError
from .session_context import _check_binding, _sign, _verify

_DOMAIN = b"project-snow-public-arrival-ticket-1\x00"
TICKET_SECONDS = 300


class ArrivalTicketExpired(PublicSecurityError):
    pass


class _Ticket(StrictModel):
    schema_version: Literal["public-arrival-ticket-1"]
    subject_binding: str
    character_id: str
    segment_id: UUID
    arrival_id: UUID
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_version: str
    issued_at: int = Field(strict=True)
    expires_at: int = Field(strict=True)
    state_key_id: str


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_arrival_ticket(settings, *, subject_hash, character_id, segment_id,
                        arrival_id, state_package, session_context_package) -> str:
    now = int(datetime.now(UTC).timestamp())
    return _sign(settings, {
        "schema_version": "public-arrival-ticket-1", "subject_binding": subject_hash,
        "character_id": character_id, "segment_id": str(segment_id), "arrival_id": str(arrival_id),
        "state_sha256": _digest(state_package), "context_sha256": _digest(session_context_package),
        "data_version": settings.data_version, "issued_at": now, "expires_at": now + TICKET_SECONDS,
    }, _DOMAIN, 2048)


def verify_arrival_ticket(settings, request: PresenceArrivalV2Request, subject_hash: str) -> None:
    raw = _verify(settings, request.arrival_ticket, _DOMAIN, 2048)
    _check_binding(raw, subject_hash, request.character_id)
    try:
        ticket = _Ticket.model_validate(raw)
    except ValidationError as exc:
        raise PublicSecurityError("Arrival ticket is malformed") from exc
    if (ticket.arrival_id != request.arrival_id or ticket.segment_id != request.segment_id
            or ticket.data_version != settings.data_version
            or ticket.state_sha256 != _digest(request.state_package)
            or ticket.context_sha256 != _digest(request.session_context_package)):
        raise PublicSecurityError("Arrival ticket scope does not match request")
    now = int(datetime.now(UTC).timestamp())
    if ticket.expires_at - ticket.issued_at != TICKET_SECONDS or ticket.issued_at > now:
        raise PublicSecurityError("Arrival ticket lifetime is invalid")
    if ticket.expires_at <= now:
        raise ArrivalTicketExpired("Arrival ticket has expired")
