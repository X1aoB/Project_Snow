from __future__ import annotations

from dataclasses import replace
import time
from unittest import TestCase

from fastapi.testclient import TestClient

from backend.snow_app import admin_main
from backend.snow_app.public_store import PublicStore
from tests.test_public_api import _settings


class PublicAdminFeedbackTests(TestCase):
    def setUp(self) -> None:
        self.settings = replace(_settings(), admin_token="admin-test")
        self.store = PublicStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.original_settings = admin_main.settings
        self.original_store = admin_main.store
        admin_main.settings = self.settings
        admin_main.store = self.store
        self.client = TestClient(admin_main.app)
        self.auth = {"Authorization": "Bearer admin-test"}

    def tearDown(self) -> None:
        self.client.close()
        admin_main.settings = self.original_settings
        admin_main.store = self.original_store

    def _feedback(self, body: str = "今天的反馈") -> str:
        return self.store.insert_feedback(
            subject_hash="subject-" + body,
            ip_fingerprint="ip-" + body,
            body_text=body,
            context={"assistant_answer": "不应出现在回执里"},
            qq_cipher="encrypted-qq",
        )

    def test_feedback_list_is_newest_first_and_exposes_resolution_state(self) -> None:
        first = self._feedback("较早反馈")
        time.sleep(0.002)
        second = self._feedback("较新反馈")
        response = self.client.get("/admin/v1/feedback?limit=2", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        rows = response.json()["feedback"]
        self.assertEqual([row["public_code"] for row in rows], [second, first])
        self.assertEqual(rows[0]["resolution_status"], "pending_triage")
        self.assertEqual(rows[0]["email_status"], "pending")
        self.assertEqual(rows[0]["qq"], "***")

    def test_triage_marker_is_append_only_and_survives_list(self) -> None:
        code = self._feedback()
        response = self.client.post(
            f"/admin/v1/feedback/{code}/triage",
            headers=self.auth,
            json={"status": "fixed_verified", "note": "回归已通过"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "fixed_verified")
        listed = self.client.get("/admin/v1/feedback", headers=self.auth).json()["feedback"][0]
        self.assertEqual(listed["resolution_status"], "fixed_verified")
        self.assertEqual(listed["resolution_note"], "回归已通过")
        self.assertNotIn("不应出现在回执里", response.text)

    def test_latest_email_queue_does_not_require_remembering_receipt_code(self) -> None:
        self._feedback("今早提交")
        first = self.client.post("/admin/v1/feedback/latest/email", headers=self.auth)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["feedback"][0]["status"], "already_pending")
        self.assertNotIn("今早提交", first.text)
        repeated = self.client.post("/admin/v1/feedback/latest/email", headers=self.auth)
        self.assertEqual(repeated.json()["feedback"][0]["status"], "already_pending")
        self.assertEqual(
            self.client.get("/admin/v1/feedback/email/status", headers=self.auth).json(),
            {"status": {"pending": 1}},
        )

    def test_sent_receipt_requires_force_for_resend(self) -> None:
        code = self._feedback()
        claimed = self.store.claim_feedback_email()
        self.assertEqual(len(claimed), 1)
        self.store.mark_feedback_email_sent(claimed[0]["outbox_id"])
        already_sent = self.client.post(
            f"/admin/v1/feedback/{code}/email", headers=self.auth
        )
        self.assertEqual(already_sent.json()["status"], "already_sent")
        resent = self.client.post(
            f"/admin/v1/feedback/{code}/email?force=true", headers=self.auth
        )
        self.assertEqual(resent.json()["status"], "queued")
        self.assertEqual(self.store.feedback_email_status(), {"pending": 1})

    def test_admin_auth_is_required_for_queue_and_triage(self) -> None:
        code = self._feedback()
        self.assertEqual(
            self.client.post(f"/admin/v1/feedback/{code}/email").status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/admin/v1/feedback/{code}/triage",
                json={"status": "resolved"},
            ).status_code,
            403,
        )
