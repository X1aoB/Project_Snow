"""Run reproducible, non-destructive relation-extraction model benchmarks.

Benchmark output is isolated below App/runtime/benchmarks. It never changes the
canonical review jobs, relation candidates, or verified graph.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .common import RUNTIME_ROOT, load_runtime_jsonl, read_jsonl, utc_now, write_json, write_jsonl
from .extract_relation_candidates import (
    _call_provider,
    _known_character_names,
    build_relation_prompt,
    load_relation_environment,
    relation_provider_settings,
    validate_relation_candidate,
)


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
JOBS_PATH = RUNTIME_ROOT / "review" / "narrative_relation_jobs.jsonl"
CANDIDATES_PATH = RUNTIME_ROOT / "review" / "narrative_relation_candidates.jsonl"


def _safe_name(value: str, label: str) -> str:
    if not SAFE_NAME.fullmatch(value):
        raise ValueError(f"{label} must contain only letters, digits, underscores, or hyphens.")
    return value


def _benchmark_root(sample_id: str) -> Path:
    return RUNTIME_ROOT / "benchmarks" / "relation_extraction" / _safe_name(sample_id, "sample ID")


def _balanced_sample(jobs: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_source: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for job in jobs:
        by_source[job.get("source_type", "unknown")].append(job)
    for source_jobs in by_source.values():
        source_jobs.sort(key=lambda job: job["job_id"])

    selected: list[dict[str, Any]] = []
    while len(selected) < limit:
        progressed = False
        for source_type in sorted(by_source):
            if not by_source[source_type] or len(selected) >= limit:
                continue
            selected.append(by_source[source_type].pop(0))
            progressed = True
        if not progressed:
            break
    return selected


def _completed_before(jobs: list[dict[str, Any]], completed_before: str | None) -> list[dict[str, Any]]:
    if not completed_before:
        return jobs
    return [
        job
        for job in jobs
        if job.get("status") == "completed" and str(job.get("completed_at", "")) < completed_before
    ]


def load_or_create_sample(sample_id: str, limit: int, completed_before: str | None = None) -> list[dict[str, Any]]:
    root = _benchmark_root(sample_id)
    path = root / "sample.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        selection = payload.get("selection") or {}
        existing_cutoff = selection.get("completed_before") if isinstance(selection, dict) else None
        if existing_cutoff != completed_before:
            raise RuntimeError("Existing benchmark sample uses a different completed-before cutoff.")
        return payload["jobs"]
    jobs = _completed_before(list(read_jsonl(JOBS_PATH)), completed_before)
    if not jobs:
        raise RuntimeError("Canonical relation review jobs are missing or empty.")
    sample = _balanced_sample(jobs, limit)
    if len(sample) < limit:
        raise RuntimeError(f"Only {len(sample)} review jobs are available; cannot create a {limit}-job benchmark.")
    write_json(
        path,
        {
            "sample_id": sample_id,
            "created_at": utc_now(),
            "selection": {
                "method": "round_robin_by_source_type_then_job_id",
                "completed_before": completed_before,
            },
            "jobs": sample,
        },
    )
    return sample


def _provider_settings() -> tuple[str, str, str, str]:
    provider, base_url, api_key, model = relation_provider_settings()
    if provider != "openai-compatible":
        raise RuntimeError("Set RELATION_CANDIDATE_PROVIDER=openai-compatible before running a benchmark.")
    if not all((base_url, api_key, model)):
        raise RuntimeError("Benchmark extraction requires DashScope/OpenAI-compatible base URL, API key, and model configuration.")
    return provider, base_url, api_key, model


def _summary_counts(candidates: list[dict[str, Any]]) -> tuple[collections.Counter[str], collections.Counter[str]]:
    return (
        collections.Counter(candidate.get("relation_type", "unknown") for candidate in candidates),
        collections.Counter(candidate.get("source_type", "unknown") for candidate in candidates),
    )


def materialize_canonical_baseline(sample_id: str, run_name: str, limit: int, completed_before: str) -> dict[str, Any]:
    """Write a read-only benchmark baseline from candidates produced before a provider cutover."""
    sample_id = _safe_name(sample_id, "sample ID")
    run_name = _safe_name(run_name, "run name")
    jobs = load_or_create_sample(sample_id, limit, completed_before)
    root = _benchmark_root(sample_id)
    candidates_path = root / f"{run_name}.candidates.jsonl"
    summary_path = root / f"{run_name}.summary.json"
    if candidates_path.exists() or summary_path.exists():
        raise RuntimeError(f"Benchmark run '{run_name}' already exists for sample '{sample_id}'. Choose a new run name.")
    sample_job_ids = {job["job_id"] for job in jobs}
    candidates = [
        candidate
        for candidate in read_jsonl(CANDIDATES_PATH)
        if candidate.get("job_id") in sample_job_ids and str(candidate.get("created_at", "")) < completed_before
    ]
    relation_counts, source_counts = _summary_counts(candidates)
    write_jsonl(candidates_path, candidates)
    summary = {
        "sample_id": sample_id,
        "run_name": run_name,
        "generated_at": utc_now(),
        "provider": "legacy_proxy_baseline",
        "model": "qwen3.7-max",
        "completed_before": completed_before,
        "jobs": len(jobs),
        "successful_jobs": len(jobs),
        "failed_jobs": 0,
        "candidate_count": len(candidates),
        "candidates_per_successful_job": round(len(candidates) / max(1, len(jobs)), 3),
        "mean_seconds_per_job": None,
        "usage_tokens": {},
        "filtered_relation_counts": {},
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "source_type_candidate_counts": dict(sorted(source_counts.items())),
        "errors": [],
        "output": str(candidates_path),
        "policy": "Baseline is copied from canonical pre-cutover candidates and never changes canonical artifacts.",
    }
    write_json(summary_path, summary)
    return summary


def run(sample_id: str, run_name: str, limit: int, completed_before: str | None = None) -> dict[str, Any]:
    sample_id = _safe_name(sample_id, "sample ID")
    run_name = _safe_name(run_name, "run name")
    provider, base_url, api_key, model = _provider_settings()
    jobs = load_or_create_sample(sample_id, limit, completed_before)
    root = _benchmark_root(sample_id)
    candidates_path = root / f"{run_name}.candidates.jsonl"
    summary_path = root / f"{run_name}.summary.json"
    if candidates_path.exists() or summary_path.exists():
        raise RuntimeError(f"Benchmark run '{run_name}' already exists for sample '{sample_id}'. Choose a new run name.")

    documents = {document["document_id"]: document for document in load_runtime_jsonl("documents.jsonl")}
    known_character_names = _known_character_names(documents)
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    durations: list[float] = []
    usage_totals: collections.Counter[str] = collections.Counter()
    filtered_relation_counts: collections.Counter[str] = collections.Counter()
    for job in jobs:
        started = time.perf_counter()
        try:
            response, usage = _call_provider(base_url, api_key, model, build_relation_prompt(job, documents), include_usage=True)
            durations.append(time.perf_counter() - started)
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(field)
                if isinstance(value, (int, float)):
                    usage_totals[field] += int(value)
            for ordinal, relation in enumerate(response.get("relations", [])):
                candidate, rejection_reason = validate_relation_candidate(relation, job, documents, known_character_names)
                if candidate is None:
                    filtered_relation_counts[rejection_reason or "invalid_candidate"] += 1
                    continue
                candidates.append(
                    {
                        "candidate_id": f"benchmark_{run_name}_{job['job_id']}_{ordinal}",
                        "job_id": job["job_id"],
                        "page_id": job["page_id"],
                        "source_type": job["source_type"],
                        **candidate,
                        "benchmark_status": "unreviewed",
                        "extractor": {"provider": provider, "model": model},
                        "created_at": utc_now(),
                    }
                )
        except Exception as exc:  # Records provider incompatibility without stopping the benchmark sample.
            durations.append(time.perf_counter() - started)
            errors.append({"job_id": job["job_id"], "source_type": job["source_type"], "error": str(exc)})

    write_jsonl(candidates_path, candidates)
    relation_counts, source_counts = _summary_counts(candidates)
    summary = {
        "sample_id": sample_id,
        "run_name": run_name,
        "generated_at": utc_now(),
        "provider": provider,
        "model": model,
        "jobs": len(jobs),
        "successful_jobs": len(jobs) - len(errors),
        "failed_jobs": len(errors),
        "candidate_count": len(candidates),
        "candidates_per_successful_job": round(len(candidates) / max(1, len(jobs) - len(errors)), 3),
        "mean_seconds_per_job": round(sum(durations) / max(1, len(durations)), 3),
        "usage_tokens": dict(sorted(usage_totals.items())),
        "filtered_relation_counts": dict(sorted(filtered_relation_counts.items())),
        "relation_type_counts": dict(sorted(relation_counts.items())),
        "source_type_candidate_counts": dict(sorted(source_counts.items())),
        "errors": errors,
        "output": str(candidates_path),
        "policy": "Benchmark results are isolated and do not alter canonical relation review or graph artifacts.",
    }
    write_json(summary_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-id", default="relation-extraction-10")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--completed-before", help="Only sample jobs completed before this ISO-8601 UTC timestamp.")
    parser.add_argument("--materialize-canonical-baseline", action="store_true")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 100:
        raise ValueError("Benchmark limit must be between 1 and 100.")
    if args.materialize_canonical_baseline:
        if not args.completed_before:
            raise ValueError("--materialize-canonical-baseline requires --completed-before.")
        result = materialize_canonical_baseline(args.sample_id, args.run_name, args.limit, args.completed_before)
    else:
        result = run(args.sample_id, args.run_name, args.limit, args.completed_before)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
