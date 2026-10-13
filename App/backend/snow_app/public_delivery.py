"""Bounded, event-loop-owned notifications for durable public block delivery.

This journal is an acceleration cache only. Reconnects recover content from
PublicStore; losing this process must never cause another model invocation.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any


class DeliveryJournal:
    PHASES = frozenset({"queued", "preparing", "retrieving", "generating", "validating", "saving"})

    def __init__(self, request_id: str, character_id: str) -> None:
        self.request_id = request_id
        self.character_id = character_id
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.changed = asyncio.Event()
        self.blocks: list[dict[str, Any]] = []
        self._phases: set[str] = set()

    def phase(self, name: str, elapsed_ms: int = 0) -> None:
        if name not in self.PHASES or name in self._phases:
            return
        self._phases.add(name)
        self.events.append(("progress", {"phase": name, "elapsed_ms": max(0, int(elapsed_ms))}))
        self.changed.set()

    def committed(self, records: list[dict[str, Any]]) -> None:
        # Called only after the store acknowledges its transaction. At most
        # eight blocks and six phase changes are retained per active request.
        if len(records) > 8 or records[:len(self.blocks)] != self.blocks:
            raise ValueError("committed prefix changed")
        for record in records[len(self.blocks):]:
            if record.get("sequence") != len(self.blocks):
                raise ValueError("committed sequence gap")
            self.blocks.append(deepcopy(record))
            self.events.append(("committed_block", {
                "request_id": self.request_id,
                "character_id": self.character_id,
                **deepcopy(record),
            }))
        self.changed.set()


def latency_metrics(diagnostics: dict[str, Any]) -> dict[str, int]:
    """An explicit numeric allowlist; never forward arbitrary provider data."""
    raw = diagnostics.get("timings_ms")
    if not isinstance(raw, dict):
        return {}
    allowed = (
        "queue_ms", "context_build_ms", "retrieval_ms", "prompt_build_ms", "hybrid_total", "initial_model_ms",
        "provider_headers_ms", "provider_first_content_ms", "provider_first_event_ms", "guard_rewrite_ms",
        "empty_recovery_ms", "validation_ms", "validation_local_ms", "validation_total_ms",
        "first_verified_block_ms", "durable_commit_ms", "first_committed_block_ms", "total",
    )
    return {
        key: max(0, min(3_600_000, int(raw[key])))
        for key in allowed
        if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool)
        and 0 <= raw[key] <= 3_600_000
    }
