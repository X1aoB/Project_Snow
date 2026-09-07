from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.snow_app.async_store import AsyncPublicStore
from backend.snow_app.dialogue_core import (
    DialogueContext,
    DialogueEngine,
    GenerationBudget,
    GenerationBudgetExceeded,
    ScopedStateMap,
    active_generation_budget,
)
from backend.snow_app.local_boundary import bounded_body, install_local_boundary
from backend.snow_app.mvp_service import MVPProviderError, MVPService
from backend.snow_app.public_providers import PROVIDERS
from backend.snow_app.public_repository import PublicRuntimeRepository
from backend.snow_app.public_service import _public_immersive_thinking_decision
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable


class BudgetAndIsolationTests(TestCase):
    def test_engine_budget_counts_real_http_attempt_and_blocks_repair(self):
        service = MVPService.__new__(MVPService)
        service._generation_diagnostics = threading.local()
        service._model_http_client = Mock()
        budget = GenerationBudget(1)

        def generate(_context, _budget):
            service._post_model("https://provider.invalid")
            with self.assertRaises(MVPProviderError):
                service._post_model("https://provider.invalid")
            return {"answer": "first answer"}

        result = DialogueEngine().generate(DialogueContext("synthetic", "hello", "text"), budget, generate)
        self.assertEqual(result.payload["answer"], "first answer")
        self.assertEqual(budget.provider_calls, 1)
        self.assertEqual(service.generation_diagnostics()["provider_http_calls"], 1)
        service._model_http_client.post.assert_called_once()
        self.assertIsNone(active_generation_budget())

    def test_one_call_budget_is_enforced_and_not_reported_as_two(self):
        decision = _public_immersive_thinking_decision(PROVIDERS["openai"], 1)
        self.assertEqual(decision["max_provider_http_calls"], 1)
        budget = GenerationBudget(1)
        budget.consume()
        with self.assertRaises(GenerationBudgetExceeded):
            budget.consume()
        self.assertEqual(budget.provider_calls, 1)

    def test_public_scopes_are_isolated_from_each_other_and_local_state(self):
        states = ScopedStateMap("test-sessions")
        states["same-id"] = {"value": "local"}
        barrier = Event()

        def worker(value):
            with states.isolate({"same-id": {"value": value}}):
                barrier.wait(1)
                return states["same-id"]["value"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            a, b = executor.submit(worker, "a"), executor.submit(worker, "b")
            barrier.set()
            self.assertEqual({a.result(), b.result()}, {"a", "b"})
        self.assertEqual(states["same-id"]["value"], "local")


class LeaseTests(TestCase):
    def setUp(self):
        self.store = PublicStore("sqlite+pysqlite:///:memory:")
        self.store.create_schema()

    def tearDown(self):
        self.store.engine.dispose()

    def test_worker_loss_replays_interruption_without_reclaiming(self):
        now = datetime(2026, 9, 7, tzinfo=UTC)
        with patch("backend.snow_app.public_store._utcnow", return_value=now):
            self.assertEqual(
                self.store.claim_request("request", "subject", "hash", owner_token="old")[0], "claimed"
            )
        with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=46)):
            status, payload = self.store.claim_request("request", "subject", "hash", owner_token="new")
            self.assertEqual(status, "completed")
            self.assertEqual(payload["terminal_error"], "generation_interrupted")
            with self.assertRaises(PublicStoreUnavailable):
                self.store.complete_request("request", {"answer": "late"}, owner_token="old")

    def test_other_owner_cannot_complete_or_release_a_claim(self):
        self.store.claim_request("request", "subject", "hash", owner_token="owner")
        self.store.release_request("request", owner_token="stranger")
        with self.assertRaises(PublicStoreUnavailable):
            self.store.complete_request("request", {"answer": "forged"}, owner_token="stranger")
        self.store.complete_request("request", {"answer": "owned"}, owner_token="owner")
        self.assertEqual(self.store.request_result("request", "subject")["answer"], "owned")

    def test_ownerless_legacy_claims_still_work(self):
        self.store.claim_request("old-client", "subject", "hash")
        self.store.complete_request("old-client", {"answer": "legacy"})
        self.assertEqual(self.store.claim_request("old-client", "subject", "hash")[0], "completed")


