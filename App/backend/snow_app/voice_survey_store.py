"""Voice-preference survey rows: one per anonymous subject, no IP or user agent.

Additive only.  Old binaries ignore this table, so a rollback needs no schema
change; ``PublicStore.cleanup`` deletes rows once their retention has passed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

VOICE_SURVEY_RETENTION_DAYS = 180
VOICE_SURVEY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public_voice_survey_response (
    subject_hash VARCHAR(64) PRIMARY KEY,
    survey_version VARCHAR(32) NOT NULL,
    answers_json TEXT NOT NULL,
    comment VARCHAR(400) NOT NULL DEFAULT '',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS public_voice_survey_response_expiry_idx
    ON public_voice_survey_response (expires_at);
"""


class VoiceSurveyStoreMixin:
    def save_voice_survey(self, subject_hash, survey_version, answers, comment, *, now=None):
        """Insert the subject's answers, or replace them on a later submission."""
        now = now or datetime.now(UTC)
        with self.begin() as connection:
            connection.execute(text(
                "INSERT INTO public_voice_survey_response "
                "(subject_hash,survey_version,answers_json,comment,created_at,updated_at,expires_at) "
                "VALUES (:subject,:version,:answers,:comment,:now,:now,:expires) "
                "ON CONFLICT (subject_hash) DO UPDATE SET survey_version=EXCLUDED.survey_version,"
                "answers_json=EXCLUDED.answers_json,comment=EXCLUDED.comment,"
                "updated_at=EXCLUDED.updated_at,expires_at=EXCLUDED.expires_at"),
                {"subject": subject_hash, "version": survey_version, "comment": comment, "now": now,
                 "answers": json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 "expires": now + timedelta(days=VOICE_SURVEY_RETENTION_DAYS)})

    def voice_survey_answered(self, subject_hash) -> bool:
        with self.begin() as connection:
            row = connection.execute(text(
                "SELECT 1 FROM public_voice_survey_response WHERE subject_hash=:subject AND expires_at>:now"),
                {"subject": subject_hash, "now": datetime.now(UTC)}).first()
        return row is not None
