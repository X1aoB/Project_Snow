"""Paid arrival HTTP-handler contracts against isolated, explicitly configured PG.

No provider calls and no production configuration discovery. Run in the existing
postgres-contracts tier; SQLite is never substituted for this test module.
"""
import asyncio
import os

import pytest

from tests.test_postgres_integration import database, migrate
from tests import test_presence_arrival_v2 as arrival_tests

pytestmark = pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL required")


@pytest.mark.parametrize("case_name", [
    "test_join_is_zero_model_and_repeat_or_recovery_only_replays",
    "test_concurrent_duplicate_does_not_start_a_second_generation",
    "test_lost_commit_ack_preserves_original_paid_result",
    "test_post_provider_failure_is_terminal_and_never_releases_claim",
    "test_persistent_write_outage_reconciles_expired_lease_without_rebilling",
    "test_completed_cache_expiry_cannot_resurrect_paid_arrival",
    "test_changed_business_body_conflicts_after_completed_request",
])
def test_real_postgres_arrival_claim_recovery(database, case_name):
    migrate(database)
    async def exercise():
        case = arrival_tests.ArrivalV2DurabilityTests(methodName=case_name)
        case.store_engine = database
        await case.asyncSetUp()
        try:
            await getattr(case, case_name)()
        finally:
            await case.asyncTearDown()
    asyncio.run(exercise())
