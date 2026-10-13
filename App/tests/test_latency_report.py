from scripts.latency_report import summarize
from backend.snow_app.public_delivery import latency_metrics


def sample(**overrides):
    return {"provider": "deepseek", "model_profile": "deepseek-flash", "channel": "text",
            "history_band": "short", "outcome": "success", "send_to_first_effective_ms": 4200,
            "ready_to_paint_ms": 24, "natural_wait_ms": 0, "complete_ms": 9000, **overrides}


def test_small_samples_never_claim_acceptance():
    report = summarize([sample()])
    assert all(group["acceptance"] == "insufficient_samples" for group in report["groups"])


def test_failures_and_missing_values_remain_in_denominator():
    rows = [sample() for _ in range(27)] + [sample(outcome="failed", send_to_first_effective_ms=None) for _ in range(3)]
    overall = next(group for group in summarize(rows)["groups"] if group["group"] == ["all"])
    assert overall["first_effective_p95_ms"] == 4200
    assert overall["successful_within_5s_rate"] == .9
    assert overall["missing_first_content"] == 3
    assert overall["failed"] == 3
    assert overall["acceptance"] == "fail"


def test_unknown_private_ids_and_fields_are_not_reported():
    report = summarize([sample(provider="private-provider", model_profile="private-model", body="private-chat")])
    assert "private-" not in str(report)
    assert any(group["group"] == ["model", "other", "other"] for group in report["groups"])


def test_slow_and_partial_results_do_not_pass():
    rows = [sample(send_to_first_effective_ms=6000) for _ in range(28)] + [sample(outcome="partial") for _ in range(2)]
    overall = next(group for group in summarize(rows)["groups"] if group["group"] == ["all"])
    assert overall["over_5s"] == 28
    assert overall["partial"] == 2
    assert overall["acceptance"] == "fail"


def test_nonfinite_boolean_and_negative_metrics_are_rejected():
    assert latency_metrics({"timings_ms": {"total": float("nan"), "queue_ms": True,
                           "validation_ms": -1, "raw_prompt": "private", "initial_model_ms": 123.5}}) == {"initial_model_ms": 123}


def test_medium_histories_remain_a_separate_scenario():
    groups = summarize([sample(history_band="medium")])["groups"]
    assert any(group["group"][:6] == ["scenario", "deepseek", "deepseek-flash", "text", "medium", "queue_unknown"]
               for group in groups)


def test_fast_cached_recovery_cannot_hide_original_generation_failures():
    rows = [sample() for _ in range(27)] + [sample(outcome="failed", send_to_first_effective_ms=None) for _ in range(3)]
    rows += [sample(recovery=True, send_to_first_effective_ms=20) for _ in range(100)]
    overall = next(group for group in summarize(rows)["groups"] if group["group"] == ["all"])
    assert overall["samples"] == 130
    assert overall["generation_samples"] == 30
    assert overall["recovery_samples"] == 100
    assert overall["first_effective_p95_ms"] == 4200
    assert overall["recovery_first_effective_p95_ms"] == 20
    assert overall["successful_within_5s_rate"] == .9
    assert overall["acceptance"] == "fail"
