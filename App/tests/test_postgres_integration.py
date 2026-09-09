"""Real PostgreSQL contract checks; never fall back to SQLite.

Each test creates a random schema in the explicitly supplied TEST_POSTGRES_URL
database. No production secret discovery or user runtime data is used.
"""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable, RateLimitExceeded

APP = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required")


@pytest.fixture
def database():
    url = os.environ["TEST_POSTGRES_URL"]
    if not url.startswith("postgresql"):
        pytest.fail("TEST_POSTGRES_URL must point to PostgreSQL")
    schema = "snow_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url,
        connect_args={
            "options": f"-c search_path={schema},public -c statement_timeout=5000 -c lock_timeout=2000"
        },
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def migrate(engine, revision="head"):
    config = Config(str(APP / "alembic.ini"))
    config.set_main_option("script_location", str(APP / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_empty_database_upgrade_and_repeated_upgrade(database):
    migrate(database)
    migrate(database)
    schema = database.connect()
    try:
        tables = inspect(schema).get_table_names()
        assert "public_request_leases" in tables
        assert "public_feedback_email_outbox" in tables
        assert schema.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260907_0005"
    finally:
        schema.close()


def test_upgrade_preserves_feedback_and_old_cache_schema(database):
    migrate(database, "20260819_0004")
    with database.begin() as connection:
        connection.execute(
            text("""INSERT INTO public_request_cache
            (request_id,subject_hash,request_hash,status,response_json,created_at,expires_at)
            VALUES ('legacy','subject','hash','completed','{"answer":"saved"}',
                    now(),now()+interval '10 minutes')""")
        )
    migrate(database)
    store = PublicStore("", engine=database)
    assert store.claim_request("legacy", "subject", "hash")[1]["answer"] == "saved"
    store.insert_feedback(
        subject_hash="subject",
        ip_fingerprint="synthetic",
        body_text="upgrade retained",
        context={},
        qq_cipher=None,
    )
    migrate(database)
    assert len(store.feedback_rows(10)) == 1


def old_store_module():
    reference = os.getenv("TEST_OLD_APP_REF")
    if not reference:
        pytest.skip("TEST_OLD_APP_REF required for actual old-code compatibility")
    source = subprocess.run(
        ["git", "show", f"{reference}:App/backend/snow_app/public_store.py"],
        cwd=APP.parent,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    old = ModuleType("old_snow_public_store")
    exec(compile(source, "old_public_store.py", "exec"), old.__dict__)
    return old


def test_real_old_store_remains_usable_after_additive_migration(database):
    old = old_store_module()
    migrate(database)
    store = old.PublicStore("", engine=database)
    assert store.claim_request("old", "subject", "hash")[0] == "claimed"
    store.complete_request("old", {"answer": "old binary"})
    assert store.request_result("old", "subject")["answer"] == "old binary"
    assert store.consume_limits("subject", [("old_write", "hour", 2)])["old_write"] == 1


def test_new_worker_crash_then_old_app_reads_reconciled_terminal_result(database):
    old = old_store_module()
    migrate(database)
    new_store = PublicStore("", engine=database)
    old_store = old.PublicStore("", engine=database)
    now = datetime(2026, 9, 7, tzinfo=UTC)
    with patch("backend.snow_app.public_store._utcnow", return_value=now):
        assert new_store.claim_request("crashed", "subject", "hash", owner_token="dead")[0] == "claimed"
    later = now + timedelta(minutes=11)
    with patch.object(old, "_utcnow", return_value=later), patch("backend.snow_app.public_store._utcnow", return_value=later):
        # The actual baseline cannot reconcile a new worker's abandoned lease.
        assert old_store.claim_request("crashed", "subject", "hash")[0] == "processing"
        assert new_store.recover_expired_requests() == 1
        assert new_store.recover_expired_requests() == 0
        status, result = old_store.claim_request("crashed", "subject", "hash")
        assert status == "completed"
        assert result["terminal_error"] == "generation_interrupted"
        # Ordinary old-code requests remain writable after rollback.
        assert old_store.claim_request("old-after-rollback", "subject", "hash")[0] == "claimed"
        old_store.complete_request("old-after-rollback", {"answer": "old works"})


def test_parallel_claim_and_limits_are_atomic(database):
    migrate(database)
    store = PublicStore("", engine=database)

    def claim(index):
        return store.claim_request("one", "subject", "hash", owner_token=f"worker-{index}")[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(claim, range(8)))
    assert statuses.count("claimed") == 1
    assert statuses.count("processing") == 7

    def consume(_index):
        try:
            store.consume_limits("limited", [("hourly", "hour", 3)])
            return True
        except RateLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(8))) == 3


def test_subscription_pair_poll_result_and_connector_limits_use_postgres(database):
    from fastapi.testclient import TestClient

    from backend.snow_app.config import Settings
    from backend.snow_app.public_main import _anonymous_cookie_name, create_app
    from backend.snow_app.public_security import open_byok_credential
    from backend.snow_app.public_subscription import MODEL, PROVIDER
    from tests.test_public_api import _settings

    migrate(database)
    store = PublicStore("", engine=database)
    settings = replace(_settings(), subscription_enabled=True, auto_create_schema=False)
    app = create_app(settings, Settings.from_environment(), store)
    broker = app.state.subscription_broker
    origin = {"Origin": "https://snow.xiaob.dev"}
    notices = {"accepted_transit_notice": True, "accepted_cost_notice": True,
               "accepted_local_history_notice": True}
    catalogue = [{"id": MODEL, "display_name": "Synthetic model", "reasoning_efforts": ["max"],
                  "default_reasoning_effort": "max"}]

    def counters():
        with database.connect() as connection:
            return {(row.subject_hash, row.scope): row.count for row in connection.execute(text(
                "SELECT subject_hash, scope, count FROM public_rate_limit"
            ))}

    # Both sessions use the same HTTP client address, but each authenticated
    # connector must receive its own allowance. All jobs/results are synthetic;
    # no provider or local Codex process is invoked.
    with TestClient(app) as client:
        completed = []
        for _ in range(2):
            client.cookies.clear()
            paired = client.post("/public/v1/subscription/pair", headers=origin, json=notices)
            assert paired.status_code == 200
            assert client.get("/public/v1/subscription/status").json()["status"] == "waiting"
            connected = client.post("/public/v1/subscription/connector/connect", headers=origin,
                                    json={"pairing_code": paired.json()["pairing_code"],
                                          "protocol_version": 2, "models": catalogue})
            assert connected.status_code == 200
            assert client.get("/public/v1/subscription/status").json()["status"] == "connected"
            token = connected.json()["connector_token"]
            relay = open_byok_credential(settings,
                anonymous_id=client.cookies.get(_anonymous_cookie_name(settings)),
                token=paired.json()["credential"], expected_provider=PROVIDER)["api_key"]
            connection, job = broker._new_job(relay, {
                "model": MODEL, "reasoning_effort": "max",
                "messages": [{"role": "user", "content": "synthetic PostgreSQL relay check"}],
            }, 30)
            polled = client.post("/public/v1/subscription/connector/poll", headers=origin,
                                 json={"connector_token": token})
            assert polled.status_code == 200
            assert polled.json()["job"]["job_id"] == job.job_id
            result = {"connector_token": token, "job_id": job.job_id,
                      "content": "synthetic reply", "usage": {"total_tokens": 1}}
            delivered = client.post("/public/v1/subscription/connector/result", headers=origin, json=result)
            assert delivered.status_code == 200 and delivered.json() == {"accepted": True}
            assert job.future.result(timeout=1)["choices"][0]["message"]["content"] == "synthetic reply"
            broker._release_completed(connection, job)
            completed.append(result)

        initial = counters()
        connector_rows = {key: count for key, count in initial.items() if key[1].startswith("subscription_connector_")}
        subjects = {key[0] for key in connector_rows}
        assert len(subjects) == 2 and all(re.fullmatch(r"[0-9a-f]{64}", value) for value in subjects)
        assert len(connector_rows) == 4 and set(connector_rows.values()) == {2}
        assert not any(scope.startswith("write_ip_") for _, scope in initial)

        # Locate the first token's bucket by observed behavior, then exhaust its
        # unchanged production hourly allowance without touching the other one.
        acknowledged = client.post("/public/v1/subscription/connector/result", headers=origin, json=completed[0])
        assert acknowledged.status_code == 200
        before_limit = counters()
        limited_subject = next(subject for subject in subjects
                               if before_limit[(subject, "subscription_connector_hour")] == 3)
        with database.begin() as connection:
            connection.execute(text("UPDATE public_rate_limit SET count=1800 "
                                    "WHERE subject_hash=:subject AND scope='subscription_connector_hour'"),
                               {"subject": limited_subject})
        exhausted = counters()
        rejected = client.post("/public/v1/subscription/connector/result", headers=origin, json=completed[0])
        assert rejected.status_code == 429
        assert rejected.json()["detail"] == {"code": "rate_limit_exceeded",
                                              "scope": "subscription_connector_hour", "limit": 1800}
        assert rejected.headers["Retry-After"] == "60" and counters() == exhausted
        other = client.post("/public/v1/subscription/connector/result", headers=origin, json=completed[1])
        assert other.status_code == 200
        after = counters()
        other_subject = (subjects - {limited_subject}).pop()
        for scope in ("subscription_connector_hour", "subscription_connector_day"):
            assert after[(other_subject, scope)] == 3
        assert all(after[key] == value for key, value in exhausted.items()
                   if key[0] != other_subject or not key[1].startswith("subscription_connector_"))
        invalid = client.post("/public/v1/subscription/connector/poll", headers=origin,
                              json={"connector_token": "A" * 43})
        assert invalid.status_code == 401 and counters() == after


def test_expired_owner_is_fenced_and_never_reissues_provider_work(database):
    migrate(database)
    store = PublicStore("", engine=database)
    now = datetime(2026, 9, 7, tzinfo=UTC)
    with patch("backend.snow_app.public_store._utcnow", return_value=now):
        assert store.claim_request("one", "subject", "hash", owner_token="old")[0] == "claimed"
    with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=46)):
        status, response = store.claim_request("one", "subject", "hash", owner_token="new")
        assert status == "completed"
        assert response["terminal_error"] == "generation_interrupted"
        with pytest.raises(PublicStoreUnavailable):
            store.complete_request("one", {"answer": "late"}, owner_token="old")


