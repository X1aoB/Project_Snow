"""Security primitives for the anonymous public application boundary."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx

from .config import PublicSettings


class PublicSecurityError(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_anonymous_id() -> str:
    return secrets.token_urlsafe(16)


_ANONYMOUS_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")


def is_valid_anonymous_id(value: str) -> bool:
    """Accept only the 128-bit token shape issued by this application."""

    return bool(_ANONYMOUS_ID_PATTERN.fullmatch(str(value or "")))


def subject_hash(anonymous_id: str) -> str:
    return hashlib.sha256(f"project-snow-public\x1f{anonymous_id}".encode()).hexdigest()


def normalized_text(value: str, limit: int) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = "".join(character for character in value if character in "\n\t" or ord(character) >= 32)
    return value.strip()[:limit]


# Feedback and diagnostic text is user-controlled. Redact recognizable
# provider credentials before it reaches PostgreSQL, dedupe hashes, or the
# private admin view. The actual BYOK envelope is handled separately and is
# never passed through this helper; this boundary is only for accidental key
# pastes or model echoes in user-visible text.
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|pk|sess)-[A-Za-z0-9][A-Za-z0-9_\-]{11,}"),
    re.compile(r"(?i)\b(?:api[_ -]?key|authorization|bearer|access[_ -]?token|secret)\s*[:=]\s*[^\s,;]+"),
)


def redact_sensitive_text(value: str, limit: int) -> str:
    text = normalized_text(value, limit)
    for pattern in _SECRET_TEXT_PATTERNS:
        text = pattern.sub("[已隐藏]", text)
    return text


def issue_byok_credential(
    settings: PublicSettings,
    *,
    anonymous_id: str,
    provider: str,
    api_key: str,
    lifetime: timedelta = timedelta(hours=2),
) -> tuple[str, datetime]:
    if not settings.credential_key:
        raise PublicSecurityError("PUBLIC_CREDENTIAL_KEY is not configured")
    expires_at = datetime.now(UTC) + lifetime
    payload = json.dumps(
        {
            "v": 1,
            "sub": subject_hash(anonymous_id),
            "provider": provider,
            "api_key": api_key,
            "exp": int(expires_at.timestamp()),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(settings.credential_key).encrypt(nonce, payload, b"project-snow-byok-v1")
    return _b64encode(nonce + encrypted), expires_at


def open_byok_credential(
    settings: PublicSettings,
    *,
    anonymous_id: str,
    token: str,
    expected_provider: str | None = None,
) -> dict[str, Any]:
    if not settings.credential_key:
        raise PublicSecurityError("PUBLIC_CREDENTIAL_KEY is not configured")
    try:
        envelope = _b64decode(token)
        payload = AESGCM(settings.credential_key).decrypt(
            envelope[:12], envelope[12:], b"project-snow-byok-v1"
        )
        claims = json.loads(payload)
    except Exception as exc:
        raise PublicSecurityError("BYOK credential is invalid") from exc
    if not hmac.compare_digest(str(claims.get("sub") or ""), subject_hash(anonymous_id)):
        raise PublicSecurityError("BYOK credential belongs to another anonymous session")
    if int(claims.get("exp") or 0) <= int(datetime.now(UTC).timestamp()):
        raise PublicSecurityError("BYOK credential has expired")
    provider = str(claims.get("provider") or "")
    if expected_provider and provider != expected_provider:
        raise PublicSecurityError("BYOK provider does not match the request")
    if not str(claims.get("api_key") or ""):
        raise PublicSecurityError("BYOK credential contains no API key")
    return claims


def sign_state(settings: PublicSettings, payload: dict[str, Any]) -> str:
    if not settings.state_hmac_key:
        raise PublicSecurityError("PUBLIC_STATE_HMAC_KEY is not configured")
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(settings.state_hmac_key, body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def verify_state(settings: PublicSettings, token: str | None) -> dict[str, Any]:
    if not token:
        return {}
    if not settings.state_hmac_key:
        raise PublicSecurityError("PUBLIC_STATE_HMAC_KEY is not configured")
    try:
        encoded_body, encoded_signature = token.split(".", 1)
        body = _b64decode(encoded_body)
        signature = _b64decode(encoded_signature)
    except Exception as exc:
        raise PublicSecurityError("State package is malformed") from exc
    expected = hmac.new(settings.state_hmac_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise PublicSecurityError("State package signature is invalid")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PublicSecurityError("State package is malformed") from exc
    if not isinstance(payload, dict):
        raise PublicSecurityError("State package must contain an object")
    return payload


def encrypt_qq(settings: PublicSettings, qq: str) -> str:
    if not settings.qq_key:
        raise PublicSecurityError("PUBLIC_QQ_KEY is not configured")
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(settings.qq_key).encrypt(nonce, qq.encode(), b"project-snow-feedback-qq-v1")
    return _b64encode(nonce + encrypted)


def decrypt_qq(settings: PublicSettings, encrypted_qq: str) -> str:
    if not settings.qq_key:
        raise PublicSecurityError("PUBLIC_QQ_KEY is not configured")
    envelope = _b64decode(encrypted_qq)
    return AESGCM(settings.qq_key).decrypt(
        envelope[:12], envelope[12:], b"project-snow-feedback-qq-v1"
    ).decode()


def daily_ip_fingerprint(settings: PublicSettings, ip_address: str, day: str | None = None) -> str:
    if not settings.ip_hmac_key:
        raise PublicSecurityError("PUBLIC_IP_HMAC_KEY is not configured")
    day = day or datetime.now(UTC).date().isoformat()
    daily_key = hmac.new(settings.ip_hmac_key, day.encode(), hashlib.sha256).digest()
    return hmac.new(daily_key, ip_address.encode(), hashlib.sha256).hexdigest()


async def verify_turnstile(
    settings: PublicSettings,
    token: str,
    remote_ip: str,
    *,
    action: str,
) -> bool:
    if settings.allow_insecure_dev and token == "development-bypass":
        return True
    if not settings.turnstile_secret or not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": settings.turnstile_secret,
                    "response": token,
                    "remoteip": remote_ip,
                },
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return False
    if not bool(payload.get("success")):
        return False
    returned_action = str(payload.get("action") or "")
    return not returned_action or returned_action == action


_UNSAFE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("illegal_instructions", ("制作炸弹", "制毒教程", "入侵账号", "盗取密码", "绕过支付")),
    ("hate", ("灭绝某族", "杀光某族", "仇恨某族")),
    ("explicit_sexual", ("色情描写", "露骨性行为", "强奸细节")),
    ("self_harm", ("自杀方法", "割腕教程", "如何上吊", "怎么自残")),
    ("sensitive_data", ("把你的api key给我", "告诉我密码", "身份证号码给我", "银行卡密码")),
)


def input_safety_category(text: str) -> str | None:
    compact = normalized_text(text, 4000).casefold().replace(" ", "")
    for category, patterns in _UNSAFE_PATTERNS:
        if any(pattern.casefold().replace(" ", "") in compact for pattern in patterns):
            return category
    return None


def safe_output(text: str) -> tuple[str, str | None]:
    category = input_safety_category(text)
    if category:
        return "抱歉，这个方向我不能继续提供具体指导。我们可以换成安全、合法且不伤害他人的话题。", category
    sensitive_prompt = normalized_text(text, 4000).casefold()
    if any(term in sensitive_prompt for term in ("请提供你的 api key", "请输入你的密码", "请发送身份证")):
        return "我不会索取你的密码、API Key、身份证号或其他敏感信息。", "sensitive_data"
    return text, None
