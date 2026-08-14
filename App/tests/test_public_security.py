from __future__ import annotations

import base64
from pathlib import Path
from unittest import TestCase

from backend.snow_app.config import PublicSettings
from backend.snow_app.public_security import (
    PublicSecurityError,
    daily_ip_fingerprint,
    issue_byok_credential,
    open_byok_credential,
    sign_state,
    verify_state,
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
    def test_public_image_uses_a_minimal_runtime_dependency_set(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        dockerfile = (app_root / "infra" / "public-api.Dockerfile").read_text(encoding="utf-8")
        requirements = (app_root / "requirements-public.txt").read_text(encoding="utf-8")

        self.assertIn("COPY requirements-public.txt", dockerfile)
        self.assertIn("COPY config/public_knowledge", dockerfile)
        self.assertIn("cryptography>=50,<51", requirements)
        for excluded in ("sentence-transformers", "torch", "playwright", "pypdf"):
            self.assertNotIn(excluded, requirements.casefold())

        embedding_dockerfile = (app_root / "infra" / "embedding.Dockerfile").read_text(encoding="utf-8")
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
