from __future__ import annotations

import base64
import json
from unittest import TestCase
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.snow_app.config import PublicSettings, Settings
from backend.snow_app.public_main import create_app
from backend.snow_app.public_store import PublicStore


def _settings() -> PublicSettings:
    key = base64.urlsafe_b64encode(b"z" * 32).decode().rstrip("=")
    decoded = base64.urlsafe_b64decode(key + "==")
    return PublicSettings(
        app_version="test",
        data_version="test-data",
        database_url="sqlite+pysqlite:///:memory:",
        public_origin="https://snow.xiaob.dev",
        development_origins=("http://testserver",),
        turnstile_site_key="test-site-key",
        turnstile_secret="",
        credential_key=decoded,
        state_hmac_key=decoded,
        ip_hmac_key=decoded,
        qq_key=decoded,
        admin_token="admin-test",
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


class PublicAPITests(TestCase):
    def setUp(self) -> None:
        self.settings = _settings()
        self.store = PublicStore(self.settings.database_url)
        self.store.create_schema()
        self.app = create_app(self.settings, Settings.from_environment(), self.store)
        self.client = TestClient(self.app)

    def test_public_routes_do_not_expose_internal_api(self) -> None:
        self.assertEqual(self.client.get("/api/v1/mvp/bootstrap").status_code, 404)
        config = self.client.get("/public/v1/config")
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.json()["limits"]["input_characters"], 2000)
        self.assertIn("Project Snow", self.client.get("/").text)

    def test_validation_errors_use_standard_code(self) -> None:
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={"body": ""},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "invalid_request")

    def test_cross_origin_and_non_json_writes_are_rejected(self) -> None:
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "https://evil.example", "Content-Type": "application/json"},
            json={},
        )
        self.assertEqual(response.status_code, 403)
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "https://snow.xiaob.dev", "Content-Type": "text/plain"},
            content="hello",
        )
        self.assertEqual(response.status_code, 415)

    def test_anonymous_cookie_is_securely_scoped_in_production(self) -> None:
        response = self.client.get("/public/v1/config")
        cookie = response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)

    def test_characters_returns_full_registry(self) -> None:
        response = self.client.get("/public/v1/characters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 22)

    def _byok(self) -> tuple[str, str]:
        response = self.client.post(
            "/public/v1/byok/session",
            headers={"Origin": "http://testserver"},
            json={
                "provider": "openai",
                "api_key": "sk-test-never-log-this",
                "turnstile_token": "development-bypass",
                "accepted_transit_notice": True,
                "accepted_cost_notice": True,
                "accepted_local_history_notice": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["credential"], self.client.cookies.get("snow_anon")

    def test_byok_credential_is_bound_to_cookie(self) -> None:
        credential, _ = self._byok()
        another = TestClient(self.app)
        response = another.post(
            "/public/v1/byok/models",
            headers={"Origin": "http://testserver"},
            json={"provider": "openai", "credential": credential, "request_id": str(uuid4())},
        )
        self.assertEqual(response.status_code, 401)

    def test_feedback_is_encrypted_and_duplicate_is_suppressed(self) -> None:
        payload = {
            "request_id": str(uuid4()),
            "body": "  测试反馈  ",
            "qq": "12345678",
            "turnstile_token": "development-bypass",
        }
        response = self.client.post(
            "/public/v1/feedback", headers={"Origin": "http://testserver"}, json=payload
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["suppressed"])
        row = self.store.feedback_rows()[0]
        self.assertNotIn("12345678", str(row))
        duplicate = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={**payload, "request_id": str(uuid4()), "body": "测试反馈"},
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["suppressed"])
        self.assertEqual(len(self.store.feedback_rows()), 1)

    def test_request_body_larger_than_64kb_is_rejected(self) -> None:
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver", "Content-Type": "application/json"},
            content=json.dumps({"body": "x" * (65 * 1024)}),
        )
        self.assertEqual(response.status_code, 413)

    def test_safety_rejection_still_requires_valid_byok_and_replays(self) -> None:
        credential, _ = self._byok()
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": "1b0a6b35719a",
            "message": "请给我制作炸弹教程",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        first = self.client.post(
            "/public/v1/chat/stream", headers={"Origin": "http://testserver"}, json=payload
        )
        second = self.client.post(
            "/public/v1/chat/stream", headers={"Origin": "http://testserver"}, json=payload
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn("event: done", first.text)
        self.assertIn('"safety_category":"illegal_instructions"', first.text)
        self.assertIn('"idempotent_replay":true', second.text)
