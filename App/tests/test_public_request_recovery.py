"""Fault injection for paid-request durability; no runtime files or providers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import HTTPException, Request

from backend.snow_app.config import PublicSettings, Settings
from backend.snow_app.dialogue_core import GenerationGate
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.public_contracts import ChatRequest, PresenceArrivalRequest, SummarizeRequest
from backend.snow_app.public_main import create_app
from backend.snow_app.public_providers import ProviderRequestError
from backend.snow_app.public_security import PublicSecurityError
from backend.snow_app.public_service import GenerationBusy, PublicChatService
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable


def settings() -> PublicSettings:
    return PublicSettings(
        app_version="test", data_version="test", database_url="sqlite+pysqlite:///:memory:",
        public_origin="https://snow.example", development_origins=("http://testserver",),
        turnstile_site_key="", turnstile_secret="", credential_key=b"x" * 32,
        state_hmac_key=b"x" * 32, ip_hmac_key=b"x" * 32, qq_key=b"x" * 32,
        admin_token="", enabled_providers=("openai",), allow_insecure_dev=True,
        qdrant_url="", qdrant_collection="", qdrant_api_key="", embedding_url="",
        neo4j_uri="", neo4j_user="", neo4j_password="",
    )


class RequestDurabilityTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = PublicStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()
        self.service = Mock()
        self.service.gate = GenerationGate()
        self.service.media.verify.return_value = {"status": "unavailable"}
        self.service.stickers.verify.return_value = {"status": "unavailable"}
        self.service.validate_content_blocks.return_value = []
        self.service.prepare_presence_arrival.return_value = {
            "decision": "noticed", "state_package": "synthetic-signed-state", "state": {},
        }
        self.service.failed_presence_arrival = PublicChatService.failed_presence_arrival
        self.service.finish_presence_arrival = AsyncMock(return_value={"model_called": True})
        self.service.chat = AsyncMock(return_value={"answer": "hello", "content_blocks": []})
        internal = Settings(Path("unused-data"), Path("unused-runtime"), False, "", [])
        self.app = create_app(settings(), internal, self.store, self.service)
        self.routes = {route.path: route.endpoint for route in self.app.routes if hasattr(route, "endpoint")}
        self.request = Request({
            "type": "http", "state": {"anonymous_id": "synthetic", "subject_hash": "subject"},
        })
        self.credential = patch(
            "backend.snow_app.public_main.open_byok_credential", return_value={"api_key": "synthetic"},
        )
        self.credential.start()
        self.shared = {
            "provider": "openai", "credential": "synthetic-credential-value", "model": "synthetic",
            "character_id": MVP_CHARACTERS[0].character_id,
        }

    async def asyncTearDown(self):
        self.credential.stop()
        await self.app.state.async_store.close()
        await self.app.state.provider_http.close()
        self.store.engine.dispose()

    async def test_arrival_write_failure_never_releases_paid_claim(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        complete = self.store.complete_request
        attempts = 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PublicStoreUnavailable("synthetic write outage")
            return complete(*args, **kwargs)

        endpoint = self.routes["/public/v1/presence/arrival"]
        with patch.object(self.store, "complete_request", side_effect=fail_once):
            with self.assertRaises(PublicStoreUnavailable):
                await endpoint(self.request, payload)
            replay = await endpoint(self.request, payload)
        self.assertEqual(self.service.finish_presence_arrival.await_count, 1)
        self.assertEqual(replay["terminal_error"], "generation_interrupted")
        self.assertTrue(replay["idempotent_replay"])

    async def test_arrival_post_generation_exception_replays_terminal(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        endpoint = self.routes["/public/v1/presence/arrival"]
        self.service.finish_presence_arrival.side_effect = RuntimeError("post-provider validation failed")
        with self.assertRaises(RuntimeError):
            await endpoint(self.request, payload)
        replay = await endpoint(self.request, payload)
        self.assertEqual(replay["terminal_error"], "generation_interrupted")
        self.assertEqual(self.service.finish_presence_arrival.await_count, 1)

    async def test_arrival_committed_result_survives_lost_write_acknowledgement(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        endpoint = self.routes["/public/v1/presence/arrival"]
        complete = self.store.complete_request

        def commit_then_lose_ack(*args, **kwargs):
            complete(*args, **kwargs)
            raise PublicStoreUnavailable("synthetic connection loss after commit")

        with patch.object(self.store, "complete_request", side_effect=commit_then_lose_ack):
            with self.assertRaises(PublicStoreUnavailable):
                await endpoint(self.request, payload)
            replay = await endpoint(self.request, payload)
        self.assertEqual(replay, {"model_called": True, "idempotent_replay": True})
        self.assertEqual(self.service.finish_presence_arrival.await_count, 1)

    def summary_payload(self) -> SummarizeRequest:
        return SummarizeRequest(
            request_id=uuid4(), **self.shared,
            turns=[
                {"role": role, "content_blocks": [{"type": "message", "text": "hello"}]}
                for role in ("user", "assistant")
            ],
        )

    async def test_summary_provider_error_replays_terminal_without_second_attempt(self):
        payload = self.summary_payload()
        endpoint = self.routes["/public/v1/chat/summarize"]
        self.service.summarize = AsyncMock(side_effect=ProviderRequestError("upstream_timeout", 504))
        with patch.object(Request, "is_disconnected", return_value=False):
            with self.assertRaises(HTTPException) as original:
                await endpoint(self.request, payload)
            self.assertEqual(original.exception.detail["code"], "upstream_timeout")
            with self.assertRaises(HTTPException) as replay:
                await endpoint(self.request, payload)
        self.assertEqual(replay.exception.detail["code"], "generation_interrupted")
        self.service.summarize.assert_awaited_once()
        self.assertEqual(self.app.state.active_request_leases, {})

    async def test_summary_terminal_write_outage_leaves_expiring_claim(self):
        payload = self.summary_payload()
        endpoint = self.routes["/public/v1/chat/summarize"]
        self.service.summarize = AsyncMock(
            side_effect=ValueError("synthetic post-provider validation failure"),
        )
        now = datetime(2026, 9, 7, tzinfo=UTC)
        with patch.object(Request, "is_disconnected", return_value=False), patch(
            "backend.snow_app.public_store._utcnow", return_value=now,
        ), patch.object(
            self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic write outage"),
        ):
            with self.assertRaises(PublicStoreUnavailable):
                await endpoint(self.request, payload)
            with self.assertRaises(HTTPException) as busy:
                await endpoint(self.request, payload)
            self.assertEqual(busy.exception.detail["code"], "request_in_progress")
        with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=46)):
            with self.assertRaises(HTTPException) as replay:
                await endpoint(self.request, payload)
        self.assertEqual(replay.exception.detail["code"], "generation_interrupted")
        self.service.summarize.assert_awaited_once()
        self.assertEqual(self.app.state.active_request_leases, {})

    async def test_arrival_admission_failure_can_retry_without_a_paid_attempt(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        endpoint = self.routes["/public/v1/presence/arrival"]
        self.service.finish_presence_arrival.side_effect = [
            GenerationBusy("generation_queue_full"), {"model_called": True},
        ]
        with self.assertRaises(GenerationBusy):
            await endpoint(self.request, payload)
        result = await endpoint(self.request, payload)
        self.assertTrue(result["model_called"])
        self.assertEqual(self.service.finish_presence_arrival.await_count, 2)

    async def test_arrival_pre_generation_state_rejection_keeps_security_error_contract(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        self.service.prepare_presence_arrival.side_effect = PublicSecurityError("State package invalid")
        with self.assertRaises(PublicSecurityError):
            await self.routes["/public/v1/presence/arrival"](self.request, payload)
        self.service.finish_presence_arrival.assert_not_awaited()
        self.assertEqual(self.app.state.active_request_leases, {})

    async def test_old_release_cannot_clear_reclaimed_request_or_subject_ownership(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        cache_id = "presence-arrival:" + str(payload.arrival_id)
        endpoint = self.routes["/public/v1/presence/arrival"]
        deleted, resume_release, new_started, finish_new = (asyncio.Event() for _ in range(4))
        call = self.app.state.async_store.call
        generations = 0

        async def release_then_pause(method, *args, **kwargs):
            result = await call(method, *args, **kwargs)
            if method == "release_request" and not deleted.is_set():
                deleted.set()
                await resume_release.wait()
            return result

        async def generate(*_args):
            nonlocal generations
            generations += 1
            if generations == 1:
                raise GenerationBusy("generation_queue_full")
            new_started.set()
            await finish_new.wait()
            return {"model_called": True}

        self.service.finish_presence_arrival.side_effect = generate
        old_task = new_task = None
        with patch.object(self.app.state.async_store, "call", side_effect=release_then_pause):
            try:
                old_task = asyncio.create_task(endpoint(self.request, payload))
                await asyncio.wait_for(deleted.wait(), timeout=2)
                new_task = asyncio.create_task(endpoint(self.request, payload))
                await asyncio.wait_for(new_started.wait(), timeout=2)
                resume_release.set()
                with self.assertRaises(GenerationBusy):
                    await old_task
                self.assertIs(self.app.state.active_request_leases.get(cache_id), new_task)
                self.assertEqual(self.app.state.active_subject_requests.get("subject"), cache_id)
                another = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
                with self.assertRaises(HTTPException) as busy:
                    await endpoint(self.request, another)
                self.assertEqual(busy.exception.detail["code"], "subject_generation_busy")
                finish_new.set()
                await new_task
                self.assertEqual(self.app.state.active_subject_requests, {})
            finally:
                resume_release.set()
                finish_new.set()
                await asyncio.gather(*(task for task in (old_task, new_task) if task), return_exceptions=True)

    async def test_cancellation_during_claim_does_not_leave_subject_permanently_busy(self):
        payload = ChatRequest(
            request_id=uuid4(), **self.shared, message="hello", communication_channel="text",
        )
        claimed = asyncio.Event()
        call = self.app.state.async_store.call

        async def claim_then_pause(method, *args, **kwargs):
            result = await call(method, *args, **kwargs)
            if method == "claim_request":
                claimed.set()
                await asyncio.Event().wait()
            return result

        with patch.object(self.app.state.async_store, "call", side_effect=claim_then_pause):
            task = asyncio.create_task(self.routes["/public/v1/chat/stream"](self.request, payload))
            try:
                await asyncio.wait_for(claimed.wait(), timeout=2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.app.state.active_subject_requests, {})
        self.assertEqual(self.app.state.active_request_leases, {})
        self.service.chat.assert_not_awaited()
        next_payload = payload.model_copy(update={"request_id": uuid4()})
        response = await self.routes["/public/v1/chat/stream"](self.request, next_payload)
        _output = [chunk async for chunk in response.body_iterator]
        self.service.chat.assert_awaited_once()

    async def test_chat_failed_write_stops_renewing_and_recovers_without_second_generation(self):
        payload = ChatRequest(
            request_id=uuid4(), **self.shared, message="hello", communication_channel="text",
        )
        endpoint = self.routes["/public/v1/chat/stream"]
        now = datetime(2026, 9, 7, tzinfo=UTC)
        with patch("backend.snow_app.public_store._utcnow", return_value=now), patch.object(
            self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic write outage"),
        ):
            response = await endpoint(self.request, payload)
            output = "".join([chunk async for chunk in response.body_iterator])
        self.assertIn("public_database_unavailable", output)
        self.assertEqual(self.app.state.active_request_leases, {})
        # Renew a distinct live request in the same process. The orphan must
        # still expire; process-level renewal must never keep it alive.
        owner = self.app.state.async_store.owner_token
        with patch("backend.snow_app.public_store._utcnow", return_value=now):
            await self.app.state.async_store.call("claim_request", "live", "other-subject", "other-hash")
        for seconds in (10, 20, 30, 40, 50):
            with patch(
                "backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=seconds),
            ):
                await self.app.state.async_store.call("renew_leases", owner, ("live",))
        with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=51)):
            response = await endpoint(self.request, payload)
            replay = "".join([chunk async for chunk in response.body_iterator])
        self.assertIn("generation_interrupted", replay)
        self.assertEqual(self.service.chat.await_count, 1)

    async def test_arrival_persistent_write_outage_expires_instead_of_rebilling(self):
        payload = PresenceArrivalRequest(arrival_id=uuid4(), **self.shared)
        endpoint = self.routes["/public/v1/presence/arrival"]
        now = datetime(2026, 9, 7, tzinfo=UTC)
        with patch("backend.snow_app.public_store._utcnow", return_value=now), patch.object(
            self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic write outage"),
        ):
            with self.assertRaises(PublicStoreUnavailable):
                await endpoint(self.request, payload)
            with self.assertRaises(HTTPException) as busy:
                await endpoint(self.request, payload)
            self.assertEqual(busy.exception.detail["code"], "request_in_progress")
        self.assertEqual(self.app.state.active_request_leases, {})
        with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=46)):
            replay = await endpoint(self.request, payload)
        self.assertEqual(replay["terminal_error"], "generation_interrupted")
        self.assertEqual(self.service.finish_presence_arrival.await_count, 1)

    async def test_lifespan_renews_only_detached_chat_while_its_job_is_live(self):
        payload = ChatRequest(
            request_id=uuid4(), **self.shared, message="hello", communication_channel="text",
        )
        started, release, tick, renewed = (asyncio.Event() for _ in range(4))
        snapshots = []
        loop = asyncio.get_running_loop()
        renew = self.store.renew_leases

        async def hold_generation(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"answer": "hello", "content_blocks": []}

        async def controlled_heartbeat(_delay):
            await tick.wait()
            tick.clear()

        def record_renewal(owner, request_ids):
            snapshots.append(tuple(request_ids))
            result = renew(owner, request_ids)
            loop.call_soon_threadsafe(renewed.set)
            return result

        self.service.chat.side_effect = hold_generation
        with patch(
            "backend.snow_app.public_main.asyncio.sleep", side_effect=controlled_heartbeat,
        ), patch.object(
            self.store, "renew_leases", side_effect=record_renewal,
        ), patch.object(
            self.store, "complete_request", side_effect=PublicStoreUnavailable("synthetic write outage"),
        ):
            async with self.app.router.lifespan_context(self.app):
                try:
                    response = await self.routes["/public/v1/chat/stream"](self.request, payload)
                    await asyncio.wait_for(started.wait(), timeout=2)
                    tick.set()
                    await asyncio.wait_for(renewed.wait(), timeout=2)
                    self.assertEqual(snapshots[-1], (str(payload.request_id),))
                    self.assertIs(
                        self.app.state.active_request_leases[str(payload.request_id)],
                        self.app.state.chat_jobs[str(payload.request_id)],
                    )
                    release.set()
                    _output = [chunk async for chunk in response.body_iterator]
                    renewed.clear()
                    tick.set()
                    await asyncio.wait_for(renewed.wait(), timeout=2)
                    self.assertEqual(snapshots[-1], ())
                finally:
                    release.set()


class SpecificLeaseRenewalTests(TestCase):
    def test_renewal_requires_both_live_request_and_matching_owner(self):
        store = PublicStore("sqlite+pysqlite:///:memory:")
        store.create_schema()
        now = datetime(2026, 9, 7, tzinfo=UTC)
        try:
            with patch("backend.snow_app.public_store._utcnow", return_value=now):
                for name, owner in (("active", "owner"), ("orphan", "owner"), ("foreign", "other")):
                    store.claim_request(name, "subject", "hash", owner_token=owner)
            for seconds in (10, 20, 30, 40, 50):
                with patch(
                    "backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=seconds),
                ):
                    self.assertEqual(store.renew_leases("owner", ("active", "foreign")), 1)
            with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=51)):
                for name in ("orphan", "foreign"):
                    status, result = store.claim_request(name, "subject", "hash", owner_token="new")
                    self.assertEqual(status, "completed")
                    self.assertEqual(result["terminal_error"], "generation_interrupted")
                store.complete_request("active", {"answer": "done"}, owner_token="owner")
        finally:
            store.engine.dispose()
