"""Anonymous voice-preference survey routes.  Additive to public v1, off by default."""
from __future__ import annotations

from fastapi import HTTPException, Request

from .mvp_policy import MVP_CHARACTERS
from .public_contracts import VoiceSurveySubmit
from .public_security import redact_sensitive_text
from .voice_survey_store import VOICE_SURVEY_RETENTION_DAYS

SURVEY_VERSION = "voice-survey-1"
_KNOWN_CHARACTERS = frozenset(item.character_id for item in MVP_CHARACTERS)


def voice_survey_config(settings) -> dict:
    return {"enabled": bool(settings.voice_survey_v1), "survey_version": SURVEY_VERSION}


def _error(code, status):
    return HTTPException(status, detail={"code": code, "retryable": False})


def install_voice_survey_routes(app, settings, async_store) -> None:
    def require_enabled():
        if not settings.voice_survey_v1:
            raise _error("voice_survey_disabled", 404)

    @app.get("/public/v1/voice-survey")
    async def voice_survey_status(request: Request) -> dict:
        require_enabled()
        answered = await async_store.call("voice_survey_answered", request.state.subject_hash)
        return {"survey_version": SURVEY_VERSION, "answered": answered}

    @app.post("/public/v1/voice-survey")
    async def voice_survey_submit(request: Request, payload: VoiceSurveySubmit) -> dict:
        require_enabled()
        if not _KNOWN_CHARACTERS.issuperset(payload.characters):
            raise _error("invalid_request", 422)
        await async_store.call("consume_limits", request.state.subject_hash,
                               [("voice_survey_hour", "hour", 10), ("voice_survey_day", "day", 20)])
        answers = payload.model_dump(exclude={"survey_version", "comment"})
        await async_store.call("save_voice_survey", request.state.subject_hash, SURVEY_VERSION, answers,
                               redact_sensitive_text(payload.comment, 200))
        return {"saved": True, "survey_version": SURVEY_VERSION, "retention_days": VOICE_SURVEY_RETENTION_DAYS}
