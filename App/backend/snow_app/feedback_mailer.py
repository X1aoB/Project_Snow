"""Asynchronous feedback notification worker.

The public API only enqueues a reference to a feedback row.  QQ remains
encrypted in PostgreSQL and is decrypted for the SMTP transaction only.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
import json
import smtplib
from datetime import UTC, datetime
from typing import Any, Callable

from .config import PublicSettings
from .public_security import decrypt_qq, redact_sensitive_text
from .public_store import PublicStore


class FeedbackMailer:
    def __init__(
        self,
        settings: PublicSettings,
        store: PublicStore,
        *,
        sender: Callable[[EmailMessage], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sender = sender

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.feedback_email_to
            and self.settings.feedback_email_from
            and self.settings.feedback_smtp_host
            and self.settings.feedback_smtp_username
            and self.settings.feedback_smtp_password
        )

    @staticmethod
    def _safe_context(context: dict[str, Any]) -> dict[str, Any]:
        allowed = (
            "character_id",
            "provider",
            "model",
            "app_version",
            "data_version",
            "request_stage",
            "error_code",
            "chat_error_code",
            "degraded_services",
            "ui_surface",
            "generation_outcome",
            "response_adjustments",
            "generation_diagnostics",
        )
        return {key: context.get(key) for key in allowed if context.get(key) not in (None, "", [], {})}

    def _message(self, row: dict[str, Any]) -> EmailMessage:
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        body = redact_sensitive_text(str(row.get("body_text") or ""), 1000)
        qq_cipher = row.get("qq_cipher")
        qq = decrypt_qq(self.settings, qq_cipher) if qq_cipher else "（未提供）"
        safe_context = self._safe_context(context)
        # This is intentionally a plain-text message: it avoids rendering any
        # user-provided HTML while still including the requested QQ value.
        lines = [
            "Project Snow 新反馈",
            f"反馈编号：{row.get('public_code') or row.get('feedback_id')}",
            f"提交时间：{row.get('created_at')}",
            "",
            "反馈内容：",
            body,
            "",
            f"QQ：{redact_sensitive_text(qq, 32)}",
            "",
            "安全上下文：",
            json.dumps(safe_context, ensure_ascii=False, separators=(",", ":")),
        ]
        message = EmailMessage()
        message["From"] = self.settings.feedback_email_from
        message["To"] = self.settings.feedback_email_to
        message["Subject"] = f"[Project Snow] 新反馈 {row.get('public_code') or ''}"[:180]
        message["Date"] = format_datetime(datetime.now(UTC))
        message.set_content("\n".join(lines))
        return message

    def _send(self, message: EmailMessage) -> None:
        if self.sender is not None:
            self.sender(message)
            return
        with smtplib.SMTP_SSL(
            self.settings.feedback_smtp_host,
            int(self.settings.feedback_smtp_port),
            timeout=30,
        ) as client:
            client.login(self.settings.feedback_smtp_username, self.settings.feedback_smtp_password)
            client.send_message(message)

    def run_once(self, *, limit: int = 10) -> dict[str, int | str]:
        if not self.configured and self.sender is None:
            return {"status": "disabled", "claimed": 0, "sent": 0, "retried": 0}
        rows = self.store.claim_feedback_email(limit)
        sent = 0
        retried = 0
        for row in rows:
            try:
                self._send(self._message(row))
            except Exception as exc:  # pragma: no cover - exercised by worker integration
                attempts = int(row.get("attempt_count") or 1)
                delay = min(86400, 60 * (2 ** min(attempts - 1, 8)))
                self.store.mark_feedback_email_retry(row["outbox_id"], type(exc).__name__, delay_seconds=delay)
                retried += 1
            else:
                self.store.mark_feedback_email_sent(row["outbox_id"])
                sent += 1
        return {"status": "ok", "claimed": len(rows), "sent": sent, "retried": retried}
