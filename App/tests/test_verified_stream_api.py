"""Incremental delivery is observable before completion and always durable.

All content, credentials and stores here are synthetic. No model is contacted.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import HTTPException, Request

from backend.snow_app.config import Settings
from backend.snow_app.dialogue_core import GenerationGate
from backend.snow_app.generation_stream import current_generation_observer
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_contracts import ChatRequest
from backend.snow_app.public_main import create_app, _model_request_body, _request_hash
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable
from tests.test_public_request_recovery import settings


BLOCK = {"type": "message", "text": "辛苦了，今天想聊些什么？"}


def packet_data(packet):
    return json.loads(packet.split("data: ", 1)[1])


class VerifiedStreamApiTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = PublicStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.service = Mock()
        self.service.gate = GenerationGate()
        self.service.media.verify.return_value = {"status": "unavailable"}
        self.service.stickers.verify.return_value = {"status": "unavailable"}
        self.service.validate_content_blocks.return_value = []
        self.service.chat = AsyncMock()
        self.release = asyncio.Event()
        self.app = create_app(
            replace(settings(), verified_block_stream=True),
            Settings(Path("unused-data"), Path("unused-runtime"), False, "", []),
            self.store, self.service,
        )
        self.endpoint = next(route.endpoint for route in self.app.routes if route.path == "/public/v1/chat/stream")
        self.payload = ChatRequest(
            request_id=uuid4(), provider="openai", credential="synthetic-credential-value",
            model="synthetic", character_id=MVP_CHARACTERS[0].character_id, message="聊会儿吧",
        )
        self.auth = patch("backend.snow_app.public_main.open_byok_credential", return_value={"api_key": "synthetic"})
        self.auth.start()

    def request(self, version="2", subject="subject"):
        return Request({
            "type": "http", "headers": [(b"x-snow-stream-version", version.encode())],
            "state": {"anonymous_id": "synthetic", "subject_hash": subject},
        })

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*list(self.app.state.chat_jobs.values()), return_exceptions=True)
        self.auth.stop()
        await self.app.state.async_store.close()
        await self.app.state.provider_http.close()
        self.store.engine.dispose()

    async def generate(self, *_args, **_kwargs):
        observer = current_generation_observer()
        observer.phase("generating")
        if observer.on_verified_blocks:
            await asyncio.to_thread(observer.publish, [BLOCK])
        await self.release.wait()
        return {"answer": BLOCK["text"], "content_blocks": [BLOCK], "communication_channel": "text"}

    async def until_block(self, response):
        packets = []
        async with asyncio.timeout(3):
            async for packet in response.body_iterator:
                packets.append(packet)
                if packet.startswith("event: committed_block\n"):
                    return packets
        self.fail("No committed block before completion")

    async def test_block_is_durable_before_final_and_reconnect_never_regenerates(self):
        self.service.chat.side_effect = self.generate
        response = await self.endpoint(self.request(), self.payload)
        packets = await self.until_block(response)
        self.assertFalse(self.release.is_set())
        self.assertNotIn("event: done", "".join(packets))
        saved = self.store.request_blocks(str(self.payload.request_id), "subject")
        self.assertEqual(saved[0]["block"], BLOCK)
        replay = await self.endpoint(self.request(), self.payload)
        replay_packets = await self.until_block(replay)
        self.assertEqual(packet_data(packets[-1])["block_id"], packet_data(replay_packets[-1])["block_id"])
        self.release.set()
        tail = "".join([part async for part in response.body_iterator])
        replay_tail = "".join([part async for part in replay.body_iterator])
        self.assertIn("event: done", tail)
        self.assertIn("event: done", replay_tail)
        self.assertNotIn("event: delta", tail)
        self.assertEqual(self.service.chat.await_count, 1)

    async def test_later_failure_preserves_prefix_and_terminal_replay(self):
        async def failed(*args, **kwargs):
            await self.generate(*args, **kwargs)
            raise ValueError("synthetic late failure")
        self.service.chat.side_effect = failed
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        self.release.set()
        tail = "".join([part async for part in response.body_iterator])
        self.assertIn('"partial":true', tail)
        self.assertIn('"retryable":false', tail)
        replay = await self.endpoint(self.request(), self.payload)
        replay_text = "".join([part async for part in replay.body_iterator])
        self.assertIn(BLOCK["text"], replay_text)
        self.assertIn('"partial":true', replay_text)
        self.assertNotIn("event: done", replay_text)
        self.assertEqual(self.service.chat.await_count, 1)

    async def test_failed_block_write_does_not_publish(self):
        self.service.chat.side_effect = self.generate
        with patch.object(self.store, "commit_request_blocks", side_effect=PublicStoreUnavailable("synthetic outage")):
            response = await self.endpoint(self.request(), self.payload)
            text = "".join([part async for part in response.body_iterator])
        self.assertIn("event: error", text)
        self.assertNotIn("event: committed_block", text)
        self.assertNotIn(BLOCK["text"], text)

    async def test_lost_write_ack_replays_durable_prefix_without_retry(self):
        original = self.store.commit_request_blocks
        def lose_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise PublicStoreUnavailable("synthetic lost ACK")
        self.service.chat.side_effect = self.generate
        with patch.object(self.store, "commit_request_blocks", side_effect=lose_ack):
            response = await self.endpoint(self.request(), self.payload)
            text = "".join([part async for part in response.body_iterator])
        self.assertIn("event: committed_block", text)
        self.assertIn('"partial":true', text)
        self.assertEqual(self.service.chat.await_count, 1)

    async def test_final_prefix_change_cannot_be_sent_as_success(self):
        async def changed(*args, **kwargs):
            await self.generate(*args, **kwargs)
            return {"answer": "changed", "content_blocks": [{"type": "message", "text": "changed"}]}
        self.service.chat.side_effect = changed
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        self.release.set()
        tail = "".join([part async for part in response.body_iterator])
        self.assertIn('"partial":true', tail)
        self.assertNotIn("event: done", tail)
        self.assertNotIn('"text":"changed"', tail)

    async def test_old_client_keeps_whole_reply_contract(self):
        self.service.chat.side_effect = self.generate
        response = await self.endpoint(self.request("1"), self.payload)
        self.release.set()
        text = "".join([part async for part in response.body_iterator])
        self.assertIn("event: delta", text)
        self.assertIn("event: done", text)
        self.assertNotIn("event: committed_block", text)
        self.assertNotIn("event: progress", text)

    async def test_queue_progress_opens_before_generation_slot(self):
        self.service.gate = GenerationGate(active=1)
        await self.service.gate.acquire()
        self.service.chat.side_effect = self.generate
        try:
            response = await asyncio.wait_for(self.endpoint(self.request(), self.payload), timeout=1)
            first = await anext(response.body_iterator)
            second = await anext(response.body_iterator)
            self.assertIn("event: meta", first)
            self.assertIn('"phase":"queued"', second)
            self.assertEqual(self.service.chat.await_count, 0)
        finally:
            self.service.gate.release()
        self.release.set()
        text = "".join([part async for part in response.body_iterator])
        self.assertIn("event: done", text)
        self.assertEqual(self.service.gate.snapshot()["active"], 0)

    async def test_different_snapshot_and_subject_are_rejected(self):
        self.service.chat.side_effect = self.generate
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        for request, payload in (
            (self.request(), self.payload.model_copy(update={"message": "changed"})),
            (self.request(subject="other"), self.payload),
        ):
            with self.assertRaises(HTTPException) as raised:
                await self.endpoint(request, payload)
            self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.store.request_blocks(str(self.payload.request_id), "other"), [])
        self.release.set()
        _ = [part async for part in response.body_iterator]

    async def test_v2_admission_rejection_can_retry_same_uuid_without_cached_failure(self):
        self.service.gate = GenerationGate(active=1, queued=0)
        await self.service.gate.acquire()
        self.service.chat.side_effect = self.generate
        try:
            response = await self.endpoint(self.request(), self.payload)
            rejected = "".join([part async for part in response.body_iterator])
            self.assertIn('"code":"generation_queue_full"', rejected)
            self.assertIn('"retryable":true', rejected)
            self.assertIsNone(self.store.request_result(str(self.payload.request_id), "subject"))
            self.assertEqual(self.service.chat.await_count, 0)
        finally:
            self.service.gate.release()
        self.release.set()
        retry = await self.endpoint(self.request(), self.payload)
        completed = "".join([part async for part in retry.body_iterator])
        self.assertIn("event: done", completed)
        self.assertEqual(self.service.chat.await_count, 1)

    async def test_disconnect_keeps_original_job_and_saved_prefix(self):
        self.service.chat.side_effect = self.generate
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        original_job = self.app.state.chat_jobs[str(self.payload.request_id)]
        await response.body_iterator.aclose()
        self.assertFalse(original_job.done())
        self.release.set()
        await original_job
        replay = await self.endpoint(self.request(), self.payload)
        text = "".join([part async for part in replay.body_iterator])
        self.assertEqual(text.count("event: committed_block"), 1)
        self.assertIn("event: done", text)
        self.assertEqual(self.service.chat.await_count, 1)

    async def test_other_owner_is_polled_without_starting_a_model(self):
        request_id = str(self.payload.request_id)
        request_hash = _request_hash(_model_request_body(self.payload))
        self.store.claim_request(request_id, "subject", request_hash, owner_token="remote")
        self.store.commit_request_blocks(request_id, "subject", [BLOCK], owner_token="remote")
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        self.store.complete_request(request_id, {"answer": BLOCK["text"], "content_blocks": [BLOCK]}, owner_token="remote")
        tail = "".join([part async for part in response.body_iterator])
        self.assertIn("event: done", tail)
        self.assertEqual(self.service.chat.await_count, 0)
        self.assertEqual(self.app.state.active_subject_requests, {})

    async def test_partial_remains_visible_when_final_store_is_unavailable(self):
        self.service.chat.side_effect = self.generate
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        with patch.object(self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic final outage")):
            self.release.set()
            tail = "".join([part async for part in response.body_iterator])
        self.assertIn('"code":"public_database_unavailable"', tail)
        self.assertIn('"partial":true', tail)
        self.assertNotIn("event: done", tail)
        self.assertEqual(self.store.request_blocks(str(self.payload.request_id), "subject")[0]["block"], BLOCK)

    async def test_sensitive_text_never_bypasses_final_public_projection(self):
        synthetic_secret = "sk-synthetic-not-a-real-credential"
        unsafe_block = {"type": "message", "text": f"内容为 {synthetic_secret}。"}
        async def echoed(*_args, **_kwargs):
            observer = current_generation_observer()
            self.assertFalse(await asyncio.to_thread(observer.publish, [unsafe_block]))
            return {"answer": unsafe_block["text"], "content_blocks": [unsafe_block]}
        self.service.chat.side_effect = echoed
        response = await self.endpoint(self.request(), self.payload)
        text = "".join([part async for part in response.body_iterator])
        self.assertNotIn(synthetic_secret, text)
        self.assertNotIn("event: committed_block", text)
        self.assertIn("已隐藏", text)
        self.assertEqual(self.store.request_blocks(str(self.payload.request_id), "subject"), [])

    async def test_feature_rollback_stops_new_publication_but_keeps_partial_replay(self):
        async def failed(*args, **kwargs):
            await self.generate(*args, **kwargs)
            raise ValueError("synthetic partial")
        self.service.chat.side_effect = failed
        response = await self.endpoint(self.request(), self.payload)
        await self.until_block(response)
        self.release.set()
        _ = [part async for part in response.body_iterator]
        await self.app.state.async_store.close()
        await self.app.state.provider_http.close()
        self.app = create_app(
            replace(settings(), verified_block_stream=False),
            Settings(Path("unused-data"), Path("unused-runtime"), False, "", []), self.store, self.service,
        )
        self.endpoint = next(route.endpoint for route in self.app.routes if route.path == "/public/v1/chat/stream")
        replay = await self.endpoint(self.request(), self.payload)
        text = "".join([part async for part in replay.body_iterator])
        self.assertIn("event: committed_block", text)
        self.assertIn('"partial":true', text)
        self.assertEqual(self.service.chat.await_count, 1)
        self.service.chat.side_effect = self.generate
        new_payload = self.payload.model_copy(update={"request_id": uuid4()})
        fresh = await self.endpoint(self.request(), new_payload)
        fresh_text = "".join([part async for part in fresh.body_iterator])
        self.assertIn("event: done", fresh_text)
        self.assertNotIn("event: committed_block", fresh_text)
        self.assertEqual(self.store.request_blocks(str(new_payload.request_id), "subject"), [])
