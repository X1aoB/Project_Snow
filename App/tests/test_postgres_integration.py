"""Real PostgreSQL contract checks; never fall back to SQLite.

Each test creates a random schema in the explicitly supplied TEST_POSTGRES_URL
database. No production secret discovery or user runtime data is used.
"""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
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


def test_real_old_store_remains_usable_after_additive_migration(database):
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
    migrate(database)
    store = old.PublicStore("", engine=database)
    assert store.claim_request("old", "subject", "hash")[0] == "claimed"
    store.complete_request("old", {"answer": "old binary"})
    assert store.request_result("old", "subject")["answer"] == "old binary"
    assert store.consume_limits("subject", [("old_write", "hour", 2)])["old_write"] == 1


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
