import json

import pytest

from scripts.latency_report import SERVER_TIMING_NAMES
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


def overall(rows):
    return next(group for group in summarize(rows)["groups"] if group["group"] == ["all"])


def test_stage_statistics_keep_missing_unmeasured_and_use_nearest_rank():
    group = overall([
        sample(server_timings_ms={"queue_ms": 0, "context_build_ms": 1000}),
        sample(server_timings_ms={"queue_ms": 300, "context_build_ms": 6000}),
        sample(outcome="failed", send_to_first_effective_ms=None),
    ])
    assert set(group["server_stages_ms"]) == set(SERVER_TIMING_NAMES)
    assert group["server_stages_ms"]["context_build_ms"] == {
        "measured": 2, "missing": 1, "p50_ms": 1000, "p95_ms": 6000, "over_5s": 1,
    }
    assert group["server_stages_ms"]["queue_ms"]["measured"] == 2
    assert group["server_stages_ms"]["durable_commit_ms"] == {
        "measured": 0, "missing": 3, "p50_ms": None, "p95_ms": None, "over_5s": 0,
    }
    assert group["missing_first_content"] == 1


def test_later_generation_validation_and_persistence_do_not_blame_early_first_reply():
    rows = [sample(server_timings_ms={"first_committed_block_ms": 4000,
                    "initial_model_ms": 9000, "validation_total_ms": 6000,
                    "durable_commit_ms": 7000, "total": 20000}) for _ in range(30)]
    group = overall(rows)
    assert group["acceptance"] == "pass"
    assert group["server_stages_ms"]["initial_model_ms"]["over_5s"] == 30
    assert group["over_5s_observations"]["samples"] == 0
    assert all(count == 0 for count in group["over_5s_observations"]["later_metrics_over_5s"].values())


def test_slow_reply_observations_never_add_overlapping_context_and_retrieval():
    rows = [sample(send_to_first_effective_ms=5500, server_timings_ms={
        "context_build_ms": 3000, "retrieval_ms": 2500, "prompt_build_ms": 1200,
        "initial_model_ms": 9000, "validation_total_ms": 7000,
    }) for _ in range(30)]
    report = summarize(rows)
    group = next(group for group in report["groups"] if group["group"] == ["all"])
    observations = group["over_5s_observations"]
    assert observations["samples"] == 30
    assert all(count == 0 for count in observations["pre_reply_metrics_over_5s"].values())
    assert observations["without_pre_reply_overrun_observation"] == 30
    assert observations["later_metrics_over_5s"]["initial_model_ms"] == 30
    assert observations["later_metrics_over_5s"]["validation_total_ms"] == 30
    assert "not causal attribution" in report["diagnostic_interpretation"]
    assert "Never add" in report["diagnostic_interpretation"]


def test_observed_slow_reply_budget_overruns_and_missing_diagnostics_are_separate():
    group = overall([
        sample(send_to_first_effective_ms=6000, ready_to_paint_ms=200, server_timings_ms={
            "queue_ms": 5100, "context_build_ms": 5000,
            "provider_first_content_ms": 5200, "first_committed_block_ms": 5800,
        }),
        sample(send_to_first_effective_ms=6000),
        sample(outcome="failed", send_to_first_effective_ms=None),
    ])
    observations = group["over_5s_observations"]
    assert observations["samples"] == 2
    assert observations["pre_reply_metrics_over_5s"]["queue_ms"] == 1
    assert observations["pre_reply_metrics_over_5s"]["context_build_ms"] == 0
    assert observations["pre_reply_metrics_over_5s"]["provider_first_content_ms"] == 1
    assert observations["pre_reply_metrics_over_5s"]["first_committed_block_ms"] == 1
    assert observations["missing_all_server_timings"] == 1
    assert observations["without_pre_reply_overrun_observation"] == 1
    assert observations["ready_to_paint_over_100ms"] == 1
    assert group["missing_first_content"] == 1


