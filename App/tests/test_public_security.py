from __future__ import annotations

import base64
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from backend.snow_app.config import PublicSettings
from backend.snow_app.public_security import (
    PublicSecurityError,
    daily_ip_fingerprint,
    issue_byok_credential,
    open_byok_credential,
    sign_state,
    verify_state,
    verify_turnstile,
)


def _settings() -> PublicSettings:
    key = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    return PublicSettings(
        app_version="test",
        data_version="test-data",
        database_url="",
        public_origin="https://snow.xiaob.dev",
        development_origins=("http://testserver",),
        turnstile_site_key="",
        turnstile_secret="",
        credential_key=base64.urlsafe_b64decode(key + "=="),
        state_hmac_key=base64.urlsafe_b64decode(key + "=="),
        ip_hmac_key=base64.urlsafe_b64decode(key + "=="),
        qq_key=base64.urlsafe_b64decode(key + "=="),
        admin_token="test",
        enabled_providers=("openai",),
        allow_insecure_dev=True,
        qdrant_url="http://qdrant",
        qdrant_collection="test",
        qdrant_api_key="qdrant-test",
        embedding_url="http://embedding",
        neo4j_uri="bolt://neo4j",
        neo4j_user="neo4j",
        neo4j_password="",
        auto_create_schema=True,
        trust_proxy_headers=False,
    )


