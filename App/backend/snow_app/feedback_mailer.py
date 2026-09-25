"""Asynchronous feedback notification worker.

The public API only enqueues a reference to a feedback row.  Email carries
the submitted feedback body and optional QQ contact, while conversation
context, diagnostics, IP data and credentials remain outside this path.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
import smtplib
from datetime import UTC, datetime
from typing import Any, Callable

from .config import PublicSettings
from .public_security import decrypt_qq
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

    def _message(self, row: dict[str, Any]) -> EmailMessage:
        receipt = str(row.get("public_code") or row.get("feedback_id") or "")[:96]
        body = str(row.get("body_text") or "").strip()
        encrypted_qq = str(row.get("qq_cipher") or "").strip()
        if encrypted_qq:
            try:
                qq = decrypt_qq(self.settings, encrypted_qq)
            except Exception as exc:
                # Treat a missing/invalid QQ key as a transient delivery
                # failure.  The worker never sends the encrypted value.
                raise RuntimeError("feedback_qq_decryption_failed") from exc
        else:
            qq = ""
        lines = [
            "Project Snow 新反馈",
            f"反馈编号：{receipt}",
            f"提交时间：{row.get('created_at')}",
            "",
            "反馈内容：",
            body or "（未提供）",
            "",
            f"联系 QQ：{qq or '未提供'}",
            "",
            "本邮件不包含对话上下文、诊断信息、IP 地址或凭据。",
        ]
        message = EmailMessage()
        message["From"] = self.settings.feedback_email_from
        message["To"] = self.settings.feedback_email_to
        message["Subject"] = f"[Project Snow] 新反馈 {receipt}"[:180]
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
