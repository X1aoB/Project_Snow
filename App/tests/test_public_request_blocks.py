"""Synthetic confirmed-block persistence, fencing, replay and cleanup checks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import text

from backend.snow_app.async_store import AsyncPublicStore
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable

NOW = datetime(2026, 10, 13, tzinfo=UTC)
BLOCKS = [{"type": "message", "text": "第一段。"}, {"type": "message", "text": "第二段。"}]


@pytest.fixture
def store():
    public_store = PublicStore("sqlite+pysqlite:///:memory:")
    public_store.create_schema()
    with patch("backend.snow_app.public_store._utcnow", return_value=NOW):
        public_store.claim_request("turn", "subject", "hash", owner_token="owner")
        yield public_store
    public_store.engine.dispose()


def publish(store, blocks=BLOCKS, **kwargs):
    return store.commit_request_blocks("turn", "subject", blocks, owner_token=kwargs.get("owner", "owner"))


def test_confirmed_prefix_is_ordered_immutable_idempotent_and_subject_scoped(store):
    first = publish(store, BLOCKS[:1])
    assert first[0]["sequence"] == 0
    assert len(first[0]["block_id"]) == 64
    full = publish(store)
    assert full[:1] == first
    assert [event["sequence"] for event in full] == [0, 1]
    assert publish(store) == full
    assert store.request_blocks("turn", "subject") == full
    assert store.request_blocks("turn", "foreign") == []
    assert store.request_blocks("unknown", "subject") == []
    with pytest.raises(PublicStoreUnavailable):
        publish(store, BLOCKS[:1])
    with pytest.raises(PublicStoreUnavailable):
        publish(store, [{"type": "message", "text": "不能改写"}, BLOCKS[1]])
    assert store.request_blocks("turn", "subject") == full


@pytest.mark.parametrize("blocks", [
    [{"type": "sticker", "text": "not a text block"}],
    [{"type": "message", "text": " "}],
    [{"type": "message", "text": "a\x00b"}],
    [{"type": "message", "text": "x" * 2001}],
    [{"type": "message", "text": "x" * 1000}] * 2,
    [{"type": "message", "text": "x"}] * 9,
    [{"type": "message", "text": "x", "sequence": 2}],
    [{"type": "message", "text": 3}],
    "not blocks",
])
def test_invalid_or_unbounded_publication_is_rejected_without_writes(store, blocks):
    with pytest.raises(PublicStoreUnavailable):
        publish(store, blocks)
    assert store.request_blocks("turn", "subject") == []


def test_live_owner_subject_and_request_lifetime_are_required(store):
    for owner in (None, "other"):
        with pytest.raises(PublicStoreUnavailable):
            publish(store, owner=owner)
    with pytest.raises(PublicStoreUnavailable):
        store.commit_request_blocks("turn", "foreign", BLOCKS, owner_token="owner")
    with patch("backend.snow_app.public_store._utcnow", return_value=NOW + timedelta(seconds=46)):
        with pytest.raises(PublicStoreUnavailable):
            publish(store)
    with store.begin() as connection:
        connection.execute(text("UPDATE public_request_cache SET expires_at = :now"), {"now": NOW})
    with pytest.raises(PublicStoreUnavailable):
        publish(store)


def test_failed_append_transaction_does_not_leave_a_partially_published_prefix(store):
    with store.begin() as connection:
        connection.execute(text("""CREATE TRIGGER reject_second_block BEFORE INSERT ON public_request_blocks
            WHEN NEW.sequence = 1 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""))
    with pytest.raises(PublicStoreUnavailable):
        publish(store)
    assert store.request_blocks("turn", "subject") == []


def test_success_must_match_published_blocks_and_answer(store):
    events = publish(store, BLOCKS[:1])
    invalid = (
        {"answer": "changed", "content_blocks": [{"type": "message", "text": "changed"}]},
        {"answer": "changed", "content_blocks": BLOCKS},
        {"answer": "", "content_blocks": []},
    )
    for response in invalid:
        with pytest.raises(PublicStoreUnavailable):
            store.complete_request("turn", response, owner_token="owner")
        assert store.request_result("turn", "subject") is None
        assert store.request_blocks("turn", "subject") == events
    response = {"answer": "第一段。\n第二段。", "content_blocks": BLOCKS}
    with pytest.raises(PublicStoreUnavailable):
        store.complete_request("turn", response)
    store.complete_request("turn", response, owner_token="owner")
    assert store.request_result("turn", "subject") == response
    assert store.request_blocks("turn", "subject") == events
    with pytest.raises(PublicStoreUnavailable):
        publish(store)


def test_terminal_failure_and_expired_owner_retain_confirmed_blocks(store):
    events = publish(store)
    with patch("backend.snow_app.public_store._utcnow", return_value=NOW + timedelta(seconds=46)):
        assert store.recover_expired_requests() == 1
        assert store.request_result("turn", "subject")["terminal_error"] == "generation_interrupted"
        assert store.request_blocks("turn", "subject") == events
        with pytest.raises(PublicStoreUnavailable):
            publish(store)
        with pytest.raises(PublicStoreUnavailable):
            store.complete_request("turn", {"terminal_error": "late"}, owner_token="owner")


def test_explicit_terminal_error_keeps_prefix_for_replay(store):
    events = publish(store)
    response = {"terminal_error": "generation_interrupted", "answer": "", "content_blocks": []}
    store.complete_request("turn", response, owner_token="owner")
    assert store.request_result("turn", "subject") == response
    assert store.request_blocks("turn", "subject") == events


@pytest.mark.parametrize("method", ["cleanup", "claim_request"])
def test_expired_blocks_follow_cache_cleanup_and_are_never_replayed(store, method):
    publish(store)
    with patch("backend.snow_app.public_store._utcnow", return_value=NOW + timedelta(days=2)):
        assert store.request_blocks("turn", "subject") == []
        if method == "cleanup":
            assert store.cleanup()["public_request_blocks"] == 2
        else:
            store.claim_request("other", "subject", "hash", owner_token="owner")
        with store.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM public_request_blocks")).scalar_one() == 0


def test_release_cleans_blocks_only_for_matching_owner(store):
    events = publish(store)
    store.release_request("turn", owner_token="other")
    assert store.request_blocks("turn", "subject") == events
    store.release_request("turn", owner_token="owner")
    assert store.request_blocks("turn", "subject") == []
    with store.begin() as connection:
        assert connection.execute(text("SELECT count(*) FROM public_request_blocks")).scalar_one() == 0


def test_async_adapter_injects_lease_owner_for_block_publication(store):
    async def run():
        adapter = AsyncPublicStore(store)
        try:
            await adapter.call("claim_request", "async", "subject", "hash")
            events = await adapter.call("commit_request_blocks", "async", "subject", BLOCKS)
            assert await adapter.call("request_blocks", "async", "subject") == events
        finally:
            await adapter.close()
    asyncio.run(run())
