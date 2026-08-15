"""Loopback-only feedback administration surface."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status

from .config import PublicSettings
from .public_security import decrypt_qq
from .public_store import PublicStore


settings = PublicSettings.from_environment()
store = PublicStore(settings.database_url)
app = FastAPI(title="Project Snow Private Admin", docs_url=None, redoc_url=None)


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
