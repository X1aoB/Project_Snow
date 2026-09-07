"""Human review records outlive rebuildable pipeline outputs.

Existing jobs, candidates and decisions are never reset by a data build. A
changed evidence set gets a separate deterministic job while its predecessor
remains available for provenance and explicit review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.snow_app.review_lock import ReviewConflict, review_locked

from .common import read_jsonl, stable_id, write_jsonl


@review_locked(lambda path, generated: path)
def preserve_review_jobs(path: Path, generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous = list(read_jsonl(path)) if path.exists() else []
    by_id = {row["job_id"]: row for row in previous}
    if len(by_id) != len(previous):
        raise ValueError(f"Duplicate review job identifiers in {path.name}")
    for item in generated:
        item = dict(item)
        key = item["job_id"]
        old = by_id.get(key)
        if old:
            old_evidence = sorted(old.get("evidence_document_ids") or [])
            new_evidence = sorted(item.get("evidence_document_ids") or [])
            if old_evidence == new_evidence:
                continue
            item["predecessor_job_id"] = key
            item["job_id"] = stable_id(key, *new_evidence, prefix="review_revision_")
        by_id.setdefault(item["job_id"], item)
    merged = list(by_id.values())
    write_jsonl(path, merged)
    return merged


@review_locked(lambda path: path)
def initialize_candidates(path: Path) -> None:
    """Create an absent queue without ever replacing an existing review file."""
    try:
        with path.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        # Fail a corrupted build input rather than silently erasing decisions.
        list(read_jsonl(path))


@review_locked(lambda path, profiles: path)
def preserve_profile_reviews(path: Path, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous = list(read_jsonl(path)) if path.exists() else []
    by_id = {row["profile_id"]: row for row in previous}
    if len(by_id) != len(previous):
        raise ValueError("Duplicate persona profile identifiers")
    merged = []
    for fresh in profiles:
        old = by_id.pop(fresh["profile_id"], {})
        # Preserve annotations, including future review metadata unknown to this
        # version of the builder. Only source-derived fields are refreshed.
        combined = {**fresh, **old}
        for field in (
            "evidence",
            "source_counts",
            "character_name",
            "policy_version",
            "relationship_invariant",
        ):
            combined[field] = fresh[field]
        if old and old.get("evidence") != fresh["evidence"]:
            history = list(old.get("evidence_history") or [])
            evidence = old.get("evidence") or {}
            if evidence not in history:
                history.append(evidence)
            combined["evidence_history"] = history
            combined["evidence_changed_since_review"] = True
        merged.append(combined)
    # Missing source documents must not implicitly revoke an approved decision.
    merged.extend({**old, "source_missing_from_build": True} for old in by_id.values())
    write_jsonl(path, merged)
    return merged


@review_locked(lambda jobs_path, candidates_path, original_job, updated_job, candidates: jobs_path)
def checkpoint_extraction(
    jobs_path: Path,
    candidates_path: Path,
    original_job: dict[str, Any],
    updated_job: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> None:
    """Commit one model result without replacing concurrent human decisions."""
    jobs = list(read_jsonl(jobs_path))
    current = next((row for row in jobs if row.get("job_id") == original_job["job_id"]), None)
    if current != original_job:
        # Keep the paid result available for reconciliation, but never attach
        # it to a job whose evidence or human resolution changed during a call.
        conflict_path = candidates_path.with_name("extraction_conflicts.jsonl")
        conflicts = list(read_jsonl(conflict_path)) if conflict_path.exists() else []
        conflicts.append({"original_job": original_job, "result_job": updated_job, "candidates": candidates})
        write_jsonl(conflict_path, conflicts)
        raise ReviewConflict(
            "Extraction job changed during generation; result saved in extraction_conflicts.jsonl"
        )
    existing = list(read_jsonl(candidates_path)) if candidates_path.exists() else []
    by_id = {row["candidate_id"]: row for row in existing}
    for candidate in candidates:
        by_id.setdefault(candidate["candidate_id"], candidate)
    write_jsonl(candidates_path, list(by_id.values()))
    write_jsonl(jobs_path, [updated_job if row is current else row for row in jobs])


@review_locked(lambda path, rows: path)
def append_review_reports(path: Path, rows: list[dict[str, Any]]) -> None:
    """Reports are an append-only audit trail, including concurrent runs."""
    previous = list(read_jsonl(path)) if path.exists() else []
    write_jsonl(path, [*previous, *rows])
