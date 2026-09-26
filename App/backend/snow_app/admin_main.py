"""Loopback-only feedback administration surface."""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from .config import PublicSettings
from .public_security import decrypt_qq
from .public_store import PublicStore


settings = PublicSettings.from_environment()
store = PublicStore(settings.database_url)
app = FastAPI(title="Project Snow Private Admin", docs_url=None, redoc_url=None)


class PublicFeedbackTriageRequest(BaseModel):
    status: Literal[
        "pending_triage",
        "planned",
        "resolved",
        "ignored",
        "open",
        "needs_verification",
        "fixed_verified",
        "not_reproduced",
        "duplicate",
        "superseded_by_architecture",
    ]
    note: str = Field(default="", max_length=2000)


def _authorized(request: Request) -> None:
    authorization = str(request.headers.get("Authorization") or "")
    token = authorization.removeprefix("Bearer ").strip()
    if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _conversation_parts(context: dict[str, Any]) -> dict[str, list[str]]:
    def texts(name: str, kinds: set[str]) -> list[str]:
        result = []
        for item in context.get(name) or []:
            if not isinstance(item, dict) or str(item.get("type") or "") not in kinds:
                continue
            value = str(item.get("text") or "").strip()
            if value:
                result.append(value)
        return result

    user_actions = texts("user_content_blocks", {"action"})
    user_dialogue = texts("user_content_blocks", {"message", "speech"})
    character_actions = texts("assistant_content_blocks", {"action"})
    character_dialogue = texts("assistant_content_blocks", {"message", "speech"})
    if not user_dialogue and str(context.get("user_message") or "").strip():
        user_dialogue = [str(context["user_message"]).strip()]
    if not character_dialogue and str(context.get("assistant_answer") or "").strip():
        character_dialogue = [str(context["assistant_answer"]).strip()]
    return {
        "user_actions": user_actions,
        "user_dialogue": user_dialogue,
        "character_actions": character_actions,
        "character_dialogue": character_dialogue,
    }


@app.get("/admin/v1/feedback")
def feedback(request: Request, limit: int = 100) -> dict[str, Any]:
    _authorized(request)
    rows = store.feedback_rows(limit)
    for row in rows:
        row["qq"] = "***" if row.pop("has_qq", False) else None
        row.pop("qq_cipher", None)
        row["conversation_parts"] = _conversation_parts(row.get("context") or {})
    return {"feedback": rows}


def _email_result(result: dict[str, Any]) -> dict[str, Any]:
    queued = bool(result.get("queued"))
    return {
        "status": "queued" if queued else (
            "already_sent" if result.get("email_status") == "sent" else "already_pending"
        ),
        "feedback_id": result["feedback_id"],
        "public_code": result["public_code"],
        "created_at": result["created_at"],
        "email_status": result["email_status"],
    }


@app.get("/admin/v1/feedback/email/status")
def feedback_email_status(request: Request) -> dict[str, Any]:
    _authorized(request)
    return {"status": store.feedback_email_status()}


@app.post("/admin/v1/feedback/latest/email")
def email_latest_feedback(
    request: Request,
    limit: int = 1,
    force: bool = False,
) -> dict[str, Any]:
    """Queue the newest feedback receipts without requiring a remembered code."""

    _authorized(request)
    rows = store.feedback_rows(max(1, min(int(limit), 20)))
    if not rows:
        raise HTTPException(status_code=404, detail="not_found")
    results = []
    for row in rows:
        queued = store.queue_feedback_email(str(row["feedback_id"]), force=force)
        if queued is not None:
            results.append(_email_result(queued))
    return {"feedback": results}


@app.post("/admin/v1/feedback/{feedback_id}/email")
def email_feedback(
    feedback_id: str,
    request: Request,
    force: bool = False,
) -> dict[str, Any]:
    _authorized(request)
    result = store.queue_feedback_email(feedback_id, force=force)
    if result is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _email_result(result)


@app.post("/admin/v1/feedback/{feedback_id}/triage")
def triage_feedback(
    feedback_id: str,
    request: Request,
    payload: PublicFeedbackTriageRequest,
) -> dict[str, Any]:
    _authorized(request)
    try:
        result = store.triage_feedback(feedback_id, payload.status, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="unsupported_status") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="not_found")
    return result


@app.get("/admin/v1/feedback/{feedback_id}/qq")
def feedback_qq(feedback_id: str, request: Request) -> dict[str, str | None]:
    _authorized(request)
    row = next((item for item in store.feedback_rows(500) if item["feedback_id"] == feedback_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="not_found")
    qq_cipher = row.get("qq_cipher")
    return {"qq": decrypt_qq(settings, qq_cipher) if qq_cipher else None}


@app.post("/admin/v1/cleanup")
def cleanup(request: Request) -> dict[str, Any]:
    _authorized(request)
    return {"deleted": store.cleanup()}


@app.get("/admin/v1/health")
def health(request: Request) -> dict[str, Any]:
    _authorized(request)
    return {"status": "ok" if store.health() else "not_ready", "data_version": settings.data_version}
