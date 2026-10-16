"""Money/recovery invariants for the opt-in manual provider probe."""
from decimal import Decimal
import pytest
from scripts.paid_probe_budget import BudgetBlocked, PaidProbeBudget


def test_unresolved_call_blocks_even_after_process_restart(tmp_path):
    path = tmp_path / "budget.json"
    ledger = PaidProbeBudget(path)
    call = ledger.reserve(case="arrival", request_id="test", prompt_bound=1000, output_bound=1000)
    restarted = PaidProbeBudget(path)
    with pytest.raises(BudgetBlocked, match="unresolved"):
        restarted.reserve(case="other", request_id="other", prompt_bound=1, output_bound=1)
    with pytest.raises(BudgetBlocked, match="usage"):
        restarted.confirm(call, {})
    assert restarted.read()["calls"][0]["status"] == "reserved"
    restarted.confirm(call, {"prompt_tokens": 100, "completion_tokens": 100})
    assert Decimal(restarted.read()["calls"][0]["cost_ceiling_cny"]) == Decimal("0.001")


def test_cumulative_limit_includes_proposed_worst_case(tmp_path):
    ledger = PaidProbeBudget(tmp_path / "budget.json", cap_cny="0.01")
    call = ledger.reserve(case="one", request_id="one", prompt_bound=1000, output_bound=1000)
    ledger.confirm(call, {"prompt_tokens": 1000, "completion_tokens": 1000})
    with pytest.raises(BudgetBlocked, match="cap"):
        ledger.reserve(case="two", request_id="two", prompt_bound=1, output_bound=1)


def test_tariff_change_or_excess_usage_cannot_release_reservation(tmp_path):
    path = tmp_path / "budget.json"
    ledger = PaidProbeBudget(path)
    call = ledger.reserve(case="one", request_id="one", prompt_bound=10, output_bound=10)
    with pytest.raises(BudgetBlocked, match="review"):
        ledger.confirm(call, {"prompt_tokens": 11, "completion_tokens": 10})
    with pytest.raises(BudgetBlocked, match="tariff"):
        PaidProbeBudget(path, input_per_million="3").read()
    assert ledger.read()["calls"][0]["status"] == "reserved"
