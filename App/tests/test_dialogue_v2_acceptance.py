"""HTTP persistence/recovery boundaries with the actual scene adapter.

Fixtures are synthetic; the adapter's generation boundary is local and counted.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from backend.snow_app.config import Settings
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_main import _anonymous_cookie_name, create_app
from backend.snow_app.public_security import subject_hash, verify_state
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable
from backend.snow_app.session_context import verify_session_context, verify_session_memory
from tests import test_dialogue_v2_service as scene_fixtures
from tests.test_postgres_integration import database, migrate, old_store_module


class DialogueV2AcceptanceTests(TestCase):
    def setUp(self):
        self.fixture = scene_fixtures.DialogueV2ServiceTests(methodName="runTest")
        self.fixture.setUp()
        self.settings = self.fixture.settings
        self.store = PublicStore(self.settings.database_url)
        self.store.create_schema()
        self.app = create_app(self.settings, Settings.from_environment(), self.store,
                              dialogue_v2_service=self.fixture.service)
        self.directory = TemporaryDirectory()
        views = Path(self.directory.name) / "character_views.jsonl"
        views.write_text("".join(json.dumps({"character_id": item.character_id}) + "\n"
                                 for item in MVP_CHARACTERS), encoding="utf-8")
        self.app.state.chat_service.mvp.views_path = views
        self.app.state.chat_service.mvp._views_cache = None
        self.client = TestClient(self.app)
        self.headers = {"Origin": "http://testserver", "X-Snow-Stream-Version": "2"}
        issued = self.client.post("/public/v1/byok/session", headers=self.headers, json={
            "provider": "openai", "api_key": "sk-synthetic-no-network", "turnstile_token": "development-bypass",
            "accepted_transit_notice": True, "accepted_cost_notice": True, "accepted_local_history_notice": True,
        })
        self.assertEqual(issued.status_code, 200)
        self.credential = issued.json()["credential"]
        self.fixture.subject = subject_hash(self.client.cookies.get(_anonymous_cookie_name(self.settings)))
        self.fixture.daily["subject_binding"] = self.fixture.subject

    def tearDown(self):
        self.client.close()
        self.app.state.chat_service.close()
        asyncio.run(self.app.state.async_store.close())
        self.directory.cleanup()

    def body(self, message="一起去公园吧。", **kwargs):
        envelope = self.fixture.request(message, credential=self.credential, **kwargs)
        return envelope.model_dump(mode="json")

    def chat(self, body, *, recovery=False):
        return self.client.post("/public/v2/chat/stream", headers={**self.headers,
            **({"X-Snow-Recovery-Only": "1"} if recovery else {})}, json=body)

    @staticmethod
    def done(response):
        result = {}
        complete = False
        for block in response.text.split("\n\n"):
            if block.startswith(("event: meta\n", "event: state\n", "event: done\n")):
                result.update(json.loads(block.split("data: ", 1)[1]))
                complete = complete or block.startswith("event: done\n")
        if not complete:
            raise AssertionError("missing final event")
        return result

    def scene_body(self, action, previous=None, **changes):
        values = {"protocol": "public-scene-action-v2", "request_id": str(uuid4()),
            "character_id": self.fixture.character.character_id, "action": action,
            "segment_id": str(self.fixture.segment)}
        if previous:
            values.update(state_package=previous["state_package"],
                          session_context_package=previous["session_context_package"])
        return {**values, **changes}

    def scene(self, body, *, recovery=False):
        return self.client.post("/public/v2/scene/action", headers={**self.headers,
            **({"X-Snow-Recovery-Only": "1"} if recovery else {})}, json=body)

    def test_committed_invitation_context_is_replayed_without_generation(self):
        body = self.body()
        first = self.chat(body)
        self.assertEqual(first.status_code, 200)
        final = self.done(first)
        self.assertEqual(final["scene_action"]["status"], "character_waiting")
        context = verify_session_context(self.settings, final["session_context_package"],
            subject_hash=self.fixture.subject, character_id=self.fixture.character.character_id,
            segment_id=self.fixture.segment, catalog_version=self.fixture.service.catalog_version)
        self.assertEqual(context.active_invitation.status, "accepted")
        for receipt in final["session_memories"]:
            self.assertIsNotNone(verify_session_memory(self.settings, receipt["proof"],
                subject_hash=self.fixture.subject, character_id=self.fixture.character.character_id))
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            replay = self.chat(body, recovery=True)
            claim.assert_not_called()
        recovered = self.done(replay)
        self.assertEqual(recovered["session_context_package"], final["session_context_package"])
        self.assertEqual(recovered["session_memories"], final["session_memories"])
        self.assertTrue(recovered["idempotent_replay"])
        self.assertEqual(len(self.fixture.calls), 1)

    def test_expired_original_envelope_returns_410_and_does_not_reclaim(self):
        body = self.body()
        self.assertEqual(self.chat(body).status_code, 200)
        with self.store.begin() as connection:
            connection.execute(text("UPDATE public_request_cache SET expires_at = :old WHERE request_id = :id"),
                {"old": datetime.now(UTC) - timedelta(seconds=1), "id": body["chat"]["request_id"]})
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            expired = self.chat(body, recovery=True)
            claim.assert_not_called()
        self.assertEqual(expired.status_code, 410)
        self.assertEqual(expired.json()["detail"]["code"], "request_result_unavailable")
        self.assertFalse(expired.json()["detail"]["retryable"])
        self.assertEqual(len(self.fixture.calls), 1)

    def test_world_context_and_memories_never_escape_failed_persistence_ack(self):
        with patch.object(self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic ACK failure")):
            response = self.chat(self.body())
        self.assertEqual(response.status_code, 200)
        self.assertIn("public_database_unavailable", response.text)
        for token in ("好呀", "session_context_package", "session_memories", "character_waiting"):
            self.assertNotIn(token, response.text)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_join_action_ack_and_replay_are_zero_model_and_exact(self):
        waiting = self.done(self.chat(self.body()))
        world = verify_state(self.settings, waiting["state_package"])
        pending = world["pending_rendezvous"][self.fixture.character.character_id]
        body = self.scene_body("join_rendezvous", waiting, rendezvous_id=pending["rendezvous_id"])
        joined = self.scene(body)
        self.assertEqual(joined.status_code, 200)
        self.assertEqual(joined.json()["communication_channel"], "in_person")
        self.assertEqual(joined.json()["usage"]["provider_calls"], 0)
        with patch.object(self.store, "claim_request", wraps=self.store.claim_request) as claim:
            replay = self.scene(body, recovery=True)
            claim.assert_not_called()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["session_context_package"], joined.json()["session_context_package"])
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(len(self.fixture.calls), 1)

    def test_scene_action_ack_failure_does_not_return_signed_join(self):
        waiting = self.done(self.chat(self.body()))
        world = verify_state(self.settings, waiting["state_package"])
        pending = world["pending_rendezvous"][self.fixture.character.character_id]
        body = self.scene_body("join_rendezvous", waiting, rendezvous_id=pending["rendezvous_id"])
        with patch.object(self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic ACK failure")):
            failed = self.scene(body)
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("session_context_package", failed.text)
        self.assertNotIn("session_memories", failed.text)
        self.assertEqual(len(self.fixture.calls), 1)

    def test_changed_snapshot_and_foreign_subject_cannot_replay_confirmed_result(self):
        body = self.body()
        self.assertEqual(self.chat(body).status_code, 200)
        changed = json.loads(json.dumps(body))
        changed["chat"]["history_summary"] = "synthetic changed snapshot"
        self.assertEqual(self.chat(changed, recovery=True).status_code, 409)
        other = TestClient(self.app)
        try:
            foreign = other.post("/public/v2/chat/stream",
                headers={**self.headers, "X-Snow-Recovery-Only": "1"}, json=body)
            self.assertEqual(foreign.status_code, 409)
            self.assertNotIn("session_context_package", foreign.text)
        finally:
            other.close()
        self.assertEqual(len(self.fixture.calls), 1)



@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required; no SQLite fallback")
def test_postgres_dialogue_sidecars_survive_actual_old_new_old_cache(database):
    """Sidecars add no server chat/history schema and survive rollback readers."""
    old = old_store_module()
    migrate(database)
    old_store = old.PublicStore("", engine=database)
    current = PublicStore("", engine=database)
    fixture = scene_fixtures.DialogueV2ServiceTests(methodName="runTest")
    fixture.setUp()
    old_store.claim_request("old-before", fixture.subject, "hash-old")
    old_store.complete_request("old-before", {"answer": "synthetic old reply"})
    assert current.lookup_request("old-before", fixture.subject, "hash-old")[1]["answer"] == "synthetic old reply"
    result = fixture.run_chat("一起去公园吧。")
    request_id = result["request_id"]
    assert current.claim_request(request_id, fixture.subject, "hash-new", owner_token="synthetic-owner")[0] == "claimed"
    current.complete_request(request_id, result, owner_token="synthetic-owner")
    replay = old_store.request_result(request_id, fixture.subject)
    assert replay["session_context_package"] == result["session_context_package"]
    assert replay["session_memories"] == result["session_memories"]
    assert current.lookup_request(request_id, fixture.subject, "hash-new") == ("completed", result)
    old_store.claim_request("old-after", fixture.subject, "hash-after")
    old_store.complete_request("old-after", {"answer": "synthetic rollback reply"})
    assert current.lookup_request("old-after", fixture.subject, "hash-after")[1]["answer"] == "synthetic rollback reply"
    with database.begin() as connection:
        connection.execute(text("UPDATE public_request_cache SET expires_at = :old WHERE request_id = :id"),
            {"old": datetime.now(UTC) - timedelta(seconds=1), "id": request_id})
    assert current.lookup_request(request_id, fixture.subject, "hash-new") == ("missing", None)
    with database.begin() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM public_request_cache WHERE request_id = :id"),
                                  {"id": request_id}).scalar_one() == 1


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required; no SQLite fallback")
def test_postgres_failed_dialogue_ack_keeps_scene_result_uncommitted(database):
    migrate(database)
    store = PublicStore("", engine=database)
    fixture = scene_fixtures.DialogueV2ServiceTests(methodName="runTest")
    fixture.setUp()
    result = fixture.run_chat("一起去公园吧。")
    request_id = result["request_id"]
    assert store.claim_request(request_id, fixture.subject, "hash", owner_token="synthetic-owner")[0] == "claimed"
    # This schema is a disposable, random fixture. A real PG constraint fault
    # forces the completion transaction to roll back, instead of mocking ACK.
    with database.begin() as connection:
        connection.execute(text("ALTER TABLE public_request_cache ADD CONSTRAINT synthetic_no_complete CHECK (status <> 'completed')"))
    with pytest.raises(PublicStoreUnavailable):
        store.complete_request(request_id, result, owner_token="synthetic-owner")
    with database.begin() as connection:
        saved = connection.execute(text("SELECT status, response_json FROM public_request_cache WHERE request_id = :id"),
                                   {"id": request_id}).mappings().one()
    assert saved["status"] == "processing"
    assert saved["response_json"] is None
    assert store.lookup_request(request_id, fixture.subject, "hash") == ("processing", None)

