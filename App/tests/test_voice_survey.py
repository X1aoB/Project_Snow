"""Voice-preference survey contract.  Synthetic subjects; no model is involved."""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.snow_app.config import Settings
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_main import create_app
from backend.snow_app.public_store import PublicStore
from backend.snow_app.voice_survey_store import VOICE_SURVEY_RETENTION_DAYS
from scripts.voice_survey_report import summarize
from tests.test_public_api import _settings

APP_ROOT = Path(__file__).resolve().parents[1]
ANSWERS = {
    "survey_version": "voice-survey-1", "use_voice": "often", "use_scenes": ["group"], "voice_type": "ai_designed",
    "pass_through_acceptance": 4, "payment_method": "own_key", "monthly_budget": "6_20", "priorities": ["latency"],
    "characters": [MVP_CHARACTERS[0].character_id], "adult": "", "comment": "",
}


class VoiceSurveyApiTests(TestCase):
    def build(self, *, enabled=True):
        settings = replace(_settings(), voice_survey_v1=enabled)
        store = PublicStore(settings.database_url)
        store.create_schema()
        app = create_app(settings, Settings.from_environment(), store)
        client = TestClient(app, headers={"Origin": "http://testserver"})
        self.addCleanup(client.close)
        self.addCleanup(app.state.chat_service.close)
        return store, client

    @staticmethod
    def rows(store):
        with store.begin() as connection:
            return connection.execute(text("SELECT * FROM public_voice_survey_response")).mappings().all()

    def test_switch_off_hides_the_survey_and_stores_nothing(self):
        store, client = self.build(enabled=False)
        self.assertEqual(client.get("/public/v1/config").json()["voice_survey"],
                         {"enabled": False, "survey_version": "voice-survey-1"})
        self.assertEqual(client.get("/public/v1/voice-survey").status_code, 404)
        self.assertEqual(client.post("/public/v1/voice-survey", json=ANSWERS).status_code, 404)
        self.assertEqual(self.rows(store), [])

    def test_a_second_submission_updates_the_one_row(self):
        store, client = self.build()
        self.assertFalse(client.get("/public/v1/voice-survey").json()["answered"])
        first = client.post("/public/v1/voice-survey", json=ANSWERS)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), {"saved": True, "survey_version": "voice-survey-1",
                                        "retention_days": VOICE_SURVEY_RETENTION_DAYS})
        self.assertEqual(first.headers["cache-control"], "no-store")
        created = self.rows(store)[0]["created_at"]
        self.assertEqual(client.post("/public/v1/voice-survey", json={**ANSWERS, "use_voice": "never"}).status_code, 200)
        rows = self.rows(store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["answers_json"])["use_voice"], "never")
        self.assertEqual(rows[0]["created_at"], created)
        self.assertTrue(client.get("/public/v1/voice-survey").json()["answered"])

    def test_a_different_visitor_gets_their_own_row(self):
        store, client = self.build()
        client.post("/public/v1/voice-survey", json=ANSWERS)
        client.cookies.clear()
        client.post("/public/v1/voice-survey", json=ANSWERS)
        self.assertEqual(len(self.rows(store)), 2)

    def test_invalid_answers_are_refused(self):
        store, client = self.build()
        for name, change in (
            ("unknown option", {"use_voice": "perhaps"}),
            ("score out of range", {"pass_through_acceptance": 6}),
            ("score as text", {"pass_through_acceptance": "4"}),
            ("unknown character", {"characters": ["not-a-character"]}),
            ("repeated choice", {"priorities": ["price", "price"]}),
            ("too long", {"comment": "字" * 201}),
            ("unknown field", {"ip": "192.0.2.1"}),
            ("wrong version", {"survey_version": "voice-survey-0"}),
        ):
            with self.subTest(name):
                self.assertEqual(client.post("/public/v1/voice-survey", json={**ANSWERS, **change}).status_code, 422)
        self.assertEqual(self.rows(store), [])

    def test_no_address_or_browser_detail_is_stored(self):
        store, client = self.build()
        client.post("/public/v1/voice-survey", json=ANSWERS, headers={"User-Agent": "synthetic-browser-ua"})
        row = self.rows(store)[0]
        self.assertEqual(set(row), {"subject_hash", "survey_version", "answers_json", "comment",
                                    "created_at", "updated_at", "expires_at"})
        self.assertNotIn("synthetic-browser-ua", json.dumps({key: str(value) for key, value in row.items()}))

    def test_comment_is_scrubbed_of_secrets_and_never_logged(self):
        store, client = self.build()
        secret = "sk-" + "a" * 40
        marker = "合成的自由文本建议"
        logger = logging.getLogger("snow.public")
        with self.assertLogs(logger, level="INFO") as captured:
            logger.info("capture-start")
            self.assertEqual(client.post("/public/v1/voice-survey", json={**ANSWERS, "comment": f"{marker} {secret}"}).status_code, 200)
            self.assertEqual(client.post("/public/v1/voice-survey", json={**ANSWERS, "comment": marker, "use_voice": "bad"}).status_code, 422)
        logged = "\n".join(captured.output)
        self.assertNotIn(marker, logged)
        self.assertNotIn(secret, logged)
        stored = self.rows(store)[0]["comment"]
        self.assertIn(marker, stored)
        self.assertNotIn(secret, stored)

    def test_rate_limit_applies_to_one_visitor(self):
        store, client = self.build()
        statuses = [client.post("/public/v1/voice-survey", json=ANSWERS).status_code for _ in range(11)]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)
        self.assertEqual(len(self.rows(store)), 1)

    def test_cleanup_deletes_only_rows_past_retention(self):
        store, _client = self.build()
        past = datetime.now(UTC) - timedelta(days=VOICE_SURVEY_RETENTION_DAYS + 1)
        store.save_voice_survey("synthetic-old", "voice-survey-1", {"use_voice": "often"}, "", now=past)
        store.save_voice_survey("synthetic-new", "voice-survey-1", {"use_voice": "never"}, "")
        self.assertFalse(store.voice_survey_answered("synthetic-old"))
        self.assertEqual(store.cleanup()["public_voice_survey_response"], 1)
        self.assertEqual([row["subject_hash"] for row in self.rows(store)], ["synthetic-new"])

    def test_every_option_in_the_copy_is_accepted_by_the_server(self):
        copy = json.loads((APP_ROOT / "public_frontend/voice-survey.json").read_text(encoding="utf-8"))
        self.assertEqual(copy["survey_version"], "voice-survey-1")
        self.assertEqual(copy["privacy"].count(str(VOICE_SURVEY_RETENTION_DAYS)), 1)
        _store, client = self.build()
        for question in copy["questions"]:
            for option in question.get("options", []):
                value = [option["value"]] if question["type"] == "multi" else option["value"]
                with self.subTest(question=question["id"], option=option["value"]):
                    self.assertEqual(client.post("/public/v1/voice-survey", json={**ANSWERS, question["id"]: value}).status_code, 200)
                client.cookies.clear()


class VoiceSurveyReportTests(TestCase):
    def test_report_has_only_counts_and_shares(self):
        rows = [
            {"answers_json": json.dumps({"use_voice": "often", "priorities": ["price", "latency"]}), "comment": "合成建议文本"},
            {"answers_json": json.dumps({"use_voice": "never", "priorities": ["price"]}), "comment": ""},
        ]
        report = summarize(rows)
        self.assertEqual(report["respondents"], 2)
        self.assertEqual(report["with_comment"], 1)
        self.assertEqual(report["questions"]["use_voice"]["often"], {"count": 1, "share": 0.5})
        self.assertEqual(report["questions"]["priorities"]["price"], {"count": 2, "share": 1.0})
        self.assertNotIn("合成建议文本", json.dumps(report, ensure_ascii=False))

    def test_an_empty_table_reports_zero(self):
        self.assertEqual(summarize([])["respondents"], 0)
