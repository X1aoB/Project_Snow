"""Summarize opt-in browser timing exports without making provider requests.

Input: JSON array from window.__projectSnowPerformance.getSamples(), or JSONL.
Unknown fields are discarded. Failures and missing first-content observations
stay in the denominator; small samples never count as acceptance evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.snow_app.model_capabilities import public_model_profiles
from backend.snow_app.public_delivery import latency_metrics


SERVER_TIMING_NAMES = (
    "queue_ms", "context_build_ms", "retrieval_ms", "prompt_build_ms", "hybrid_total", "initial_model_ms",
    "provider_headers_ms", "provider_first_content_ms", "provider_first_event_ms", "guard_rewrite_ms",
    "empty_recovery_ms", "validation_ms", "validation_local_ms", "validation_total_ms",
    "first_verified_block_ms", "durable_commit_ms", "first_committed_block_ms", "total",
)
PRE_REPLY_TIMING_NAMES = (
    "queue_ms", "context_build_ms", "retrieval_ms", "prompt_build_ms", "hybrid_total",
    "provider_headers_ms", "provider_first_content_ms", "provider_first_event_ms",
    "first_verified_block_ms", "first_committed_block_ms",
)
LATER_TIMING_NAMES = tuple(name for name in SERVER_TIMING_NAMES if name not in PRE_REPLY_TIMING_NAMES)


def duration(value: Any) -> float | None:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return None
    if not 0 <= value <= 3_600_000:
        return None
    return float(value) if math.isfinite(value) else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 2)


def server_timings(row: dict[str, Any]) -> dict[str, int]:
    """Use the public numeric allowlist; old flat exports remain readable."""
    raw = row.get("server_timings_ms")
    measured = latency_metrics({"timings_ms": raw})
    legacy = latency_metrics({"timings_ms": {
        "queue_ms": row.get("server_queue_ms"), "guard_rewrite_ms": row.get("rewrite_ms"),
    }})
    for name, value in legacy.items():
        if "server_timings_ms" not in row or isinstance(raw, dict) and name not in raw:
            measured.setdefault(name, value)
    return measured


def stage_statistics(generations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Separate stage observations; nested durations must never be added."""
    result = {}
    for name in SERVER_TIMING_NAMES:
        measured = [sample["stages"][name] for sample in generations if name in sample["stages"]]
        result[name] = {
            "measured": len(measured), "missing": len(generations) - len(measured),
            "p50_ms": percentile(measured, .5), "p95_ms": percentile(measured, .95),
            "over_5s": sum(value > 5000 for value in measured),
        }
    return result


def slow_observations(generations: list[dict[str, Any]]) -> dict[str, Any]:
    """Count observations attached to slow first replies, without assigning causes."""
    slow = [sample for sample in generations if sample["first"] is not None and sample["first"] > 5000]
    def overruns(names):
        return {name: sum(sample["stages"].get(name, 0) > 5000 for sample in slow) for name in names}
    return {
        "samples": len(slow),
        "pre_reply_metrics_over_5s": overruns(PRE_REPLY_TIMING_NAMES),
        "later_metrics_over_5s": overruns(LATER_TIMING_NAMES),
        "ready_to_paint_over_100ms": sum(sample["paint"] is not None and sample["paint"] > 100 for sample in slow),
        "missing_all_server_timings": sum(not sample["stages"] for sample in slow),
        "without_pre_reply_overrun_observation": sum(
            not any(sample["stages"].get(name, 0) > 5000 for name in PRE_REPLY_TIMING_NAMES)
            for sample in slow
        ),
    }


