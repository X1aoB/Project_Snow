"""Explicit, persistence-free contracts shared by local and public dialogue.

The engine owns orchestration, while adapters own storage and wire formats.
No constructor in this module opens a database or reads environment variables.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


class GenerationBudgetExceeded(RuntimeError):
    pass


@dataclass
class GenerationBudget:
    max_provider_calls: int = 2
    provider_calls: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.max_provider_calls <= 2:
            raise ValueError("provider call budget must be 1 or 2")

    def consume(self) -> None:
        if self.provider_calls >= self.max_provider_calls:
            raise GenerationBudgetExceeded("provider_call_budget_exhausted")
        self.provider_calls += 1


_ACTIVE_BUDGET: ContextVar[GenerationBudget | None] = ContextVar("dialogue_budget", default=None)


def active_generation_budget() -> GenerationBudget | None:
    return _ACTIVE_BUDGET.get()


@dataclass(frozen=True)
class WorldState:
    """An adapter-provided snapshot; the engine never mutates the input."""

    values: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", deepcopy(self.values))


@dataclass(frozen=True)
class DialogueContext:
    character_id: str
    message: str
    communication_channel: str
    world: WorldState = field(default_factory=WorldState)


@dataclass(frozen=True)
class DialogueResult:
    """Retain the existing v1 payload while extracting the implementation."""

    payload: dict[str, Any]


class DialogueEngine:
    """Pure orchestration boundary around a supplied generation implementation."""

    def generate(
        self,
        context: DialogueContext,
        budget: GenerationBudget,
        generate: Callable[[DialogueContext, GenerationBudget], dict[str, Any]],
    ) -> DialogueResult:
        if context.communication_channel not in {"text", "in_person"}:
            raise ValueError("unsupported communication channel")
        token = _ACTIVE_BUDGET.set(budget)
        try:
            return DialogueResult(generate(context, budget))
        finally:
            _ACTIVE_BUDGET.reset(token)


class ScopedStateMap(MutableMapping[str, dict[str, Any]]):
    """Compatibility map with explicit isolated state for public requests.

    Local sessions retain their process cache. Public calls install a fresh
    context snapshot, so neither success nor exception touches the local cache.
    """

    def __init__(self, name: str):
        self._local: dict[str, dict[str, Any]] = {}
        self._scope: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(name, default=None)

    def _values(self) -> dict[str, dict[str, Any]]:
        value = self._scope.get()
        return self._local if value is None else value

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self._values()[key]

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        self._values()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._values()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values())

    def __len__(self) -> int:
        return len(self._values())

    @contextmanager
    def isolate(self, state: dict[str, dict[str, Any]]) -> Iterator[None]:
        token = self._scope.set(deepcopy(state))
        try:
            yield
        finally:
            self._scope.reset(token)


class GenerationBusy(RuntimeError):
    pass


class GenerationGate:
    def __init__(self, active: int = 4, queued: int = 8, queue_timeout: float = 30):
        self.active_limit = active
        self._active = 0
        self.semaphore = asyncio.Semaphore(active)
        self.queued_limit = queued
        self.queue_timeout = queue_timeout
        self._lock = asyncio.Lock()
        self._waiting = 0

    @contextmanager
    def _noop(self) -> Iterator[None]:
        yield

    async def acquire(self) -> None:
        async with self._lock:
            if self.semaphore.locked() and self._waiting >= self.queued_limit:
                raise GenerationBusy("generation_queue_full")
            self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=self.queue_timeout)
                self._active += 1
            except TimeoutError as exc:
                raise GenerationBusy("generation_queue_timeout") from exc
        finally:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)

    def release(self) -> None:
        self._active = max(0, self._active - 1)
        self.semaphore.release()

    def snapshot(self) -> dict[str, int]:
        """Event-loop-local operational counts; no subject or request identifiers."""
        return {
            "active": self._active,
            "queued": self._waiting,
            "active_limit": self.active_limit,
            "queue_limit": self.queued_limit,
        }

    async def run(self, callback):
        await self.acquire()
        try:
            return await callback()
        finally:
            self.release()
