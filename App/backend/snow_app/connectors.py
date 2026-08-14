"""Small, explicit connector layer used by the local Agent.

The connector layer intentionally exposes boring operations (search, draft,
send) instead of handing arbitrary credentials to a model.  Secrets are read
from the OS credential vault by reference.  Network responses are returned as
untrusted data and never become character facts automatically.
"""

from __future__ import annotations

import email
from email.message import EmailMessage
from email.utils import formatdate
import imaplib
import json
import secrets
import smtplib
import base64
from hashlib import sha256
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx

from .agent_store import AgentStore
from .provider_registry import CredentialVault


OAUTH_DEFAULTS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "openid email profile https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar",
    },
    "microsoft_graph": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": "openid profile offline_access User.Read Mail.Read Calendars.ReadWrite Files.ReadWrite",
    },
}


class ConnectorError(ValueError):
    pass


def _secret_payload(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "")
        return decoded if isinstance(decoded, dict) else {"token": value}
    except json.JSONDecodeError:
        return {"token": value}


class ConnectorManager:
    def __init__(self, store: AgentStore, vault: CredentialVault):
        self.store = store
        self.vault = vault

    def get(self, connector_id: str) -> dict[str, Any]:
        record = self.store.get_connector(connector_id)
        if not record:
            raise ConnectorError("连接器不存在。")
        return record

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        config = dict(record.get("config") or {})
        for key in ("client_secret", "password", "token", "access_token", "refresh_token"):
            config.pop(key, None)
        return {
            **record,
            "credential_ref": "configured" if record.get("credential_ref") else "",
            "config": config,
            "capabilities": self.capabilities(record.get("connector_type")),
        }

    @staticmethod
    def capabilities(connector_type: str | None) -> dict[str, bool]:
        return {
            "read_search": connector_type in {"imap_smtp", "caldav", "webdav", "microsoft_graph", "google"},
            "draft": True,
            "send": connector_type in {"imap_smtp", "microsoft_graph", "google"},
            "calendar_read": connector_type in {"caldav", "microsoft_graph", "google"},
            "calendar_write": connector_type in {"caldav", "microsoft_graph", "google"},
            "cloud_file_read": connector_type in {"webdav", "microsoft_graph", "google"},
            "cloud_file_write": connector_type in {"webdav", "microsoft_graph", "google"},
            "external_writes_require_approval": True,
        }

    def _secret(self, record: dict[str, Any]) -> dict[str, Any]:
        reference = str(record.get("credential_ref") or "")
        if not reference:
            raise ConnectorError("连接器尚未配置凭据。")
        value = self.vault.get(reference)
        if not value:
            raise ConnectorError("连接器凭据不可用。")
        return _secret_payload(value)

    def oauth_start(self, connector_id: str, redirect_uri: str | None = None) -> dict[str, Any]:
        record = self.get(connector_id)
        kind = str(record.get("connector_type") or "")
        defaults = OAUTH_DEFAULTS.get(kind)
        if not defaults:
            raise ConnectorError("该连接器类型不支持 OAuth。")
        config = dict(record.get("config") or {})
        client_id = str(config.get("client_id") or "")
        if not client_id:
            raise ConnectorError("OAuth 连接器需要 client_id。")
        redirect = str(redirect_uri or config.get("redirect_uri") or f"http://127.0.0.1:8000/api/v1/connectors/oauth/callback?connector_id={connector_id}")
        if not (redirect.startswith("http://127.0.0.1") or redirect.startswith("http://localhost") or redirect.startswith("https://")):
            raise ConnectorError("redirect_uri 只允许本机回调或 HTTPS。")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        self.vault.put(f"oauth-verifier:{state}", verifier)
        config.update({"oauth_state": state, "oauth_redirect_uri": redirect})
        self.store.upsert_connector({**record, "config": config, "status": "oauth_pending"})
        query = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": str(config.get("scopes") or defaults["scopes"]),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        return {"connector_id": connector_id, "authorization_url": str(config.get("authorize_url") or defaults["authorize_url"]) + "?" + urlencode(query), "state": state, "status": "oauth_pending"}

    def oauth_callback(self, connector_id: str, code: str, state: str) -> dict[str, Any]:
        record = self.get(connector_id)
        config = dict(record.get("config") or {})
        if not secrets.compare_digest(str(config.get("oauth_state") or ""), state):
            raise ConnectorError("OAuth state 校验失败。")
        verifier = self.vault.get(f"oauth-verifier:{state}")
        if not verifier:
            raise ConnectorError("OAuth PKCE verifier 已过期，请重新授权。")
        defaults = OAUTH_DEFAULTS.get(str(record.get("connector_type") or ""), {})
        client_id = str(config.get("client_id") or "")
        current_secret = self._secret(record) if record.get("credential_ref") else {}
        client_secret = str(current_secret.get("client_secret") or "")
        payload = {
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": config.get("oauth_redirect_uri") or config.get("redirect_uri"),
        }
        if client_secret:
            payload["client_secret"] = client_secret
        response = httpx.post(str(config.get("token_url") or defaults.get("token_url") or ""), data=payload, timeout=30, follow_redirects=True)
        response.raise_for_status()
        token = response.json()
        if not isinstance(token, dict) or not token.get("access_token"):
            raise ConnectorError("OAuth token 响应缺少 access_token。")
        reference = str(record.get("credential_ref") or f"connector:{connector_id}")
        self.vault.put(reference, json.dumps(token, ensure_ascii=False))
        config.pop("oauth_state", None)
        config.pop("oauth_redirect_uri", None)
        updated = self.store.upsert_connector({**record, "credential_ref": reference, "status": "connected", "config": config})
        self.vault.delete(f"oauth-verifier:{state}")
        return self.public(updated)

    def search(self, connector_id: str, query: str, limit: int = 20) -> dict[str, Any]:
        record = self.get(connector_id)
        query = str(query or "").strip()[:500]
        if not query:
            raise ConnectorError("搜索条件不能为空。")
        kind = str(record.get("connector_type") or "")
        secret = self._secret(record)
        config = dict(record.get("config") or {})
        if kind == "imap_smtp":
            host = str(config.get("imap_host") or "")
            username = str(config.get("username") or secret.get("username") or "")
            password = str(secret.get("password") or secret.get("token") or "")
            if not host or not username or not password:
                raise ConnectorError("IMAP 需要 imap_host、username 和 password。")
            mailbox = str(config.get("mailbox") or "INBOX")
            with imaplib.IMAP4_SSL(host, int(config.get("imap_port") or 993)) as client:
                client.login(username, password)
                client.select(mailbox, readonly=True)
                _status, data = client.search(None, "TEXT", query)
                ids = (data[0] or b"").split()[-limit:]
                rows = []
                for uid in reversed(ids):
                    _status, payload = client.fetch(uid, "(RFC822)")
                    raw = next((part[1] for part in payload if isinstance(part, tuple)), b"")
                    message = email.message_from_bytes(raw)
                    rows.append({"uid": uid.decode(errors="ignore"), "subject": str(message.get("Subject") or ""), "from": str(message.get("From") or ""), "date": str(message.get("Date") or "")})
            return {"connector_id": connector_id, "kind": kind, "query": query, "items": rows, "security": "untrusted_connector_data"}
        token = str(secret.get("access_token") or secret.get("token") or "")
        url = str(config.get("search_url") or config.get("base_url") or "")
        if not url:
            raise ConnectorError("连接器需要 search_url 或 base_url。")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = httpx.get(url, params={"q": query, "limit": min(max(limit, 1), 100)}, headers=headers, timeout=30, follow_redirects=True)
        response.raise_for_status()
        return {"connector_id": connector_id, "kind": kind, "query": query, "items": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:20_000], "security": "untrusted_connector_data"}

    def draft(self, connector_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.get(connector_id)
        return {"connector_id": connector_id, "kind": "draft", "draft": {key: str(value)[:10_000] for key, value in payload.items()}, "status": "draft_only"}

    @staticmethod
    def _network_auth(record: dict[str, Any], secret: dict[str, Any]) -> tuple[dict[str, str], tuple[str, str] | None]:
        token = str(secret.get("access_token") or secret.get("token") or "")
        if token:
            return {"Authorization": f"Bearer {token}"}, None
        username = str((record.get("config") or {}).get("username") or secret.get("username") or "")
        password = str(secret.get("password") or "")
        return {}, (username, password) if username and password else None

    @staticmethod
    def _configured_url(record: dict[str, Any], key: str) -> str:
        config = dict(record.get("config") or {})
        value = str(config.get(key) or config.get("base_url") or "").strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConnectorError(f"连接器缺少有效的 {key}。")
        return value

    def calendar_list(self, connector_id: str, query: str = "") -> dict[str, Any]:
        record = self.get(connector_id)
        if str(record.get("connector_type")) not in {"caldav", "microsoft_graph", "google"}:
            raise ConnectorError("当前连接器不支持日历。")
        secret = self._secret(record)
        headers, auth = self._network_auth(record, secret)
        url = self._configured_url(record, "calendar_url")
        response = httpx.get(url, params={"q": query[:500]} if query else None, headers=headers, auth=auth, timeout=30, follow_redirects=True)
        response.raise_for_status()
        payload: Any = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:30_000]
        return {"connector_id": connector_id, "items": payload, "security": "untrusted_connector_data"}

    def calendar_write(self, connector_id: str, event: dict[str, Any]) -> dict[str, Any]:
        record = self.get(connector_id)
        if str(record.get("connector_type")) not in {"caldav", "microsoft_graph", "google"}:
            raise ConnectorError("当前连接器不支持日历写入。")
        secret = self._secret(record)
        headers, auth = self._network_auth(record, secret)
        url = self._configured_url(record, "calendar_url")
        response = httpx.post(url, json=event, headers={**headers, "Content-Type": "application/json"}, auth=auth, timeout=30, follow_redirects=True)
        response.raise_for_status()
        return {"connector_id": connector_id, "status": "created_or_updated", "response": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:4000]}

    def cloud_list(self, connector_id: str, path: str = "") -> dict[str, Any]:
        record = self.get(connector_id)
        if str(record.get("connector_type")) not in {"webdav", "microsoft_graph", "google"}:
            raise ConnectorError("当前连接器不支持云端文件。")
        secret = self._secret(record)
        headers, auth = self._network_auth(record, secret)
        base = self._configured_url(record, "files_url")
        url = urljoin(base.rstrip("/") + "/", quote(path.strip("/"), safe="/"))
        method = "PROPFIND" if str(record.get("connector_type")) == "webdav" else "GET"
        response = httpx.request(method, url, headers={**headers, **({"Depth": "1"} if method == "PROPFIND" else {})}, auth=auth, timeout=30, follow_redirects=True)
        response.raise_for_status()
        return {"connector_id": connector_id, "path": path, "items": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:30_000], "security": "untrusted_connector_data"}

    def cloud_upload(self, connector_id: str, remote_path: str, data: bytes, content_type: str = "application/octet-stream") -> dict[str, Any]:
        record = self.get(connector_id)
        secret = self._secret(record)
        headers, auth = self._network_auth(record, secret)
        base = self._configured_url(record, "files_url")
        url = urljoin(base.rstrip("/") + "/", quote(remote_path.strip("/"), safe="/"))
        response = httpx.put(url, content=data, headers={**headers, "Content-Type": content_type}, auth=auth, timeout=60, follow_redirects=True)
        response.raise_for_status()
        return {"connector_id": connector_id, "status": "uploaded", "remote_path": remote_path, "bytes": len(data)}

    def cloud_delete(self, connector_id: str, remote_path: str) -> dict[str, Any]:
        record = self.get(connector_id)
        secret = self._secret(record)
        headers, auth = self._network_auth(record, secret)
        base = self._configured_url(record, "files_url")
        url = urljoin(base.rstrip("/") + "/", quote(remote_path.strip("/"), safe="/"))
        response = httpx.delete(url, headers=headers, auth=auth, timeout=30, follow_redirects=True)
        response.raise_for_status()
        return {"connector_id": connector_id, "status": "deleted", "remote_path": remote_path}

    def send_email(self, connector_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = self.get(connector_id)
        if str(record.get("connector_type")) != "imap_smtp":
            raise ConnectorError("当前连接器不是 SMTP 邮件连接器。")
        config = dict(record.get("config") or {})
        secret = self._secret(record)
        host = str(config.get("smtp_host") or "")
        username = str(config.get("username") or secret.get("username") or "")
        password = str(secret.get("password") or secret.get("token") or "")
        recipient = str(payload.get("to") or "").strip()
        if not host or not username or not password or "@" not in recipient:
            raise ConnectorError("SMTP 需要 smtp_host、凭据和有效收件人。")
        message = EmailMessage()
        message["From"] = str(payload.get("from") or username)
        message["To"] = recipient
        message["Subject"] = str(payload.get("subject") or "Project Snow")[:500]
        message["Date"] = formatdate(localtime=True)
        message.set_content(str(payload.get("body") or "")[:100_000])
        with smtplib.SMTP_SSL(host, int(config.get("smtp_port") or 465), timeout=30) as client:
            client.login(username, password)
            client.send_message(message)
        return {"connector_id": connector_id, "status": "sent", "to": recipient, "subject": message["Subject"]}
