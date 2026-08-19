from __future__ import annotations

from dataclasses import replace
from email.message import EmailMessage
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import text

from backend.snow_app.config import _public_database_url
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

    def test_feedback_email_contains_receipt_only(self) -> None:
        encrypted = encrypt_qq(self.settings, "12345678")
        self.store.insert_feedback(
            subject_hash="subject",
            ip_fingerprint="daily-ip",
            body_text="页面反馈",
            context={"app_version": "0.9.0", "assistant_answer": "安全回复"},
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
        self.assertNotIn("12345678", body)
        self.assertNotIn("页面反馈", body)
        self.assertNotIn("安全回复", body)
        self.assertIn("snow-", body)
        self.assertNotIn(encrypted, body)
        self.assertNotIn("smtp-secret", body)

    def test_component_database_url_uses_only_dedicated_password(self) -> None:
        with TemporaryDirectory() as directory:
            password_file = Path(directory) / "password"
            password_file.write_text("mail:secret/@", encoding="utf-8")
            environment = {
                "PUBLIC_DATABASE_URL": "",
                "PUBLIC_DATABASE_URL_FILE": "",
                "PUBLIC_DATABASE_PASSWORD_FILE": str(password_file),
                "PUBLIC_DATABASE_USER": "project_snow_feedback_mailer",
                "PUBLIC_DATABASE_HOST": "postgres",
                "PUBLIC_DATABASE_PORT": "5432",
                "PUBLIC_DATABASE_NAME": "project_snow",
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertEqual(
                    _public_database_url(),
                    "postgresql+psycopg://project_snow_feedback_mailer:"
                    "mail%3Asecret%2F%40@postgres:5432/project_snow",
                )

    def test_claim_exposes_only_receipt_delivery_fields(self) -> None:
        encrypted = encrypt_qq(self.settings, "12345678")
        self.store.insert_feedback(
            subject_hash="subject",
            ip_fingerprint="daily-ip",
            body_text="绝不能交给邮件进程",
            context={"assistant_answer": "也不能交给邮件进程"},
            qq_cipher=encrypted,
        )
        claimed = self.store.claim_feedback_email()
        self.assertEqual(len(claimed), 1)
        self.assertEqual(
            set(claimed[0]),
            {"outbox_id", "feedback_id", "attempt_count", "public_code", "created_at"},
        )

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
