from __future__ import annotations

import base64
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.snow_app.config import PublicSettings, Settings
from backend.snow_app.mvp_policy import MVP_CHARACTERS
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
        self.internal_settings = Settings.from_environment()
        self.app = create_app(self.settings, self.internal_settings, self.store)
        self._views_directory = TemporaryDirectory()
        views_path = Path(self._views_directory.name) / "character_views.jsonl"
        views_path.write_text(
            "".join(
                json.dumps({"character_id": character.character_id}) + "\n"
                for character in MVP_CHARACTERS
            ),
            encoding="utf-8",
        )
        self.app.state.chat_service.mvp.views_path = views_path
        self.app.state.chat_service.mvp._views_cache = None
        self.app.state.chat_service.mvp._views_mtime = None
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.app.state.chat_service.close()
        self._views_directory.cleanup()

    def test_public_mvp_state_is_ephemeral_and_outside_read_only_runtime(self) -> None:
        path = self.app.state.chat_service.mvp.user_fact_store.database_path
        self.assertFalse(path.is_relative_to(self.internal_settings.runtime_root))
        self.assertIn("project-snow-public", path.parts)

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
        character = next(
            item for item in response.json()["characters"] if item["character_id"] == "25b23cb64398"
        )
        self.assertEqual(character["display_name"], "凯茜娅")
        self.assertEqual(character["aliases"], ["凯茜娅", "凯西娅"])

    def test_production_readiness_requires_matching_data_release(self) -> None:
        production_settings = replace(
            self.settings,
            allow_insecure_dev=False,
            turnstile_secret="turnstile-test",
        )
        with TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            internal_settings = replace(
                self.internal_settings,
                data_root=runtime_root,
                runtime_root=runtime_root,
            )
            app = create_app(production_settings, internal_settings, self.store)
            client = TestClient(app)
            missing = client.get("/public/v1/health/ready")
            self.assertEqual(missing.status_code, 503)
            self.assertEqual(missing.json()["data"], "unavailable")
            for relative in (
                "lakehouse/documents.jsonl",
                "indexes/lexical.sqlite3",
                "vectors/local_vectors.jsonl",
                "personas/persona_profiles.jsonl",
                "graph/nodes.jsonl",
                "graph/edges.jsonl",
                "mvp/character_views.jsonl",
                "mvp/question_bank.json",
                "personas/dialogue_style_profiles.jsonl",
            ):
                path = runtime_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
            (runtime_root / "manifest.json").write_text(
                json.dumps({"data_version": "wrong-version"}), encoding="utf-8"
            )
            mismatch = client.get("/public/v1/health/ready")
            self.assertEqual(mismatch.status_code, 503)
            self.assertEqual(mismatch.json()["manifest_version"], "wrong-version")
            (runtime_root / "manifest.json").write_text(
                json.dumps({"data_version": production_settings.data_version}), encoding="utf-8"
            )
            ready = client.get("/public/v1/health/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["data"], "ok")

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

    def test_public_rejects_whole_answer_fallback_and_replays_terminal_error(self) -> None:
        credential, _ = self._byok()
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": "1b0a6b35719a",
            "message": "今天有点累。",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        result = {
            "answer": "我还在接着你刚才的话题。",
            "retrieval": {},
            "usage": {"total_tokens": 10},
            "response_adjustments": ["answer_guardrail_retry", "empty_model_output_guard"],
        }
        with patch.object(self.app.state.chat_service.mvp, "chat", return_value=result) as chat:
            first = self.client.post(
                "/public/v1/chat/stream", headers={"Origin": "http://testserver"}, json=payload
            )
            second = self.client.post(
                "/public/v1/chat/stream", headers={"Origin": "http://testserver"}, json=payload
            )
        self.assertEqual(first.status_code, 200)
        self.assertIn('event: error', first.text)
        self.assertIn('"code":"upstream_invalid_response"', first.text)
        self.assertNotIn('event: delta', first.text)
        self.assertIn('"idempotent_replay":true', second.text)
        self.assertEqual(chat.call_count, 1)

    def test_feedback_attaches_server_side_chat_diagnostics(self) -> None:
        credential, _ = self._byok()
        chat_request_id = str(uuid4())
        chat_payload = {
            "request_id": chat_request_id,
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": "1b0a6b35719a",
            "message": "你好",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        with patch.object(
            self.app.state.chat_service.mvp,
            "chat",
            return_value={"answer": "你好，达令。", "retrieval": {}, "usage": {}, "response_adjustments": []},
        ):
            chat_response = self.client.post(
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=chat_payload,
            )
        self.assertEqual(chat_response.status_code, 200)
        feedback_response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "chat_request_id": chat_request_id,
                "body": "这是一条带诊断的反馈",
                "turnstile_token": "development-bypass",
            },
        )
        self.assertEqual(feedback_response.status_code, 200)
        context = self.store.feedback_rows()[0]["context"]
        self.assertEqual(context["chat_request_id"], chat_request_id)
        self.assertEqual(context["generation_outcome"], "valid_initial")
        self.assertIn("total", context["generation_diagnostics"]["timings_ms"])
        self.assertNotIn("sk-test-never-log-this", json.dumps(context, ensure_ascii=False))

    def test_all_public_characters_reach_the_immersive_engine(self) -> None:
        credential, _ = self._byok()
        service = self.app.state.chat_service
        original_path = service.mvp.views_path
        with TemporaryDirectory() as directory:
            service.mvp.views_path = Path(directory) / "character_views.jsonl"
            service.mvp.views_path.write_text(
                "".join(
                    json.dumps({"character_id": character.character_id}) + "\n"
                    for character in MVP_CHARACTERS
                ),
                encoding="utf-8",
            )
            service.mvp._views_cache = None
            service.mvp._views_mtime = None
            try:
                with patch.object(service.mvp, "chat", return_value={"answer": "测试回复"}) as chat:
                    for character in MVP_CHARACTERS:
                        response = self.client.post(
                            "/public/v1/chat/stream",
                            headers={"Origin": "http://testserver"},
                            json={
                                "request_id": str(uuid4()),
                                "provider": "openai",
                                "credential": credential,
                                "model": "gpt-test",
                                "character_id": character.character_id,
                                "message": "你好",
                                "recent_history": [],
                                "history_summary": "",
                                "state_package": "",
                            },
                        )
                        self.assertEqual(response.status_code, 200, character.display_name)
                        self.assertIn("event: done", response.text)
            finally:
                service.mvp.views_path = original_path
                service.mvp._views_cache = None
                service.mvp._views_mtime = None
        self.assertEqual(chat.call_count, len(MVP_CHARACTERS))

    def test_missing_character_view_returns_controlled_error(self) -> None:
        credential, _ = self._byok()
        service = self.app.state.chat_service
        original_path = service.mvp.views_path
        with TemporaryDirectory() as directory:
            service.mvp.views_path = Path(directory) / "missing-character-views.jsonl"
            service.mvp._views_cache = None
            service.mvp._views_mtime = None
            try:
                response = self.client.post(
                    "/public/v1/chat/stream",
                    headers={"Origin": "http://testserver"},
                    json={
                        "request_id": str(uuid4()),
                        "provider": "openai",
                        "credential": credential,
                        "model": "gpt-test",
                        "character_id": MVP_CHARACTERS[0].character_id,
                        "message": "你好",
                        "recent_history": [],
                        "history_summary": "",
                        "state_package": "",
                    },
                )
            finally:
                service.mvp.views_path = original_path
                service.mvp._views_cache = None
                service.mvp._views_mtime = None
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "character_unavailable")