class AsyncStoreTests(IsolatedAsyncioTestCase):
    async def test_canceled_caller_does_not_free_running_transaction_capacity(self):
        store = PublicStore("sqlite+pysqlite:///:memory:")
        started, release = Event(), Event()

        def slow_health():
            started.set()
            release.wait(2)
            return True

        store.health = slow_health
        adapter = AsyncPublicStore(store, workers=1, queued=0)
        task = asyncio.create_task(adapter.call("health"))
        try:
            await asyncio.to_thread(started.wait, 1)
            # A synchronous DB operation is running, but the event loop proceeds.
            await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with self.assertRaises(PublicStoreUnavailable):
                await adapter.call("health")
        finally:
            release.set()
            await adapter.close()
            store.engine.dispose()


class LocalBoundaryTests(TestCase):
    def setUp(self):
        self.app = FastAPI()
        install_local_boundary(self.app, ["http://localhost:8080"], legacy_enabled=False)

        @self.app.get("/api/v1/bootstrap")
        def bootstrap():
            return {"ok": True}

        @self.app.post("/api/v1/attachments")
        async def upload(request: Request):
            return {"size": len(await bounded_body(request, 8))}

    def test_untrusted_host_origin_and_legacy_routes_are_rejected(self):
        with TestClient(self.app) as client:
            self.assertEqual(
                client.get("/api/v1/bootstrap", headers={"Host": "evil.example"}).status_code, 400
            )
            self.assertEqual(
                client.post(
                    "/api/v1/attachments", content=b"x", headers={"Origin": "https://evil.example"}
                ).status_code,
                403,
            )
            self.assertEqual(client.get("/api/v1/agent/runs").status_code, 404)

    def test_actual_stream_limit_does_not_trust_content_length(self):
        with TestClient(self.app) as client:
            result = client.post(
                "/api/v1/attachments", content=iter([b"abcd", b"efghi"]), headers={"Content-Length": "1"}
            )
            self.assertEqual(result.status_code, 413)
            self.assertEqual(client.post("/api/v1/attachments", content=b"12345678").json()["size"], 8)

    def test_network_client_requires_bootstrap_cookie_and_matching_origin(self):
        async def exercise():
            transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 12345))
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as client:
                headers = {"Origin": "http://localhost:8080"}
                self.assertEqual(
                    (await client.post("/api/v1/attachments", content=b"x", headers=headers)).status_code, 403
                )
                response = await client.get("/api/v1/bootstrap")
                self.assertIn("HttpOnly", response.headers["set-cookie"])
                self.assertEqual((await client.post("/api/v1/attachments", content=b"x")).status_code, 403)
                self.assertEqual(
                    (await client.post("/api/v1/attachments", content=b"x", headers=headers)).status_code, 200
                )

        asyncio.run(exercise())


class RetrievalDeadlineTests(TestCase):
    def test_slow_vector_keeps_lexical_result_and_graph_shares_deadline(self):
        repository = PublicRuntimeRepository.__new__(PublicRuntimeRepository)
        repository._request_context = ContextVar("deadline-test", default=None)
        repository._health_local = threading.local()
        repository._neo4j_lock = threading.RLock()
        repository._circuit_until = {}
        repository._retrieval_capacity = threading.BoundedSemaphore(2)
        repository._executor = ThreadPoolExecutor(max_workers=2)
        release = Event()
        repository.lexical_search = lambda *_args: [("document", 1)]

        def slow_vector(*_args):
            release.wait(2)
            return []

        repository.vector_search = slow_vector
        repository.documents_by_id = lambda: {
            "document": {
                "page_id": "page",
                "title": "Synthetic",
                "source_type": "test",
                "text": "lexical evidence",
                "metadata": {"source_priority": 1},
            },
        }
        repository._is_allowed_context = lambda *_args: True
        repository._serving_graph_context_sync = Mock()
        repository.reset_request_health()
        repository._collector()["deadline"] = time.monotonic() + 0.05
        started = time.monotonic()
        try:
            mode, used_vectors, results = repository.hybrid_search("test", None, 3)
            graph = repository.serving_graph_context("test", None, ())
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(mode, "lexical_only")
            self.assertFalse(used_vectors)
            self.assertEqual(results[0]["text"], "lexical evidence")
            self.assertEqual(graph["status"], "degraded")
            repository._serving_graph_context_sync.assert_not_called()
            repository.reset_request_health()
            self.assertFalse(repository._available("qdrant"))
        finally:
            release.set()
            repository._executor.shutdown(wait=True)
