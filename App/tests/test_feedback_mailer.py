from __future__ import annotations

from dataclasses import replace
from email.message import EmailMessage
from unittest import TestCase

from sqlalchemy import text

from backend.snow_app.public_security import encrypt_qq
from backend.snow_app.public_store import PublicStore
from backend.snow_app.feedback_mailer import FeedbackMailer
from tests.test_public_api import _settings


class FeedbackMailerTests(TestCase):
    def setUp(self) -> None:
        self.settings = replace(
            _settings(),
            feedback_email_to="admin@xiaob.dev",
            feedback_email_from="snow@xiaob.dev",
            feedback_smtp_host="smtp.example.invalid",
            feedback_smtp_username="snow",
            feedback_smtp_password="smtp-secret",
        )
        self.store = PublicStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()

    def test_feedback_is_enqueued_and_qq_is_decrypted_only_for_message(self) -> None:
        encrypted = encrypt_qq(self.settings, "12345678")
        self.store.insert_feedback(
            subject_hash="subject",
            ip_fingerprint="daily-ip",
            body_text="页面反馈",
            context={"app_version": "0.8.2", "assistant_answer": "安全回复"},
            qq_cipher=encrypted,
        )
        with self.store.begin() as connection:
            row = connection.execute(
                text("SELECT qq_cipher FROM public_feedback")
            ).scalar_one()
        self.assertNotEqual(row, "12345678")
        sent: list[EmailMessage] = []
        mailer = FeedbackMailer(self.settings, self.store, sender=sent.append)
        result = mailer.run_once()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.store.feedback_email_status(), {"sent": 1})
        body = sent[0].get_content()
        self.assertIn("12345678", body)
        self.assertNotIn(encrypted, body)
        self.assertNotIn("smtp-secret", body)

    def test_failed_delivery_is_retried_without_losing_feedback(self) -> None:
        self.store.insert_feedback(
            subject_hash="subject",
            ip_fingerprint="daily-ip",
            body_text="暂时失败",
            context={},
            qq_cipher=None,
        )

        def fail(_message: EmailMessage) -> None:
            raise RuntimeError("smtp unavailable")

        result = FeedbackMailer(self.settings, self.store, sender=fail).run_once()
        self.assertEqual(result["retried"], 1)
        status = self.store.feedback_email_status()
        self.assertEqual(status.get("retry"), 1)
        self.assertEqual(len(self.store.feedback_rows()), 1)
