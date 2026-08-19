from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.snow_app.config import PublicSettings, Settings
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_contracts import ChatRequest, HistoryTurn
from backend.snow_app.public_main import create_app
from backend.snow_app.public_service import GenerationBusy, PublicChatService
from backend.snow_app.public_security import sign_state, verify_state
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
        payload = config.json()
        self.assertEqual(payload["limits"]["input_characters"], 2000)
        self.assertEqual(payload["history_schema"], "indexeddb-v4")
        self.assertEqual(payload["sticker_version"], "2026.08.19.sticker.1")
        self.assertEqual(payload["privacy_policy"]["version"], "0.9")
        self.assertEqual(payload["privacy_policy"]["url"], "/privacy/")
        self.assertEqual(payload["attribution_url"], "/public/v1/attributions")
        self.assertEqual(payload["max_provider_calls_per_action"], 2)
        self.assertTrue(payload["movement_catalog"])
        self.assertTrue(payload["feature_flags"]["joint_movement"])
        self.assertTrue(payload["feature_flags"]["request_recovery"])
        self.assertTrue(payload["feature_flags"]["indexeddb_v4"])
        self.assertTrue(payload["providers"][0]["documentation_url"].startswith("https://"))
        self.assertTrue(payload["providers"][0]["privacy_url"].startswith("https://"))
        self.assertIn("Project Snow", self.client.get("/").text)

    def test_joint_move_requires_named_current_request_and_immediate_acceptance(self) -> None:
        service: PublicChatService = self.app.state.chat_service
        character = MVP_CHARACTERS[0]
        request = ChatRequest(
            request_id=uuid4(),
            provider="openai",
            credential="c" * 24,
            model="gpt-test",
            character_id=character.character_id,
            message="我们去商场逛街吧",
        )
        self.assertEqual(service._joint_move_intent(request.message)[0], "shopping_mall")
        self.assertEqual(service._joint_move_intent("我想带角色逛街")[0], "commercial_street")
        self.assertEqual(service._joint_move_intent("陪她去公园散步")[0], "park")
        self.assertIsNone(service._joint_move_intent("我们明天去商场逛街吧"))
        self.assertEqual(
            service._joint_move_intent("要不要一起去商场逛街？")[0],
            "shopping_mall",
        )
        self.assertEqual(
            service._joint_move_intent("想不想一起去公园散步？")[0],
            "park",
        )
        self.assertEqual(
            service._joint_move_intent("好不好一起去基地海滩？")[0],
            "base_beach",
        )
        self.assertIsNone(service._joint_move_intent("能不能陪我去基地食堂？"))
        catalog_ids = {item["location_id"] for item in service.movement_catalog()}
        self.assertEqual(len(catalog_ids), 13)
        self.assertNotIn("base_canteen", catalog_ids)
        self.assertTrue({
            "base_beach",
            "base_arcade",
            "base_hot_spring",
            "base_healing_center",
            "base_bar",
        }.issubset(catalog_ids))
        state = {
            "schema_version": "public-state-2",
            "data_version": "test-data",
            "revision": 4,
            "analyst_location": None,
            "presence": {
                character.character_id: {
                    "character_id": character.character_id,
                    "character_name": character.display_name,
                    "location": "观景区",
                    "activity": "正在看雪",
                    "state_scope": "shared_daily",
                }
            },
            "relationships": {},
            "recent_events": [],
            "schedule_date": "2026-08-18",
            "schedule_revision": 1,
            "generated_at": "",
            "expires_at": "",
        }
        next_state, event, diagnostics = service._apply_joint_movement(
            state,
            request,
            {
                "answer": "好，我们去商场，现在走。",
                "state_updates": [{
                    "type": "joint_move",
                    "location_id": "shopping_mall",
                    "activity_id": "shopping_together",
                    "commit": "now",
                }],
            },
        )
        self.assertEqual(diagnostics["state_update_status"], "applied")
        self.assertEqual(next_state["analyst_location"], "购物中心")
        self.assertEqual(next_state["presence"][character.character_id]["location"], "购物中心")
        self.assertEqual(event.event_type, "joint_movement")
        self.assertNotIn("scene_state", next_state)
        replay_state, replay_event, replay_diagnostics = service._apply_joint_movement(
            next_state,
            request,
            {
                "answer": "好，我们去商场，现在走。",
                "state_updates": [{
                    "type": "joint_move",
                    "location_id": "shopping_mall",
                    "activity_id": "shopping_together",
                    "commit": "now",
                }],
            },
        )
        self.assertEqual(replay_diagnostics["state_update_status"], "already_applied")
        self.assertEqual(replay_state["revision"], next_state["revision"])
        self.assertEqual(replay_event.event_id, event.event_id)

    def test_joint_move_continuation_target_presence_and_advisory_proposal(self) -> None:
        service: PublicChatService = self.app.state.chat_service
        speaker, target = MVP_CHARACTERS[:2]
        state = {
            "schema_version": "public-state-2",
            "data_version": "test-data",
            "revision": 2,
            "analyst_location": "基地休息区",
            "presence": {
                speaker.character_id: {
                    "character_id": speaker.character_id,
                    "character_name": speaker.display_name,
                    "location": "基地休息区",
                    "activity": "正在休息",
                    "state_scope": "conversation_confirmed",
                },
                target.character_id: {
                    "character_id": target.character_id,
                    "character_name": target.display_name,
                    "location": "观景区",
                    "activity": "正在看风景",
                    "state_scope": "shared_daily",
                },
            },
            "relationships": {},
            "recent_events": [],
            "schedule_date": "2026-08-19",
            "schedule_revision": 1,
            "generated_at": "",
            "expires_at": "",
        }
        target_message = f"我们去找{target.display_name}吧"
        target_intent = service._joint_move_intent(
            target_message,
            [],
            state,
            speaker.character_id,
        )
        self.assertEqual(target_intent[0], "observation")
        self.assertEqual(target_intent[1]["target_character_id"], target.character_id)

        legacy_canteen_state = json.loads(json.dumps(state))
        legacy_canteen_state["presence"][target.character_id]["location"] = "基地食堂"
        canteen_target = service._joint_move_intent(
            target_message,
            [],
            legacy_canteen_state,
            speaker.character_id,
        )
        self.assertEqual(canteen_target[0], "base_canteen")
        self.assertEqual(canteen_target[1]["resolution"], "target_presence")

        continuation = service._joint_move_intent(
            "那就走吧",
            [
                HistoryTurn(role="user", content="要不要一起去商场？"),
                HistoryTurn(role="assistant", content="当然愿意。"),
            ],
            state,
            speaker.character_id,
        )
        self.assertEqual(continuation[0], "shopping_mall")
        self.assertEqual(continuation[1]["resolution"], "history_continuation")

        request = ChatRequest(
            request_id=uuid4(),
            provider="openai",
            credential="c" * 24,
            model="gpt-test",
            character_id=speaker.character_id,
            message="要不要一起去商场？",
        )
        moved, event, diagnostics = service._apply_joint_movement(
            state,
            request,
            {
                "answer": "当然，走吧。",
                "state_updates": [{
                    "type": "joint_move",
                    "location_id": "park",
                    "activity_id": "strolling_together",
                    "commit": "now",
                }],
            },
        )
        self.assertEqual(moved["analyst_location"], "购物中心")
        self.assertEqual(event.location_id, "shopping_mall")
        self.assertEqual(diagnostics["model_proposal_status"], "mismatch_ignored")

    def test_joint_move_rejects_future_or_noncommittal_answer(self) -> None:
        service: PublicChatService = self.app.state.chat_service
        self.assertIsNone(service._joint_move_intent("我们明天一起去公园吧"))
        self.assertFalse(service._joint_move_is_accepted("可以吗？"))
        self.assertFalse(service._joint_move_is_accepted("改天再去吧。"))
        self.assertTrue(service._joint_move_is_accepted("好呀，现在出发。"))
        self.assertTrue(service._joint_move_is_accepted("好。"))
        self.assertTrue(service._joint_move_is_accepted("正合我意，这就动身。"))
        self.assertTrue(service._joint_move_is_accepted("乐意奉陪，就这么定。"))
        self.assertFalse(service._joint_move_is_accepted("你好，分析员。"))
        self.assertFalse(service._joint_move_is_accepted("不好吧。"))
        self.assertFalse(service._joint_move_is_accepted("算了，先不动身。"))
        self.assertFalse(service._joint_move_is_accepted("好，等会儿再动身吧。"))
        self.assertFalse(service._joint_move_is_accepted("可以，你可以先去等我。"))

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
        production_settings = replace(
            self.settings,
            allow_insecure_dev=False,
            turnstile_secret="turnstile-test",
            neo4j_password="neo4j-test",
        )
        with (
            patch(
                "backend.snow_app.public_media.PublicMediaCatalog.verify",
                return_value={"status": "ok"},
            ),
            patch(
                "backend.snow_app.public_stickers.PublicStickerCatalog.verify",
                return_value={"status": "ok", "sticker_count": 363},
            ),
        ):
            app = create_app(production_settings, self.internal_settings, self.store)
        with TestClient(app, base_url="https://snow.xiaob.dev") as client:
            for route in (
                "/public/v1/config",
                "/public/v1/characters",
                "/public/v1/stickers",
            ):
                response = client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("set-cookie", ""), "")
            response = client.post(
                "/public/v1/presence/resolve",
                headers={"Origin": "https://snow.xiaob.dev"},
                json={
                    "request_id": str(uuid4()),
                    "character_id": MVP_CHARACTERS[0].character_id,
                    "state_package": "",
                },
            )
            self.assertEqual(response.status_code, 200)
            cookie = response.headers.get("set-cookie", "")
            self.assertIn("__Host-snow_anon=", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertIn("Path=/", cookie)
            self.assertNotIn("Domain=", cookie)

    def test_sticker_route_exposes_filtered_paged_display_contract(self) -> None:
        expected = {
            "version": "2026.08.19.sticker.1",
            "stickers": [
                {
                    "asset_id": "sticker-1",
                    "display_src": "/media/2026.08.19.sticker.1/display/sticker-1.webp",
                    "display_animated": True,
                    "character_ids": [MVP_CHARACTERS[0].character_id],
                    "emotion_tags": ["happy"],
                }
            ],
            "next_cursor": 40,
            "total": 41,
            "facets": {
                "sections": {"角色": 41},
                "emotion_tags": {"happy": 41},
                "candidate_scopes": {"character": 41},
            },
            "status": "ok",
        }
        with patch.object(
            self.app.state.chat_service.stickers,
            "list",
            return_value=expected,
        ) as list_stickers:
            response = self.client.get(
                "/public/v1/stickers",
                params={
                    "section": "角色",
                    "character_id": MVP_CHARACTERS[0].character_id,
                    "emotion_tag": "happy",
                    "candidate_scope": "character",
                    "q": "微笑",
                    "cursor": 0,
                    "limit": 40,
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        list_stickers.assert_called_once_with(
            section="角色",
            character_id=MVP_CHARACTERS[0].character_id,
            emotion_tag="happy",
            candidate_scope="character",
            q="微笑",
            cursor=0,
            limit=40,
        )
        invalid = self.client.get(
            "/public/v1/stickers", params={"candidate_scope": "another-character"}
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            invalid.json()["detail"]["code"], "invalid_sticker_candidate_scope"
        )

    def test_characters_returns_full_registry(self) -> None:
        response = self.client.get("/public/v1/characters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 22)
        character = next(
            item for item in response.json()["characters"] if item["character_id"] == "25b23cb64398"
        )
        self.assertEqual(character["display_name"], "凯茜娅")
        self.assertEqual(character["aliases"], ["凯茜娅", "凯西娅"])
        self.assertEqual(character["search_tokens"], ["kxy", "kaixiya"])

    def test_production_readiness_requires_matching_data_release(self) -> None:
        production_settings = replace(
            self.settings,
            allow_insecure_dev=False,
            turnstile_secret="turnstile-test",
            neo4j_password="neo4j-test",
        )
        with TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            internal_settings = replace(
                self.internal_settings,
                data_root=runtime_root,
                runtime_root=runtime_root,
            )
            with (
                patch(
                    "backend.snow_app.public_media.PublicMediaCatalog.verify",
                    return_value={"status": "ok"},
                ),
                patch(
                    "backend.snow_app.public_stickers.PublicStickerCatalog.verify",
                    return_value={"status": "ok", "sticker_count": 363},
                ),
            ):
                app = create_app(production_settings, internal_settings, self.store)
            client = TestClient(app, base_url="http://127.0.0.1")
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

    def test_feedback_redacts_accidentally_pasted_provider_keys(self) -> None:
        leaked = "sk-test-never-log-this"
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "body": f"页面显示了 {leaked}",
                "user_message": f"api_key={leaked}",
                "assistant_answer": leaked,
                "turnstile_token": "development-bypass",
            },
        )
        self.assertEqual(response.status_code, 200)
        stored = json.dumps(self.store.feedback_rows()[0], ensure_ascii=False)
        self.assertNotIn(leaked, stored)
        self.assertIn("[已隐藏]", stored)

    def test_feedback_redacts_keys_in_forged_model_metadata(self) -> None:
        leaked = "sk-test-model-field-never-log-this"
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "body": "模型字段脱敏测试",
                "provider": "openai",
                "model": leaked,
                "turnstile_token": "development-bypass",
            },
        )
        self.assertEqual(response.status_code, 200)
        stored = json.dumps(self.store.feedback_rows()[0], ensure_ascii=False)
        self.assertNotIn(leaked, stored)
        self.assertIn("[已隐藏]", stored)

    def test_feedback_accepts_sticker_only_user_context(self) -> None:
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "body": "这条表情的展示有问题",
                "user_content_blocks": [
                    {
                        "type": "sticker",
                        "asset_id": "fixture-sticker",
                        "caption": "收到",
                    }
                ],
                "turnstile_token": "development-bypass",
            },
        )
        self.assertEqual(response.status_code, 200)
        context = self.store.feedback_rows()[0]["context"]
        self.assertEqual(context["user_message"], "")
        self.assertEqual(
            context["user_content_blocks"],
            [{"type": "sticker", "asset_id": "fixture-sticker", "caption": "收到"}],
        )

    def test_feedback_opt_out_discards_forged_conversation_context(self) -> None:
        forged_chat_request_id = str(uuid4())
        response = self.client.post(
            "/public/v1/feedback",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "chat_request_id": forged_chat_request_id,
                "body": "只提交这段反馈正文",
                "character_id": MVP_CHARACTERS[0].character_id,
                "provider": "openai",
                "model": "gpt-forged",
                "user_message": "不应保留的用户消息",
                "assistant_answer": "不应保留的角色回复",
                "user_content_blocks": [
                    {"type": "message", "text": "不应保留的用户块"}
                ],
                "assistant_content_blocks": [
                    {"type": "message", "text": "不应保留的回复块"}
                ],
                "request_stage": "text",
                "error_code": "forged_error",
                "degraded_services": ["forged_dependency"],
                "include_conversation_context": False,
                "turnstile_token": "development-bypass",
            },
        )
        self.assertEqual(response.status_code, 200)
        context = self.store.feedback_rows()[0]["context"]
        self.assertNotIn("chat_request_id", context)
        self.assertNotIn("generation_diagnostics", context)
        self.assertEqual(context["character_id"], "")
        self.assertEqual(context["provider"], "")
        self.assertEqual(context["model"], "")
        self.assertEqual(context["user_message"], "")
        self.assertEqual(context["assistant_answer"], "")
        self.assertEqual(context["user_content_blocks"], [])
        self.assertEqual(context["assistant_content_blocks"], [])
        self.assertEqual(context["request_stage"], "")
        self.assertEqual(context["error_code"], "")
        self.assertEqual(context["degraded_services"], [])

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

    def test_sse_emits_meta_and_heartbeat_while_model_is_slow(self) -> None:
        credential, _ = self._byok()
        payload = {
            "request_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": MVP_CHARACTERS[0].character_id,
            "message": "你好",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }

        async def slow_chat(*_args, **_kwargs):
            await asyncio.sleep(4.2)
            return {
                "answer": "慢速回复",
                "content_blocks": [{"type": "speech", "text": "慢速回复"}],
                "retrieval": {},
                "usage": {},
                "response_adjustments": [],
            }

        with patch.object(self.app.state.chat_service, "chat", side_effect=slow_chat):
            started = time.monotonic()
            with self.client.stream(
                "POST",
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=payload,
            ) as response:
                self.assertEqual(response.status_code, 200)
                chunks = response.iter_text()
                first_chunk = next(chunks)
                first_elapsed = time.monotonic() - started
                remainder = "".join(chunks)

        # Starlette's in-process TestClient buffers the body until the ASGI
        # call completes, so it cannot measure network-level first-byte time.
        # The route itself creates the StreamingResponse before awaiting the
        # worker; this test locks in the observable heartbeat contract.
        self.assertGreaterEqual(first_elapsed, 0)
        stream = first_chunk + remainder
        self.assertIn("event: meta", stream)
        self.assertIn(": heartbeat", stream)
        self.assertIn("event: done", stream)

    def test_chat_queue_rejection_is_http_429_with_retry_after(self) -> None:
        credential, _ = self._byok()
        payload = {
            "request_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": MVP_CHARACTERS[0].character_id,
            "message": "你好",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        with patch.object(
            self.app.state.chat_service.gate,
            "acquire",
            side_effect=GenerationBusy("generation_queue_full"),
        ):
            response = self.client.post(
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "2")
        self.assertEqual(response.json()["detail"]["code"], "generation_queue_full")
        self.assertEqual(response.json()["detail"]["retry_after_seconds"], 2)

    def test_public_returns_revalidated_empty_output_fallback_and_replays_it(self) -> None:
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
        self.assertNotIn('event: error', first.text)
        self.assertIn('event: delta', first.text)
        self.assertIn('event: done', first.text)
        self.assertIn("我还在接着你刚才的话题", first.text)
        self.assertIn('"idempotent_replay":true', second.text)
        self.assertEqual(chat.call_count, 1)

    def test_public_renders_safe_current_activity_fallback(self) -> None:
        credential, _ = self._byok()
        payload = {
            "request_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": "1b0a6b35719a",
            "message": "你在干什么？",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        result = {
            "answer": "我在整理手边的资料，现在可以陪你聊会儿。",
            "content_blocks": [
                {"type": "message", "text": "我在整理手边的资料，现在可以陪你聊会儿。"}
            ],
            "question_focus": "current_activity",
            "retrieval": {},
            "usage": {"total_tokens": 10},
            "response_adjustments": ["live_scene_guard"],
        }
        with patch.object(self.app.state.chat_service.mvp, "chat", return_value=result) as chat:
            response = self.client.post(
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('"code":"role_guard_rejected"', response.text)
        self.assertIn("整理手边的资料", response.text)
        self.assertIn("event: done", response.text)
        self.assertEqual(chat.call_count, 1)

    def test_public_renders_revalidated_companion_fallback(self) -> None:
        credential, _ = self._byok()
        payload = {
            "request_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": "a2ffc5b44d7f",
            "message": "你和猫汐尔相处得怎么样？",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        result = {
            "answer": "我和猫汐尔偶尔会拌嘴，但我们是可以互相信任的同伴。",
            "content_blocks": [{
                "type": "message",
                "text": "我和猫汐尔偶尔会拌嘴，但我们是可以互相信任的同伴。",
            }],
            "retrieval": {},
            "usage": {"total_tokens": 10},
            "validation_disposition": "safe_fallback",
            "guard_resolution": "safe_fallback",
            "response_adjustments": ["answer_guardrail_retry", "companion_social_guard"],
        }
        with patch.object(self.app.state.chat_service.mvp, "chat", return_value=result):
            response = self.client.post(
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('"code":"role_guard_rejected"', response.text)
        self.assertIn("猫汐尔", response.text)
        self.assertIn("event: done", response.text)

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
        self.assertIn("model_calls", context["generation_diagnostics"]["timings_ms"])
        self.assertNotIn("sk-test-never-log-this", json.dumps(context, ensure_ascii=False))

    def test_chat_idempotency_record_redacts_forged_model_metadata(self) -> None:
        credential, _ = self._byok()
        leaked = "sk-test-model-cache-never-log-this"
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "provider": "openai",
            "credential": credential,
            "model": leaked,
            "character_id": MVP_CHARACTERS[0].character_id,
            "message": "你好",
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        with patch.object(
            self.app.state.chat_service.mvp,
            "chat",
            return_value={"answer": "你好", "retrieval": {}, "usage": {}, "response_adjustments": []},
        ):
            response = self.client.post(
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        # Inspect the raw cache row without weakening the public ownership
        # contract of request_result (which expects a subject hash).
        with self.store.begin() as connection:
            raw = connection.execute(
                text("SELECT response_json FROM public_request_cache WHERE request_id = :request_id"),
                {"request_id": request_id},
            ).scalar_one()
        self.assertNotIn(leaked, raw)
        self.assertIn("[已隐藏]", raw)

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

    def test_public_state_v1_is_upgraded_without_losing_known_presence(self) -> None:
        character = MVP_CHARACTERS[0]
        legacy = sign_state(
            self.settings,
            {
                "schema_version": "public-state-1",
                "data_version": "legacy-data",
                "revision": 7,
                "relationships": {character.character_id: {"address": "分析员"}},
                "world": {
                    "presence": {
                        character.character_id: {
                            "location": "观景区",
                            "activity": "正在看雪",
                            "state_scope": "conversation_confirmed",
                        }
                    }
                },
            },
        )
        response = self.client.post(
            "/public/v1/presence/resolve",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "character_id": character.character_id,
                "state_package": legacy,
            },
        )
        self.assertEqual(response.status_code, 200)
        upgraded = verify_state(self.settings, response.json()["state_package"])
        self.assertEqual(upgraded["schema_version"], "public-state-2")
        self.assertEqual(upgraded["revision"], 7)
        self.assertEqual(len(upgraded["presence"]), 22)
        self.assertEqual(upgraded["presence"][character.character_id]["location"], "观景区")
        self.assertEqual(upgraded["relationships"][character.character_id]["address"], "分析员")

    def test_shared_daily_state_has_hong_kong_schedule_window(self) -> None:
        character = MVP_CHARACTERS[0]
        first = self.client.post(
            "/public/v1/presence/resolve",
            headers={"Origin": "http://testserver"},
            json={"request_id": str(uuid4()), "character_id": character.character_id, "state_package": ""},
        )
        self.assertEqual(first.status_code, 200)
        state = verify_state(self.settings, first.json()["state_package"])
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["schedule_revision"], 1)
        self.assertTrue(state["schedule_date"])
        self.assertTrue(state["generated_at"] < state["expires_at"])
        self.assertEqual(state["presence"][character.character_id]["state_scope"], "shared_daily")

    def test_text_channel_rejects_action_blocks(self) -> None:
        response = self.client.post(
            "/public/v1/chat/stream",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "provider": "openai",
                "credential": "x" * 40,
                "model": "gpt-test",
                "character_id": MVP_CHARACTERS[0].character_id,
                "communication_channel": "text",
                "content_blocks": [{"type": "action", "text": "向她挥手"}],
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "invalid_request")

    def test_presence_rejects_tampered_state_package(self) -> None:
        signed = sign_state(
            self.settings,
            {"schema_version": "public-state-1", "data_version": "legacy", "revision": 0},
        )
        response = self.client.post(
            "/public/v1/presence/resolve",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "character_id": MVP_CHARACTERS[0].character_id,
                "state_package": signed[:-1] + ("A" if signed[-1] != "A" else "B"),
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "state_invalid")

    def test_subject_bound_state_cannot_cross_anonymous_cookie(self) -> None:
        first_client = TestClient(self.app)
        second_client = TestClient(self.app)
        try:
            resolved = first_client.post(
                "/public/v1/presence/resolve",
                headers={"Origin": "http://testserver"},
                json={
                    "request_id": str(uuid4()),
                    "character_id": MVP_CHARACTERS[0].character_id,
                    "state_package": "",
                },
            )
            self.assertEqual(resolved.status_code, 200)
            rejected = second_client.post(
                "/public/v1/presence/resolve",
                headers={"Origin": "http://testserver"},
                json={
                    "request_id": str(uuid4()),
                    "character_id": MVP_CHARACTERS[0].character_id,
                    "state_package": resolved.json()["state_package"],
                },
            )
            self.assertEqual(rejected.status_code, 409)
            self.assertEqual(
                rejected.json()["detail"]["code"], "state_subject_mismatch"
            )
        finally:
            first_client.close()
            second_client.close()

    def test_in_person_action_only_turn_reaches_mvp_with_structured_blocks(self) -> None:
        credential, _ = self._byok()
        payload = {
            "request_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": MVP_CHARACTERS[0].character_id,
            "communication_channel": "in_person",
            "content_blocks": [{"type": "action", "text": "向她挥了挥手"}],
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        generated = {
            "answer": "她轻轻点头。\n晚上好。",
            "content_blocks": [
                {"type": "action", "text": "她轻轻点头。"},
                {"type": "speech", "text": "晚上好。"},
            ],
            "response_adjustments": [],
        }
        with patch.object(self.app.state.chat_service.mvp, "chat", return_value=generated) as chat:
            response = self.client.post(
                "/public/v1/chat/stream",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn('"communication_channel":"in_person"', response.text)
        self.assertIn('"type":"action"', response.text)
        self.assertIn('"block_index":0', response.text)
        self.assertIn('"block_type":"action"', response.text)
        self.assertEqual(chat.call_args.kwargs["communication_channel"], "in_person")
        self.assertEqual(
            chat.call_args.kwargs["analyst_content_blocks"],
            [{"type": "action", "text": "向她挥了挥手"}],
        )

    def test_presence_transition_moves_analyst_and_replays(self) -> None:
        character = MVP_CHARACTERS[0]
        resolved = self.client.post(
            "/public/v1/presence/resolve",
            headers={"Origin": "http://testserver"},
            json={
                "request_id": str(uuid4()),
                "character_id": character.character_id,
                "state_package": "",
            },
        ).json()
        request_id = str(uuid4())
        payload = {
            "request_id": request_id,
            "character_id": character.character_id,
            "target_channel": "in_person",
            "action": "join_character",
            "state_package": resolved["state_package"],
        }
        first = self.client.post(
            "/public/v1/presence/transition",
            headers={"Origin": "http://testserver"},
            json=payload,
        )
        second = self.client.post(
            "/public/v1/presence/transition",
            headers={"Origin": "http://testserver"},
            json=payload,
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["scene_state"]["co_located"])
        transitioned = verify_state(self.settings, first.json()["state_package"])
        # The shared daily schedule starts at revision 1; a transition adds
        # one user-state revision on top of that immutable daily snapshot.
        self.assertEqual(transitioned["revision"], 2)
        self.assertEqual(transitioned["recent_events"][-1]["event_type"], "presence_transition")
        self.assertTrue(second.json()["idempotent_replay"])

    def test_arrival_unnoticed_does_not_call_model_and_is_idempotent(self) -> None:
        credential, _ = self._byok()
        character = MVP_CHARACTERS[0]
        arrival_id = str(uuid4())
        payload = {
            "arrival_id": arrival_id,
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": character.character_id,
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        with patch("backend.snow_app.public_service.secrets.randbelow", return_value=1), patch.object(
            self.app.state.chat_service.mvp,
            "chat",
        ) as chat:
            first = self.client.post(
                "/public/v1/presence/arrival",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
            second = self.client.post(
                "/public/v1/presence/arrival",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["decision"], "unnoticed")
        self.assertFalse(first.json()["model_called"])
        self.assertIsNone(first.json()["reaction"])
        self.assertTrue(first.json()["scene_state"]["co_located"])
        self.assertTrue(second.json()["idempotent_replay"])
        chat.assert_not_called()

    def test_arrival_queue_rejection_is_http_429_with_retry_after(self) -> None:
        credential, _ = self._byok()
        payload = {
            "arrival_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": MVP_CHARACTERS[0].character_id,
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        with patch(
            "backend.snow_app.public_service.secrets.randbelow", return_value=0
        ), patch.object(
            self.app.state.chat_service.gate,
            "acquire",
            side_effect=GenerationBusy("generation_queue_full"),
        ):
            response = self.client.post(
                "/public/v1/presence/arrival",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "2")
        self.assertEqual(response.json()["detail"]["code"], "generation_queue_full")

    def test_arrival_guard_failure_keeps_transition_without_fake_dialogue(self) -> None:
        credential, _ = self._byok()
        payload = {
            "arrival_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": MVP_CHARACTERS[0].character_id,
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        generated = {
            "answer": "机械兜底",
            "content_blocks": [{"type": "speech", "text": "机械兜底"}],
            "response_adjustments": ["empty_model_output_guard"],
        }
        with patch("backend.snow_app.public_service.secrets.randbelow", return_value=0), patch.object(
            self.app.state.chat_service.mvp,
            "chat",
            return_value=generated,
        ):
            response = self.client.post(
                "/public/v1/presence/arrival",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "noticed")
        self.assertEqual(response.json()["terminal_error"], "upstream_invalid_response")
        self.assertIsNone(response.json()["reaction"])
        self.assertTrue(response.json()["scene_state"]["co_located"])

    def test_arrival_noticed_returns_guarded_structured_reaction(self) -> None:
        credential, _ = self._byok()
        payload = {
            "arrival_id": str(uuid4()),
            "provider": "openai",
            "credential": credential,
            "model": "gpt-test",
            "character_id": MVP_CHARACTERS[0].character_id,
            "recent_history": [],
            "history_summary": "",
            "state_package": "",
        }
        generated = {
            "answer": "她抬眼看向你。\n你来了。",
            "content_blocks": [
                {"type": "action", "text": "她抬眼看向你。"},
                {"type": "speech", "text": "你来了。"},
            ],
            "response_adjustments": [],
            "usage": {"total_tokens": 12},
        }
        with patch("backend.snow_app.public_service.secrets.randbelow", return_value=0), patch.object(
            self.app.state.chat_service.mvp,
            "chat",
            return_value=generated,
        ) as chat:
            response = self.client.post(
                "/public/v1/presence/arrival",
                headers={"Origin": "http://testserver"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["decision"], "noticed")
        self.assertTrue(response.json()["model_called"])
        self.assertEqual(response.json()["reaction"]["content_blocks"][0]["type"], "action")
        self.assertEqual(response.json()["reaction"]["content_blocks"][1]["type"], "speech")
        self.assertEqual(chat.call_count, 1)
        decision = chat.call_args.kwargs["thinking_decision"]
        self.assertEqual(decision["max_provider_http_calls"], 2)
        self.assertTrue(decision["disable_compatibility_retries"])