def summarize(rows: list[dict[str, Any]], *, minimum_samples: int = 30) -> dict[str, Any]:
    if type(minimum_samples) is not int or minimum_samples < 30:
        raise ValueError("at least 30 generation samples are required for acceptance")
    profiles = public_model_profiles()
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    groups[("all",)] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("every sample must be an object")
        provider = row.get("provider")
        provider = provider if isinstance(provider, str) and provider in profiles else "other"
        model = row.get("model_profile")
        model = model if isinstance(model, str) and model in profiles.get(provider, []) else "other"
        channel = row.get("channel")
        channel = channel if isinstance(channel, str) and channel in {"text", "in_person"} else "unknown"
        history = row.get("history_band")
        history = history if isinstance(history, str) and history in {"short", "medium", "long"} else "unknown"
        stages = server_timings(row)
        queue = stages.get("queue_ms")
        rewrite = stages.get("guard_rewrite_ms")
        outcome = row.get("outcome")
        sample = {
            "recovery": row.get("recovery") is True,
            "first": duration(row.get("send_to_first_effective_ms")),
            "paint": duration(row.get("ready_to_paint_ms")),
            "natural": duration(row.get("natural_wait_ms")),
            "complete": duration(row.get("complete_ms")),
            "outcome": outcome if isinstance(outcome, str) and outcome in {"success", "partial", "failed"} else "unknown",
            "stages": stages,
        }
        groups[("all",)].append(sample)
        groups[("model", provider, model)].append(sample)
        groups[("scenario", provider, model, channel, history,
                "queue_unknown" if queue is None else "queued" if queue > 100 else "not_queued",
                "rewrite_unknown" if rewrite is None else "rewritten" if rewrite > 0 else "not_rewritten",
                "recovery" if sample["recovery"] else "generation")].append(sample)
    summaries = []
    for key, samples in sorted(groups.items()):
        generations = [sample for sample in samples if not sample["recovery"]]
        values = [sample["first"] for sample in generations if sample["first"] is not None]
        success_on_time = sum(sample["outcome"] == "success" and sample["first"] is not None
                              and sample["first"] <= 5000 for sample in generations)
        total = len(samples)
        generation_total = len(generations)
        p95 = percentile(values, .95)
        rate = success_on_time / generation_total if generation_total else 0
        model_verified = key[0] == "all" or key[1] in profiles and key[2] in profiles[key[1]]
        summaries.append({
            "group": list(key), "samples": total,
            "generation_samples": generation_total, "recovery_samples": total - generation_total,
            "missing_first_content": generation_total - len(values),
            "failed": sum(sample["outcome"] == "failed" for sample in samples),
            "partial": sum(sample["outcome"] == "partial" for sample in samples),
            "unknown_outcome": sum(sample["outcome"] == "unknown" for sample in samples),
            "over_5s": sum(value > 5000 for value in values),
            "successful_within_5s_rate": round(rate, 4),
            "first_effective_p50_ms": percentile(values, .5),
            "first_effective_p95_ms": p95,
            "recovery_first_effective_p95_ms": percentile([s["first"] for s in samples if s["recovery"] and s["first"] is not None], .95),
            "ready_to_paint_p95_ms": percentile([s["paint"] for s in samples if s["paint"] is not None], .95),
            "ready_to_paint_over_100ms": sum(s["paint"] is not None and s["paint"] > 100 for s in samples),
            "missing_paint": sum(s["paint"] is None for s in samples),
            "natural_wait_p95_ms": percentile([s["natural"] for s in samples if s["natural"] is not None], .95),
            "complete_p95_ms": percentile([s["complete"] for s in samples if s["complete"] is not None], .95),
            "server_stages_ms": stage_statistics(generations),
            "over_5s_observations": slow_observations(generations),
            "acceptance": "insufficient_samples" if generation_total < minimum_samples else
                          "unverified_model" if not model_verified else
                          "pass" if rate >= .95 and p95 is not None and p95 <= 5000 else "fail",
        })
    return {"schema": "snow-latency-report-1", "target_ms": 5000,
            "minimum_samples": minimum_samples,
            "percentiles": "nearest-rank; generation first-content values only; original missing/failed remain in success-rate denominator; recovery attempts are reported separately and cannot improve generation acceptance",
            "stage_statistics_scope": "Generation samples only; missing stages stay unmeasured. Replayed server diagnostics do not measure a new generation or recovery latency.",
            "diagnostic_interpretation": "Over-budget observations are not causal attribution. Never add nested or cumulative server timings. Full model generation, rewrite, validation and final persistence may continue after first delivery and cannot explain an already delivered prefix. First upstream content may be JSON metadata, not an effective reply. No pre-reply overrun observation means the reason is unverified, not that those stages were fast.",
            "groups": summaries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        raw = args.input.read_text(encoding="utf-8-sig").strip()
        rows = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not isinstance(rows, list):
            raise ValueError("expected an array")
        result = summarize(rows)
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Unable to read a valid timing export.\n")
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
