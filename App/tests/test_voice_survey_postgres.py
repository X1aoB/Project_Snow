"""Real PostgreSQL contract for the additive survey table, including old/new/old."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text

from backend.snow_app.public_store import PublicStore
from backend.snow_app.voice_survey_store import VOICE_SURVEY_RETENTION_DAYS
from tests.test_postgres_integration import database, migrate, old_store_module  # noqa: F401

pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required")


def count(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM public_voice_survey_response")).scalar_one()


def test_concurrent_submissions_from_one_visitor_keep_one_row(database):
    migrate(database)
    store = PublicStore("", engine=database)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: store.save_voice_survey("synthetic-subject", "voice-survey-1", {"n": index}, ""), range(16)))
    assert count(database) == 1
    assert store.voice_survey_answered("synthetic-subject")


def test_retention_cleanup_uses_the_real_column(database):
    migrate(database)
    store = PublicStore("", engine=database)
    past = datetime.now(UTC) - timedelta(days=VOICE_SURVEY_RETENTION_DAYS + 1)
    store.save_voice_survey("synthetic-old", "voice-survey-1", {}, "", now=past)
    store.save_voice_survey("synthetic-new", "voice-survey-1", {}, "")
    assert store.cleanup()["public_voice_survey_response"] == 1
    assert count(database) == 1


def test_real_old_binary_works_before_and_after_the_expansion(database):
    # 1.1.0 is the release this one replaces and the one a rollback returns to.
    old = old_store_module(os.getenv("TEST_VOICE_SURVEY_OLD_APP_REF") or "bddd613a6b0844f1712f0402ab06ed3346489f64")
    migrate(database, "20261020_0010")
    old_store = old.PublicStore("", engine=database)
    old_store.claim_request("old-before", "synthetic-subject", "old-hash")
    old_store.complete_request("old-before", {"answer": "synthetic old before expansion"})
    with database.connect() as connection:
        before = {table: [column["name"] for column in inspect(connection).get_columns(table)]
                  for table in inspect(connection).get_table_names() if table != "alembic_version"}
    migrate(database)
    with database.connect() as connection:
        after = {table: [column["name"] for column in inspect(connection).get_columns(table)]
                 for table in inspect(connection).get_table_names() if table != "alembic_version"}
    assert set(after) - set(before) == {"public_voice_survey_response"}
    assert {table: after[table] for table in before} == before

    store = PublicStore("", engine=database)
    assert store.request_result("old-before", "synthetic-subject")["answer"] == "synthetic old before expansion"
    store.save_voice_survey("synthetic-subject", "voice-survey-1", {"use_voice": "often"}, "")

    # Roll the binary back and keep the schema: the old store still writes and
    # its cleanup leaves the survey row it does not know about.
    old_store.claim_request("old-after", "synthetic-subject", "old-hash")
    old_store.complete_request("old-after", {"answer": "synthetic old after expansion"})
    assert old_store.request_result("old-after", "synthetic-subject")["answer"] == "synthetic old after expansion"
    old_store.cleanup()
    assert count(database) == 1
