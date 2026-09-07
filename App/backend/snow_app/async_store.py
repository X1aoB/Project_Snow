"""Bounded async adapter for the synchronous, transaction-owning public store."""

from __future__ import annotations

import asyncio
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from .public_store import PublicStore, PublicStoreUnavailable


class AsyncPublicStore:
    """Keep complete transactions off the ASGI event loop.

    Capacity is released when the actual transaction ends, including when an
    HTTP caller disconnects. A cancellation never pretends a write rolled back.
    PostgreSQL statement/lock/connect timeouts bound the active operation.
    """

    def __init__(self, store: PublicStore, *, workers: int = 5, queued: int = 5):
        self.store = store
        self.owner_token = secrets.token_hex(16)
        # SQLite's test StaticPool shares one connection: serialize transactions.
        if store.engine is not None and store.engine.dialect.name == "sqlite":
            workers = 1
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snow-store")
        self._capacity = threading.BoundedSemaphore(workers + queued)
        self._closed = False

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self._closed or not self._capacity.acquire(blocking=False):
            raise PublicStoreUnavailable("public database capacity exhausted")
        if method in {"claim_request", "complete_request", "release_request"}:
            kwargs.setdefault("owner_token", self.owner_token)
        try:
            future = self._executor.submit(partial(getattr(self.store, method), *args, **kwargs))
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda _future: self._capacity.release())
        # Shield the thread's completion; canceling an await cannot cancel a DB
        # transaction already submitted to the executor.
        completion = asyncio.wrap_future(future)
        # A disconnected caller will no longer retrieve a possible late failure.
        completion.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(completion)

    async def close(self) -> None:
        self._closed = True
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=False)