def test_replayed_server_metrics_do_not_pollute_new_generation_stage_percentiles():
    rows = [sample(server_timings_ms={"queue_ms": 300, "initial_model_ms": 6000}) for _ in range(27)]
    rows += [sample(outcome="failed", send_to_first_effective_ms=None) for _ in range(3)]
    rows += [sample(recovery=True, send_to_first_effective_ms=20, server_timings_ms={
        "queue_ms": 300000, "initial_model_ms": 3600000,
    }) for _ in range(100)]
    group = overall(rows)
    assert group["acceptance"] == "fail"
    assert group["server_stages_ms"]["queue_ms"] == {
        "measured": 27, "missing": 3, "p50_ms": 300, "p95_ms": 300, "over_5s": 0,
    }
    assert group["server_stages_ms"]["initial_model_ms"]["p95_ms"] == 6000
    assert group["over_5s_observations"]["samples"] == 0


def test_unknown_models_never_receive_verified_per_model_acceptance():
    for provider in ("deepseek", "synthetic-private-provider"):
        groups = summarize([sample(provider=provider, model_profile="synthetic-private-model") for _ in range(30)])["groups"]
        assert all(group["acceptance"] == "unverified_model" for group in groups if group["group"][0] != "all")
        assert "synthetic-private" not in json.dumps(groups)
    known = summarize([sample() for _ in range(30)])
    assert all(group["acceptance"] == "pass" for group in known["groups"])


def test_malformed_types_nonfinite_and_private_fields_cannot_enter_report():
    row = sample(provider={"synthetic-private-provider": 1}, model_profile=["synthetic-private-model"],
                 channel=[], history_band={}, outcome={"synthetic-private": True},
                 send_to_first_effective_ms=10**1000, ready_to_paint_ms=float("nan"),
                 server_timings_ms={
                     "queue_ms": True, "context_build_ms": float("inf"), "retrieval_ms": -1,
                     "initial_model_ms": 10**1000, "guard_rewrite_ms": "synthetic-private",
                     "total": {"synthetic-private": 1}, "provider_id": "synthetic-private-provider",
                 }, body="synthetic-private-chat")
    report = summarize([row])
    encoded = json.dumps(report, allow_nan=False)
    assert "synthetic-private" not in encoded
    group = next(group for group in report["groups"] if group["group"] == ["all"])
    assert group["unknown_outcome"] == group["missing_first_content"] == group["missing_paint"] == 1
    assert all(stage["measured"] == 0 and stage["missing"] == 1 for stage in group["server_stages_ms"].values())


def test_legacy_flat_exports_remain_measurable_when_new_keys_are_absent():
    for overrides in ({}, {"server_timings_ms": {}}):
        report = summarize([sample(server_queue_ms=150, rewrite_ms=2000, **overrides)])
        group = next(group for group in report["groups"] if group["group"] == ["all"])
        assert group["server_stages_ms"]["queue_ms"]["p95_ms"] == 150
        assert group["server_stages_ms"]["guard_rewrite_ms"]["p95_ms"] == 2000
        assert any(item["group"][-3:-1] == ["queued", "rewritten"] for item in report["groups"] if item["group"][0] == "scenario")


@pytest.mark.parametrize("nested", [None, [], True, {"queue_ms": None, "guard_rewrite_ms": True},
                                     {"queue_ms": float("nan"), "guard_rewrite_ms": -1}])
def test_invalid_present_nested_values_remain_unmeasured(nested):
    group = overall([sample(server_timings_ms=nested, server_queue_ms=1, rewrite_ms=2)])
    assert group["server_stages_ms"]["queue_ms"]["measured"] == 0
    assert group["server_stages_ms"]["guard_rewrite_ms"]["measured"] == 0


def test_nested_values_take_precedence_and_match_the_public_whitelist():
    group = overall([sample(server_timings_ms={"queue_ms": 6000, "guard_rewrite_ms": 8000},
                            server_queue_ms=1, rewrite_ms=2)])
    assert group["server_stages_ms"]["queue_ms"]["p95_ms"] == 6000
    assert group["server_stages_ms"]["guard_rewrite_ms"]["p95_ms"] == 8000
    packet = {name: 123 for name in SERVER_TIMING_NAMES}
    packet["synthetic_private"] = 123
    assert set(latency_metrics({"timings_ms": packet})) == set(SERVER_TIMING_NAMES)
    assert len(SERVER_TIMING_NAMES) == 18


@pytest.mark.parametrize("minimum", [0, 1, 29, True, None, 30.0, float("nan")])
def test_minimum_sample_gate_cannot_be_lowered_or_use_invalid_types(minimum):
    with pytest.raises(ValueError):
        summarize([sample() for _ in range(30)], minimum_samples=minimum)
