from __future__ import annotations

import base64
import asyncio
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import yaml

from backend.snow_app.config import (
    PublicSettings,
    TURNSTILE_TEST_SECRET,
    TURNSTILE_TEST_SITE_KEY,
    TURNSTILE_TEST_TOKEN,
)
from backend.snow_app.public_security import (
    BYOK_RETENTION_HOURS,
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
        test_credentials = replace(
            complete,
            allow_insecure_dev=False,
            turnstile_site_key=TURNSTILE_TEST_SITE_KEY,
            turnstile_secret=TURNSTILE_TEST_SECRET,
        )
        self.assertIn("TURNSTILE_TEST_CREDENTIALS", test_credentials.missing_production_secrets())

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
        self.assertIn("msgpack==1.2.3", embedding_dockerfile)
        self.assertIn("&& pip check", embedding_dockerfile)
        self.assertIn("setuptools==84.0.0", embedding_dockerfile)
        self.assertIn("wheel==0.48.0", embedding_dockerfile)
        self.assertIn("/opt/venv/lib/python3.12/site-packages/pip", embedding_dockerfile)
        self.assertIn("/opt/venv/lib/python3.12/site-packages/setuptools", embedding_dockerfile)
        self.assertIn("/opt/venv/lib/python3.12/site-packages/wheel", embedding_dockerfile)

    def test_local_public_compose_uses_turnstile_test_keys_only(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        local_compose = yaml.safe_load((app_root / "compose.yml").read_text(encoding="utf-8"))
        public_env = local_compose["services"]["public-api"]["environment"]
        self.assertEqual(public_env["TURNSTILE_SITE_KEY"], TURNSTILE_TEST_SITE_KEY)
        self.assertEqual(public_env["TURNSTILE_SECRET"], TURNSTILE_TEST_SECRET)
        self.assertEqual(public_env["TURNSTILE_SECRET_FILE"], "")
        self.assertEqual(public_env["PUBLIC_ALLOW_INSECURE_DEV"], "true")

        production_compose = (app_root / "compose.prod.yml").read_text(encoding="utf-8")
        self.assertNotIn(TURNSTILE_TEST_SITE_KEY, production_compose)
        self.assertNotIn(TURNSTILE_TEST_SECRET, production_compose)

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
        with self.assertRaisesRegex(PublicSecurityError, "lifetime is invalid"):
            issue_byok_credential(
                replace(settings, byok_lifetime_hours=13),
                anonymous_id="anonymous-a",
                provider="openai",
                api_key="sk-test-key",
            )

    def test_state_signature_rejects_tampering(self) -> None:
        settings = _settings()
        token = sign_state(settings, {"data_version": "test-data", "revision": 3})
        self.assertEqual(verify_state(settings, token)["revision"], 3)
        with self.assertRaises(PublicSecurityError):
            verify_state(settings, token[:-1] + ("A" if token[-1] != "A" else "B"))

    def test_retention_envelope_has_exact_v2_claims_and_revoke_cutoff(self) -> None:
        settings = replace(_settings(), byok_retention_enabled=True)
        token, expires_at = issue_byok_credential(
            settings,
            anonymous_id="anonymous-a",
            provider="openai",
            api_key="sk-test-key",
            retention="3d",
        )
        claims = open_byok_credential(settings, anonymous_id="anonymous-a", token=token)
        self.assertEqual(claims["v"], 2)
        self.assertEqual(claims["ret"], "3d")
        self.assertEqual(claims["exp"], claims["iat"] + 72 * 3600)
        self.assertEqual(claims["exp"], int(expires_at.timestamp()))
        with self.assertRaises(PublicSecurityError):
            open_byok_credential(
                settings,
                anonymous_id="anonymous-a",
                token=token,
                revoked_before=datetime.now(UTC) + timedelta(seconds=1),
            )

    def test_revoke_cutoff_distinguishes_credentials_issued_within_one_second(self) -> None:
        settings = _settings()
        issued_at = datetime(2026, 10, 20, 12, 0, 0, 100000, tzinfo=UTC)
        cutoff = issued_at + timedelta(microseconds=100000)
        with patch("backend.snow_app.public_security.datetime") as clock:
            clock.now.return_value = issued_at
            before, _ = issue_byok_credential(
                settings, anonymous_id="anonymous-a", provider="openai",
                api_key="sk-test-key", retention="24h",
            )
            clock.now.return_value = cutoff + timedelta(microseconds=100000)
            after, _ = issue_byok_credential(
                settings, anonymous_id="anonymous-a", provider="openai",
                api_key="sk-test-key", retention="24h",
            )
            with self.assertRaisesRegex(PublicSecurityError, "revoked"):
                open_byok_credential(
                    settings, anonymous_id="anonymous-a", token=before,
                    revoked_before=cutoff,
                )
            self.assertEqual(
                open_byok_credential(
                    settings, anonymous_id="anonymous-a", token=after,
                    revoked_before=cutoff,
                )["ret"],
                "24h",
            )

    def test_all_retention_terms_have_exact_claims_and_expire_at_the_term(self) -> None:
        issued_at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        for retention, hours in BYOK_RETENTION_HOURS.items():
            with self.subTest(retention=retention), patch("backend.snow_app.public_security.datetime") as clock:
                clock.now.return_value = issued_at
                token, expires_at = issue_byok_credential(
                    _settings(), anonymous_id="anonymous-a", provider="openai",
                    api_key="sk-test-key", retention=retention,
                )
                claims = open_byok_credential(_settings(), anonymous_id="anonymous-a", token=token)
                self.assertEqual(claims["exp"], int(expires_at.timestamp()))
                if retention == "tab":
                    self.assertEqual(claims["v"], 1)
                    self.assertNotIn("iat", claims)
                    self.assertNotIn("ret", claims)
                else:
                    self.assertEqual(claims["v"], 2)
                    self.assertEqual(claims["ret"], retention)
                    self.assertEqual(claims["exp"], claims["iat"] + hours * 3600)
                clock.now.return_value = issued_at + timedelta(hours=hours, seconds=1)
                with self.assertRaisesRegex(PublicSecurityError, "expired"):
                    open_byok_credential(_settings(), anonymous_id="anonymous-a", token=token)

    def test_v1_and_v2_aad_domains_reject_version_rollback(self) -> None:
        settings = _settings()

        def reseal(token: str, old_aad: bytes, new_aad: bytes) -> str:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload = AESGCM(settings.credential_key).decrypt(raw[:12], raw[12:], old_aad)
            nonce = secrets.token_bytes(12)
            sealed = AESGCM(settings.credential_key).encrypt(nonce, payload, new_aad)
            return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")

        v1, _ = issue_byok_credential(
            settings, anonymous_id="anonymous-a", provider="openai", api_key="sk-test-key", retention="tab"
        )
        self.assertEqual(open_byok_credential(settings, anonymous_id="anonymous-a", token=v1)["v"], 1)
        with self.assertRaises(PublicSecurityError):
            open_byok_credential(
                settings, anonymous_id="anonymous-a",
                token=reseal(v1, b"project-snow-byok-v1", b"project-snow-byok-v2"),
            )

        v2, _ = issue_byok_credential(
            settings, anonymous_id="anonymous-a", provider="openai", api_key="sk-test-key", retention="24h"
        )
        self.assertEqual(open_byok_credential(settings, anonymous_id="anonymous-a", token=v2)["v"], 2)
        with self.assertRaises(PublicSecurityError):
            open_byok_credential(
                settings, anonymous_id="anonymous-a",
                token=reseal(v2, b"project-snow-byok-v2", b"project-snow-byok-v1"),
            )

    def test_relay_v3_envelope_seals_base_and_keeps_v1_v2_open(self) -> None:
        settings = _settings()
        relay_base = "https://relay.example.com/v1"
        token, _ = issue_byok_credential(
            settings, anonymous_id="anonymous-a", provider="relay",
            api_key="sk-relay-key", relay_base=relay_base,
        )
        claims = open_byok_credential(
            settings, anonymous_id="anonymous-a", token=token, expected_provider="relay",
        )
        self.assertEqual(claims["v"], 3)
        self.assertEqual(claims["relay_base"], relay_base)
        self.assertEqual(claims["provider"], "relay")
        # An official provider must not carry a sealed base.
        with self.assertRaises(PublicSecurityError):
            issue_byok_credential(
                settings, anonymous_id="anonymous-a", provider="openai",
                api_key="sk-test-key", relay_base=relay_base,
            )
        # A relay provider must carry a sealed base.
        with self.assertRaises(PublicSecurityError):
            issue_byok_credential(
                settings, anonymous_id="anonymous-a", provider="relay", api_key="sk-relay-key",
            )
        # v1 (tab) and v2 (retained) still open after v3 was added.
        retained = replace(settings, byok_retention_enabled=True)
        v1, _ = issue_byok_credential(
            settings, anonymous_id="anonymous-a", provider="openai", api_key="sk-test-key", retention="tab")
        self.assertEqual(open_byok_credential(settings, anonymous_id="anonymous-a", token=v1)["v"], 1)
        v2, _ = issue_byok_credential(
            retained, anonymous_id="anonymous-a", provider="openai", api_key="sk-test-key", retention="24h")
        self.assertEqual(open_byok_credential(retained, anonymous_id="anonymous-a", token=v2)["v"], 2)

    def test_retention_envelope_rejects_non_whitelisted_duration(self) -> None:
        settings = replace(_settings(), byok_retention_enabled=True)
        short_tab = replace(settings, byok_lifetime_hours=2)
        token, expires_at = issue_byok_credential(
            short_tab,
            anonymous_id="anonymous-a",
            provider="openai",
            api_key="sk-test-key",
            retention="tab",
        )
        self.assertGreaterEqual(expires_at - datetime.now(UTC), timedelta(hours=1, minutes=59))
        self.assertLessEqual(expires_at - datetime.now(UTC), timedelta(hours=2, seconds=1))
        self.assertEqual(
            open_byok_credential(short_tab, anonymous_id="anonymous-a", token=token)["v"],
            1,
        )
        with self.assertRaises(PublicSecurityError):
            issue_byok_credential(
                settings,
                anonymous_id="anonymous-a",
                provider="openai",
                api_key="sk-test-key",
                retention="24h",
                lifetime=timedelta(hours=23),
            )

    def test_v2_requires_valid_millisecond_issued_at_claim(self) -> None:
        settings = _settings()
        token, _ = issue_byok_credential(
            settings,
            anonymous_id="anonymous-a",
            provider="openai",
            api_key="sk-test-key",
            retention="24h",
        )

        def reseal(mutator) -> str:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload = AESGCM(settings.credential_key).decrypt(
                raw[:12], raw[12:], b"project-snow-byok-v2"
            )
            claims = json.loads(payload)
            mutator(claims)
            nonce = secrets.token_bytes(12)
            sealed = AESGCM(settings.credential_key).encrypt(
                nonce,
                json.dumps(claims, separators=(",", ":")).encode(),
                b"project-snow-byok-v2",
            )
            return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")

        mutators = {
            "missing": lambda claims: claims.pop("iat_ms"),
            "type": lambda claims: claims.__setitem__("iat_ms", "not-an-int"),
            "inconsistent": lambda claims: claims.__setitem__("iat_ms", claims["iat_ms"] + 1000),
            "future": lambda claims: (
                claims.__setitem__("iat", claims["iat"] + 31),
                claims.__setitem__("iat_ms", claims["iat_ms"] + 31_000),
                claims.__setitem__("exp", claims["exp"] + 31),
            ),
        }
        for label, mutate in mutators.items():
            with self.subTest(mutate=label), self.assertRaisesRegex(
                PublicSecurityError, "issued time is invalid"
            ):
                open_byok_credential(
                    settings,
                    anonymous_id="anonymous-a",
                    token=reseal(mutate),
                )

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
            Client.payload = {
                "success": True,
                "action": "test",
                "hostname": "localhost",
                "challenge_ts": "2022-02-28T15:14:30.096Z",
            }
            local_test_settings = replace(
                settings,
                allow_insecure_dev=True,
                turnstile_site_key=TURNSTILE_TEST_SITE_KEY,
                turnstile_secret=TURNSTILE_TEST_SECRET,
            )
            self.assertTrue(
                asyncio.run(
                    verify_turnstile(
                        local_test_settings,
                        TURNSTILE_TEST_TOKEN,
                        "172.18.0.1",
                        action="byok-session",
                    )
                )
            )
            self.assertFalse(
                asyncio.run(
                    verify_turnstile(
                        local_test_settings,
                        "not-the-cloudflare-test-token",
                        "127.0.0.1",
                        action="byok-session",
                    )
                )
            )
            production_test_settings = replace(local_test_settings, allow_insecure_dev=False)
            self.assertFalse(
                asyncio.run(
                    verify_turnstile(
                        production_test_settings,
                        TURNSTILE_TEST_TOKEN,
                        "127.0.0.1",
                        action="byok-session",
                    )
                )
            )

    def test_turnstile_failure_diagnostics_never_include_private_inputs(self) -> None:
        import httpx

        class Response:
            def json(self):
                return Client.payload

        class Client:
            payload = {}
            offline = False

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                if self.offline:
                    raise httpx.ConnectError("DO_NOT_LOG network diagnostics")
                return Response()

        settings = replace(_settings(), allow_insecure_dev=False,
                           turnstile_secret="DO_NOT_LOG secret")
        valid = {"success": True, "action": "byok-session", "hostname": "snow.xiaob.dev",
                 "challenge_ts": datetime.now(UTC).isoformat()}
        cases = [
            ([], "invalid_response"),
            ({**valid, "success": "true"}, "token_rejected"),
            ({"success": False, "error-codes": ["invalid-input-secret", "DO_NOT_LOG payload"]}, "secret_invalid"),
            ({"success": False, "error-codes": ["timeout-or-duplicate"]}, "token_expired_or_duplicate"),
            ({"success": False, "error-codes": ["internal-error"]}, "upstream_unavailable"),
            ({**valid, "action": "DO_NOT_LOG action"}, "action_mismatch"),
            ({**valid, "hostname": "DO_NOT_LOG hostname"}, "hostname_mismatch"),
            ({**valid, "challenge_ts": "DO_NOT_LOG timestamp"}, "timestamp_invalid"),
            ({**valid, "challenge_ts": (datetime.now(UTC) - timedelta(minutes=10)).isoformat()}, "timestamp_expired"),
        ]
        with patch("backend.snow_app.public_security.httpx.AsyncClient", Client):
            for payload, reason in cases:
                with self.subTest(reason=reason):
                    Client.payload = payload
                    with self.assertLogs("snow.public", level="WARNING") as records:
                        accepted = asyncio.run(verify_turnstile(
                            settings, "DO_NOT_LOG token", "203.0.113.123", action="byok-session"))
                    self.assertFalse(accepted)
                    self.assertEqual([record.getMessage() for record in records.records],
                                     ["public_turnstile_" + reason])
                    self.assertNotIn("DO_NOT_LOG", "".join(records.output))
                    self.assertNotIn("203.0.113.123", "".join(records.output))
            Client.offline = True
            with self.assertLogs("snow.public", level="WARNING") as records:
                self.assertFalse(asyncio.run(verify_turnstile(
                    settings, "DO_NOT_LOG token", "203.0.113.123", action="byok-session")))
            self.assertEqual(records.records[0].getMessage(), "public_turnstile_network_unavailable")
            Client.offline = False
            Client.payload = valid
            with self.assertNoLogs("snow.public", level="WARNING"):
                self.assertTrue(asyncio.run(verify_turnstile(
                    settings, "DO_NOT_LOG token", "203.0.113.123", action="byok-session")))