def test_renewal_targets_only_live_request_ids_with_matching_owner(database):
    migrate(database)
    store = PublicStore("", engine=database)
    now = datetime(2026, 9, 7, tzinfo=UTC)
    with patch("backend.snow_app.public_store._utcnow", return_value=now):
        for request_id, owner in (("live", "worker"), ("orphan", "worker"), ("foreign", "other")):
            assert store.claim_request(request_id, "subject", "hash", owner_token=owner)[0] == "claimed"
    for seconds in (10, 20, 30, 40, 50):
        with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=seconds)):
            assert store.renew_leases("worker", ("live", "foreign")) == 1
            assert store.renew_leases("worker", ()) == 0
    with patch("backend.snow_app.public_store._utcnow", return_value=now + timedelta(seconds=51)):
        for request_id in ("orphan", "foreign"):
            status, result = store.claim_request(request_id, "subject", "hash", owner_token="new")
            assert status == "completed"
            assert result["terminal_error"] == "generation_interrupted"
        store.complete_request("live", {"answer": "done"}, owner_token="worker")


def test_restricted_application_role_can_write_data_but_cannot_change_schema(database):
    """Prove the proposed runtime grants independently of the migration owner."""
    migrate(database)
    role = "snow_role_" + uuid4().hex
    with database.begin() as connection:
        schema = connection.execute(text("SELECT current_schema()")).scalar_one()
        tables = [name for name in inspect(connection).get_table_names() if name.startswith("public_")]
        qualified_tables = ", ".join(f'"{schema}"."{name}"' for name in tables)
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        connection.execute(text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified_tables} TO "{role}"'))
    try:
        with database.begin() as connection:
            connection.execute(text(f'SET LOCAL ROLE "{role}"'))
            for table in tables:
                connection.execute(text(f'SELECT 1 FROM "{table}" LIMIT 0'))
                connection.execute(text(f'DELETE FROM "{table}" WHERE false'))
            connection.execute(
                text("""INSERT INTO public_rate_limit
                (subject_hash, scope, bucket_start, count, expires_at)
                VALUES ('role-test', 'test', 'test', 1, now() + interval '1 hour')""")
            )
            for forbidden in (
                "CREATE TABLE forbidden_schema_change (id INTEGER)",
                "DROP TABLE public_request_leases",
                "UPDATE alembic_version SET version_num = 'forged'",
            ):
                with pytest.raises(DBAPIError):
                    with connection.begin_nested():
                        connection.execute(text(forbidden))
    finally:
        with database.begin() as connection:
            connection.execute(text(f'REVOKE ALL ON {qualified_tables} FROM "{role}"'))
            connection.execute(text(f'REVOKE ALL ON SCHEMA "{schema}" FROM "{role}"'))
            connection.execute(text(f'DROP ROLE "{role}"'))


def test_failed_migration_rolls_back_ddl_and_revision_atomically(database, tmp_path):
    migrate(database)
    # An intentionally failing child revision lives only in the test directory.
    (tmp_path / "failed_revision.py").write_text(
        "from alembic import op\n"
        "revision = 'synthetic_failure'\n"
        "down_revision = '20260907_0005'\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade():\n"
        "    op.execute('CREATE TABLE failed_migration_probe (id INTEGER)')\n"
        "    op.execute('SELECT 1 / 0')\n"
        "def downgrade():\n"
        "    pass\n",
        encoding="utf-8",
    )
    config = Config(str(APP / "alembic.ini"))
    config.set_main_option("script_location", str(APP / "migrations"))
    config.set_main_option("path_separator", "os")
    config.set_main_option(
        "version_locations", os.pathsep.join((str(APP / "migrations" / "versions"), str(tmp_path)))
    )
    with pytest.raises(DBAPIError):
        with database.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    with database.connect() as connection:
        assert "failed_migration_probe" not in inspect(connection).get_table_names()
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "20260907_0005"
        )
    assert PublicStore("", engine=database).claim_request("after-failure", "subject", "hash")[0] == "claimed"
