"""Real PostgreSQL quota, paid-result recovery and actual old/new/old contracts."""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import inspect, text

from backend.snow_app.public_store import PublicStore
from tests.test_postgres_integration import database, migrate, old_store_module
from tests import test_proactive_message as proactive_tests

pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required")


@pytest.mark.parametrize("case_name", [
    name for name in vars(proactive_tests.ProactiveMessageTests) if name.startswith("test_")
])
def test_real_postgres_proactive_http_contract(database, case_name):
    migrate(database)
    async def exercise():
        case = proactive_tests.ProactiveMessageTests(methodName=case_name)
        case.store_engine = database
        await case.asyncSetUp()
        try:
            await getattr(case, case_name)()
        finally:
            await case.asyncTearDown()
    asyncio.run(exercise())


def authority(store, rid, *, closed=False, character="synthetic-character"):
    with store.begin() as connection:
        store._record_rendezvous(connection, {"subject": "synthetic-subject", "character": character,
            "day": "2026-10-19", "revision": 1, "created_id": None if closed else rid,
            "closed_ids": [rid] if closed else [], "expires": datetime.now(UTC) + timedelta(days=3)})


def claim(store, request_id, rid, *, owner="synthetic-worker", character="synthetic-character"):
    return store.claim_proactive(request_id, "synthetic-subject", "synthetic-body-hash",
        day="2026-10-19", rendezvous_id=rid, character_id=character,
        expires=datetime.now(UTC) + timedelta(days=3), owner_token=owner)[0]


def test_concurrent_daily_quota_and_same_rendezvous_are_atomic(database):
    migrate(database)
    store = PublicStore("", engine=database)
    for index in range(8):
        authority(store, f"synthetic-rendezvous-{index}", character=f"synthetic-character-{index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda index: claim(store, f"proactive:{index}", f"synthetic-rendezvous-{index}",
            character=f"synthetic-character-{index}"), range(8)))
    assert statuses.count("claimed") == 3
    assert statuses.count("proactive_daily_limit") == 5
    with database.connect() as connection:
        assert connection.execute(text("SELECT reserved FROM public_proactive_day")).scalar_one() == 3
        assert connection.execute(text("SELECT count(*) FROM public_proactive_attempt")).scalar_one() == 3


def test_concurrent_same_rendezvous_and_permanent_close_cannot_reopen(database):
    migrate(database)
    store = PublicStore("", engine=database)
    authority(store, "synthetic-one-rendezvous")
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda index: claim(store, f"proactive:{index}", "synthetic-one-rendezvous"), range(8)))
    assert statuses.count("claimed") == 1
    assert statuses.count("rendezvous_already_used") == 7
    authority(store, "synthetic-closed-rendezvous", closed=True)
    authority(store, "synthetic-closed-rendezvous")
    assert claim(store, "proactive:closed", "synthetic-closed-rendezvous") == "rendezvous_not_active"


def test_0009_actual_old_new_old_preserves_ledger_after_old_cache_cleanup(database):
    old = old_store_module(os.getenv("TEST_PROACTIVE_OLD_APP_REF") or "95005fe2efade3e9c3b916e8c47c73fced5b19e0")
    migrate(database, "20261013_0008")
    old_store = old.PublicStore("", engine=database)
    old_store.claim_request("old-before", "synthetic-subject", "old-hash")
    old_store.complete_request("old-before", {"answer": "synthetic old before expansion"})
    with database.connect() as connection:
        columns = inspect(connection).get_columns("public_request_cache")
        before = [(row["name"], str(row["type"]), row["nullable"], row["default"]) for row in columns]
    migrate(database)
    migrate(database)
    with database.connect() as connection:
        columns = inspect(connection).get_columns("public_request_cache")
        assert before == [(row["name"], str(row["type"]), row["nullable"], row["default"]) for row in columns]
    store = PublicStore("", engine=database)
    assert store.request_result("old-before", "synthetic-subject")["answer"] == "synthetic old before expansion"
    authority(store, "synthetic-new-rendezvous")
    assert claim(store, "proactive:new", "synthetic-new-rendezvous") == "claimed"
    store.complete_proactive("proactive:new", {"status": "completed", "content_blocks": [{"type": "message", "text": "合成提醒。"}],
        "usage": {"provider_calls": 1, "total_tokens": None}}, owner_token="synthetic-worker")
    # Roll back the application binary, retaining the additive schema. The old
    # binary remains writable and its cache cleanup cannot delete quota rows.
    old_store.claim_request("old-after", "synthetic-subject", "old-hash")
    old_store.complete_request("old-after", {"answer": "synthetic old after expansion"})
    assert old_store.request_result("old-after", "synthetic-subject")["answer"] == "synthetic old after expansion"
    later = datetime.now(UTC) + timedelta(minutes=11)
    with patch.object(old, "_utcnow", return_value=later):
        old_store.cleanup()
    assert store.lookup_proactive("proactive:new", "synthetic-subject", "synthetic-body-hash") == ("unknown", None)
    assert claim(store, "proactive:different", "synthetic-new-rendezvous") == "rendezvous_already_used"
    with database.connect() as connection:
        assert connection.execute(text("SELECT reserved FROM public_proactive_day")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM public_proactive_attempt")).scalar_one() == 1
