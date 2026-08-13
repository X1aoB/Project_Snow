from __future__ import annotations

from datetime import UTC, datetime
from unittest import TestCase

from sqlalchemy import text

from backend.snow_app.public_store import DuplicateFeedback, PublicStore, RateLimitExceeded


class PublicStoreTests(TestCase):
    def setUp(self) -> None:
        self.store = PublicStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()

    def test_rate_limit_failure_rolls_back_the_failed_increment(self) -> None:
        now = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
        self.store.consume_limits("subject", [("chat_hour", "hour", 1)], now=now)
        with self.assertRaises(RateLimitExceeded):
            self.store.consume_limits("subject", [("chat_hour", "hour", 1)], now=now)
        with self.store.begin() as connection:
            count = connection.execute(
                text("SELECT count FROM public_rate_limit WHERE subject_hash = 'subject'")
            ).scalar_one()
        self.assertEqual(count, 1)

    def test_feedback_dedupe_does_not_persist_ip_in_feedback_row(self) -> None:
        self.store.insert_feedback(
            subject_hash="subject-a",
            ip_fingerprint="daily-fingerprint",
            body_text="反馈",
            context={},
            qq_cipher=None,
        )
        with self.store.begin() as connection:
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(public_feedback)")).all()
            }
        self.assertNotIn("ip_fingerprint", columns)
        with self.assertRaises(DuplicateFeedback):
            self.store.insert_feedback(
                subject_hash="subject-b",
                ip_fingerprint="daily-fingerprint",
                body_text="反馈",
                context={},
                qq_cipher=None,
            )
