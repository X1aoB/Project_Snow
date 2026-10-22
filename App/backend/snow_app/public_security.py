"""Security primitives for the anonymous public application boundary."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import ipaddress
import logging
import re
import secrets
import unicodedata
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx

from .config import (
    PublicSettings,
    TURNSTILE_TEST_SECRET,
    TURNSTILE_TEST_SITE_KEY,
    TURNSTILE_TEST_TOKEN,
)

LOGGER = logging.getLogger("snow.public")


def _turnstile_rejected(reason: str) -> bool:
    # Only fixed local categories may reach the whitelist-only public logger.
    # Never attach Siteverify payloads, exceptions, tokens, secrets or addresses.
    LOGGER.warning("public_turnstile_" + reason)
    return False


class PublicSecurityError(ValueError):
    pass


BYOK_RETENTION_HOURS = {"tab": 12, "2h": 2, "24h": 24, "3d": 72, "7d": 168}
BYOK_RETENTION_NOTICE_VERSION = "credential-retention-1"


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
    lifetime: timedelta | None = None,
    retention: str = "tab",
) -> tuple[str, datetime]:
    if not settings.credential_key:
        raise PublicSecurityError("PUBLIC_CREDENTIAL_KEY is not configured")
    if retention not in BYOK_RETENTION_HOURS:
        raise PublicSecurityError("BYOK credential retention is invalid")
    if lifetime is None:
        lifetime = timedelta(
            hours=(settings.byok_lifetime_hours if retention == "tab" else BYOK_RETENTION_HOURS[retention])
        )
    if lifetime <= timedelta(0) or (retention == "tab" and lifetime > timedelta(hours=12)):
        raise PublicSecurityError("BYOK credential lifetime is invalid")
    if retention != "tab" and lifetime != timedelta(hours=BYOK_RETENTION_HOURS[retention]):
        raise PublicSecurityError("BYOK credential lifetime does not match retention")
    issued_at = datetime.now(UTC)
    expires_at = issued_at + lifetime
    version = 1 if retention == "tab" else 2
    claims: dict[str, Any] = {
        "v": version,
        "sub": subject_hash(anonymous_id),
        "provider": provider,
        "api_key": api_key,
        "exp": int(expires_at.timestamp()),
    }
    # Keep the tab-scoped envelope byte-compatible with public v1.  Only an
    # explicitly retained credential receives issued-at/retention claims and
    # the v2 AAD domain used by revocation checks.
    if version == 2:
        claims.update({
            "iat": int(issued_at.timestamp()),
            # Keep the public second-based claim for compatibility while
            # retaining enough precision for revoke cutoffs.
            "iat_ms": int(issued_at.timestamp() * 1000),
            "ret": retention,
        })
    payload = json.dumps(
        claims,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    nonce = secrets.token_bytes(12)
    aad = b"project-snow-byok-v2" if version == 2 else b"project-snow-byok-v1"
    encrypted = AESGCM(settings.credential_key).encrypt(nonce, payload, aad)
    return _b64encode(nonce + encrypted), expires_at


def open_byok_credential(
    settings: PublicSettings,
    *,
    anonymous_id: str,
    token: str,
    expected_provider: str | None = None,
    revoked_before: datetime | None = None,
) -> dict[str, Any]:
    if not settings.credential_key:
        raise PublicSecurityError("PUBLIC_CREDENTIAL_KEY is not configured")
    try:
        envelope = _b64decode(token)
        claims = None
        for aad, version in ((b"project-snow-byok-v2", 2), (b"project-snow-byok-v1", 1)):
            try:
                payload = AESGCM(settings.credential_key).decrypt(envelope[:12], envelope[12:], aad)
                candidate = json.loads(payload)
                if int(candidate.get("v") or 0) == version:
                    claims = candidate
                    break
            except Exception:
                continue
        if not isinstance(claims, dict):
            raise ValueError("unsupported credential envelope")
    except Exception as exc:
        raise PublicSecurityError("BYOK credential is invalid") from exc
    if not hmac.compare_digest(str(claims.get("sub") or ""), subject_hash(anonymous_id)):
        raise PublicSecurityError("BYOK credential belongs to another anonymous session")
    version = int(claims.get("v") or 0)
    if version not in {1, 2}:
        raise PublicSecurityError("BYOK credential version is invalid")
    if version == 2 or "iat" in claims or "ret" in claims:
        retention = str(claims.get("ret") or "")
        issued_at = claims.get("iat")
        expiry = claims.get("exp")
        if (retention not in BYOK_RETENTION_HOURS
                or (version == 2 and retention == "tab")
                or (version == 1 and retention != "tab")
                or type(issued_at) is not int or type(expiry) is not int):
            raise PublicSecurityError("BYOK credential retention is invalid")
        issued_at_ms = claims.get("iat_ms")
        if version == 2 and type(issued_at_ms) is not int:
            raise PublicSecurityError("BYOK credential issued time is invalid")
        if issued_at_ms is not None and (
            type(issued_at_ms) is not int
            or issued_at_ms // 1000 != issued_at
            or issued_at_ms > int(datetime.now(UTC).timestamp() * 1000) + 30_000
        ):
            raise PublicSecurityError("BYOK credential issued time is invalid")
        if expiry != issued_at + BYOK_RETENTION_HOURS[retention] * 3600:
            raise PublicSecurityError("BYOK credential lifetime is invalid")
        if issued_at > int(datetime.now(UTC).timestamp()) + 30:
            raise PublicSecurityError("BYOK credential issued time is invalid")
        issued_at_cutoff = issued_at_ms if issued_at_ms is not None else issued_at * 1000
        if revoked_before is not None and issued_at_cutoff <= int(revoked_before.timestamp() * 1000):
            raise PublicSecurityError("BYOK credential has been revoked")
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
    signed_payload = dict(payload)
    if settings.state_key_id:
        signed_payload["state_key_id"] = settings.state_key_id
    body = json.dumps(
        signed_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
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
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PublicSecurityError("State package is malformed") from exc
    if not isinstance(payload, dict):
        raise PublicSecurityError("State package must contain an object")
    # ``_key_id`` was used by an unreleased rotation prototype.  Continue to
    # accept it so a staged candidate can always be rolled forward safely.
    key_id = str(payload.get("state_key_id") or payload.get("_key_id") or "")
    candidate_keys: list[bytes] = []
    if not key_id or key_id == settings.state_key_id:
        candidate_keys.append(settings.state_hmac_key)
    if (
        settings.state_hmac_previous_key
        and (not key_id or key_id == settings.state_previous_key_id)
    ):
        candidate_keys.append(settings.state_hmac_previous_key)
    if not candidate_keys or not any(
        hmac.compare_digest(
            signature,
            hmac.new(candidate_key, body, hashlib.sha256).digest(),
        )
        for candidate_key in candidate_keys
    ):
        raise PublicSecurityError("State package signature is invalid")
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
        if remote_ip == "testclient":
            return True
        try:
            return ipaddress.ip_address(remote_ip).is_loopback
        except ValueError:
            return False
    if not settings.turnstile_secret or not token:
        return _turnstile_rejected("missing_input")
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
    except httpx.HTTPError:
        return _turnstile_rejected("network_unavailable")
    except ValueError:
        return _turnstile_rejected("invalid_response")
    if not isinstance(payload, dict):
        return _turnstile_rejected("invalid_response")
    if payload.get("success") is not True:
        codes = payload.get("error-codes")
        codes = codes if isinstance(codes, list) else []
        if any(code in codes for code in ("invalid-input-secret", "missing-input-secret")):
            return _turnstile_rejected("secret_invalid")
        if "timeout-or-duplicate" in codes:
            return _turnstile_rejected("token_expired_or_duplicate")
        if "internal-error" in codes:
            return _turnstile_rejected("upstream_unavailable")
        return _turnstile_rejected("token_rejected")
    if (
        settings.allow_insecure_dev
        and settings.turnstile_site_key == TURNSTILE_TEST_SITE_KEY
        and settings.turnstile_secret == TURNSTILE_TEST_SECRET
        and token == TURNSTILE_TEST_TOKEN
    ):
        return True
    returned_action = str(payload.get("action") or "")
    returned_hostname = str(payload.get("hostname") or "").casefold().rstrip(".")
    if not returned_action or not hmac.compare_digest(returned_action, action):
        return _turnstile_rejected("action_mismatch")
    if not returned_hostname or not hmac.compare_digest(
        returned_hostname,
        settings.turnstile_hostname.casefold().rstrip("."),
    ):
        return _turnstile_rejected("hostname_mismatch")
    challenge_timestamp = str(payload.get("challenge_ts") or "").strip()
    if not challenge_timestamp:
        return _turnstile_rejected("timestamp_invalid")
    try:
        challenged_at = datetime.fromisoformat(challenge_timestamp.replace("Z", "+00:00"))
        if challenged_at.tzinfo is None:
            challenged_at = challenged_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - challenged_at.astimezone(UTC)
    except ValueError:
        return _turnstile_rejected("timestamp_invalid")
    if not timedelta(seconds=-30) <= age <= timedelta(seconds=settings.turnstile_max_age_seconds):
        return _turnstile_rejected("timestamp_expired")
    return True


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