class PublicSecurityTests(TestCase):
    def test_production_readiness_requires_turnstile_site_and_neo4j_credentials(self) -> None:
        complete = replace(
            _settings(),
            database_url="postgresql+psycopg://project_snow@postgres/project_snow",
            turnstile_site_key="site-key",
            turnstile_secret="secret",
            neo4j_password="neo4j-secret",
        )
        self.assertNotIn("TURNSTILE_SITE_KEY", complete.missing_production_secrets())
        self.assertNotIn("NEO4J_PASSWORD", complete.missing_production_secrets())
        missing = replace(complete, turnstile_site_key="", neo4j_password="")
        self.assertIn("TURNSTILE_SITE_KEY", missing.missing_production_secrets())
        self.assertIn("NEO4J_PASSWORD", missing.missing_production_secrets())

    def test_public_image_uses_a_minimal_runtime_dependency_set(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        dockerfile = (app_root / "infra" / "public-api.Dockerfile").read_text(encoding="utf-8")
        requirements = (app_root / "requirements-public.txt").read_text(encoding="utf-8")

        self.assertIn("COPY requirements-public.txt", dockerfile)
        self.assertIn("COPY config/public_knowledge", dockerfile)
        self.assertIn("cryptography==50.0.0", requirements)
        self.assertIn("--hash=sha256:", requirements)
        for excluded in ("sentence-transformers", "torch", "playwright", "pypdf"):
            self.assertNotIn(excluded, requirements.casefold())

        embedding_dockerfile = (app_root / "infra" / "embedding.Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS system-base",
            embedding_dockerfile,
        )
        self.assertEqual(embedding_dockerfile.count("FROM system-base"), 2)
        self.assertIn("apt-get update", embedding_dockerfile)
        self.assertIn("apt-get upgrade -y --no-install-recommends", embedding_dockerfile)
        self.assertIn("rm -rf /var/lib/apt/lists/*", embedding_dockerfile)
        self.assertIn("AS builder", embedding_dockerfile)
        self.assertIn("COPY --from=builder /opt/venv /opt/venv", embedding_dockerfile)
        self.assertIn("https://download.pytorch.org/whl/cpu", embedding_dockerfile)
        self.assertIn("--no-deps", embedding_dockerfile)
        self.assertIn("torch==2.13.0+cpu", embedding_dockerfile)
        self.assertIn("transformers==5.15.0", embedding_dockerfile)
        self.assertIn("msgpack==1.2.1", embedding_dockerfile)
        self.assertIn("&& pip check", embedding_dockerfile)
        self.assertIn("setuptools==84.0.0", embedding_dockerfile)
        self.assertIn("wheel==0.48.0", embedding_dockerfile)
        self.assertIn("/opt/venv/lib/python3.12/site-packages/pip", embedding_dockerfile)
        self.assertIn("/opt/venv/lib/python3.12/site-packages/setuptools", embedding_dockerfile)
        self.assertIn("/opt/venv/lib/python3.12/site-packages/wheel", embedding_dockerfile)

    def test_byok_credential_is_bound_to_anonymous_session_and_provider(self) -> None:
        settings = _settings()
        token, _ = issue_byok_credential(
            settings, anonymous_id="anonymous-a", provider="openai", api_key="sk-test-key"
        )
        claims = open_byok_credential(
            settings, anonymous_id="anonymous-a", token=token, expected_provider="openai"
        )
        self.assertEqual(claims["api_key"], "sk-test-key")
        with self.assertRaises(PublicSecurityError):
            open_byok_credential(settings, anonymous_id="anonymous-b", token=token)
        with self.assertRaises(PublicSecurityError):
            open_byok_credential(settings, anonymous_id="anonymous-a", token=token, expected_provider="deepseek")

    def test_byok_credential_has_absolute_twelve_hour_default_and_legacy_lifetime(self) -> None:
        settings = _settings()
        issued_at = datetime.now(UTC)
        token, expires_at = issue_byok_credential(
            settings,
            anonymous_id="anonymous-a",
            provider="openai",
            api_key="sk-test-key",
        )
        self.assertGreaterEqual(expires_at - issued_at, timedelta(hours=11, minutes=59))
        self.assertLessEqual(expires_at - issued_at, timedelta(hours=12, seconds=5))
        claims = open_byok_credential(
            settings,
            anonymous_id="anonymous-a",
            token=token,
            expected_provider="openai",
        )
        self.assertEqual(claims["exp"], int(expires_at.timestamp()))

        legacy, legacy_expiry = issue_byok_credential(
            settings,
            anonymous_id="anonymous-a",
            provider="openai",
            api_key="sk-test-key",
            lifetime=timedelta(hours=2),
        )
        self.assertLess(legacy_expiry, expires_at)
        self.assertEqual(
            open_byok_credential(
                settings,
                anonymous_id="anonymous-a",
                token=legacy,
                expected_provider="openai",
            )["api_key"],
            "sk-test-key",
        )
        with self.assertRaises(PublicSecurityError):
            issue_byok_credential(
                settings,
                anonymous_id="anonymous-a",
                provider="openai",
                api_key="sk-test-key",
                lifetime=timedelta(hours=13),
            )

    def test_state_signature_rejects_tampering(self) -> None:
        settings = _settings()
        token = sign_state(settings, {"data_version": "test-data", "revision": 3})
        self.assertEqual(verify_state(settings, token)["revision"], 3)
        with self.assertRaises(PublicSecurityError):
            verify_state(settings, token[:-1] + ("A" if token[-1] != "A" else "B"))

    def test_ip_fingerprint_rotates_by_day(self) -> None:
        settings = _settings()
        self.assertNotEqual(
            daily_ip_fingerprint(settings, "203.0.113.9", "2026-08-13"),
            daily_ip_fingerprint(settings, "203.0.113.9", "2026-08-14"),
        )

    def test_state_key_rotation_accepts_only_the_named_previous_key(self) -> None:
        old = replace(_settings(), state_hmac_key=b"o" * 32, state_key_id="old")
        token = sign_state(old, {"schema_version": "public-state-2", "revision": 3})
        rotated = replace(
            old,
            state_hmac_key=b"n" * 32,
            state_hmac_previous_key=b"o" * 32,
            state_key_id="new",
            state_previous_key_id="old",
        )
        self.assertEqual(verify_state(rotated, token)["revision"], 3)
        with self.assertRaises(PublicSecurityError):
            verify_state(replace(rotated, state_previous_key_id="other"), token)

    def test_turnstile_requires_exact_action_hostname_and_fresh_timestamp(self) -> None:
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class Client:
            payload = {}

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return Response(self.payload)

        settings = replace(
            _settings(),
            allow_insecure_dev=False,
            turnstile_secret="secret",
            turnstile_hostname="snow.xiaob.dev",
            turnstile_max_age_seconds=300,
        )
        valid = {
            "success": True,
            "action": "feedback",
            "hostname": "snow.xiaob.dev",
            "challenge_ts": datetime.now(UTC).isoformat(),
        }
        with patch("backend.snow_app.public_security.httpx.AsyncClient", Client):
            Client.payload = valid
            self.assertTrue(asyncio.run(verify_turnstile(settings, "token", "127.0.0.1", action="feedback")))
            Client.payload = {**valid, "action": ""}
            self.assertFalse(asyncio.run(verify_turnstile(settings, "token", "127.0.0.1", action="feedback")))
            Client.payload = {**valid, "hostname": "evil.example"}
            self.assertFalse(asyncio.run(verify_turnstile(settings, "token", "127.0.0.1", action="feedback")))
