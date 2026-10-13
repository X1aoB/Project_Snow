"""Synthetic public capacity checks; never call a paid provider or live data."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from backend.snow_app.async_store import AsyncPublicStore
from backend.snow_app.dialogue_core import GenerationBusy, GenerationGate


class _SyntheticStore:
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __init__(self, target_active: int = 0) -> None:
        self.active = 0
        self.max_active = 0
        self.target_active = target_active
        self.ready = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def operation(self, delay: float) -> bool:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.target_active and self.active >= self.target_active:
                self.ready.set()
        try:
            if self.target_active:
                self.release.wait(2)
            else:
                time.sleep(delay)
            return True
        finally:
            with self.lock:
                self.active -= 1


class PublicConcurrencyTests(IsolatedAsyncioTestCase):
    async def test_store_capacity_covers_generation_admission_envelope(self) -> None:
        store = _SyntheticStore(target_active=5)
        adapter = AsyncPublicStore(store, workers=5)
        try:
            tasks = [asyncio.create_task(adapter.call("operation", 0.01)) for _ in range(13)]
            self.assertTrue(await asyncio.to_thread(store.ready.wait, 1))
            store.release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            store.release.set()
            await adapter.close()

        self.assertTrue(all(result is True for result in results), results)
        self.assertEqual(store.max_active, 5)

    async def test_generation_gate_keeps_four_active_and_eight_waiting(self) -> None:
        gate = GenerationGate(active=4, queued=8, queue_timeout=1)
        active = 0
        max_active = 0
        active_ready = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 4:
                active_ready.set()
            try:
                await release.wait()
            finally:
                active -= 1

        async def acquire_and_hold() -> str:
            try:
                await gate.run(hold)
                return "accepted"
            except GenerationBusy as exc:
                return str(exc)

        running = [asyncio.create_task(acquire_and_hold()) for _ in range(4)]
        await asyncio.wait_for(active_ready.wait(), timeout=1)
        waiting = [asyncio.create_task(acquire_and_hold()) for _ in range(9)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*running, *waiting)

        self.assertEqual(results.count("accepted"), 12)
        self.assertEqual(results.count("generation_queue_full"), 1)
        self.assertEqual(max_active, 4)
