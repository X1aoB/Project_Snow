"""Synthetic process-level regressions: never open the user's review runtime."""

from __future__ import annotations

import multiprocessing
from types import SimpleNamespace

import pytest

from backend.snow_app.repository import RuntimeRepository
from backend.snow_app.review_lock import ReviewConflict, review_lock
from pipelines.common import read_jsonl, write_jsonl
from pipelines.review_state import append_review_reports, checkpoint_extraction, preserve_review_jobs


def _human_job_writer(path, ready, release):
    with review_lock(path):
        rows = list(read_jsonl(path))
        ready.set()
        if not release.wait(10):
            raise RuntimeError("Test barrier expired")
        rows[0].update(status="completed_no_relation", resolved_by="human")
        write_jsonl(path, rows)


def _rebuild(path, started, finished):
    started.set()
    preserve_review_jobs(path, [{"job_id": "j", "evidence_document_ids": ["doc"], "status": "queued"}])
    finished.set()


def _append(path, index):
    append_review_reports(path, [{"report_key": str(index), "verdict": "abstain"}])


def _decide(runtime, candidate_id):
    repository = RuntimeRepository(SimpleNamespace(runtime_root=runtime))
    repository.decide_relation_candidate(candidate_id, "rejected", "human", "Synthetic evidence review")


def _hold_lock(path, ready):
    with review_lock(path):
        ready.set()
        # The parent terminates this synthetic worker to simulate a crash.
        multiprocessing.Event().wait(30)


def _join(process):
    process.join(10)
    assert not process.is_alive()
    assert process.exitcode == 0


def test_builder_waits_for_another_process_and_preserves_human_resolution(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "review" / "jobs.jsonl"
    write_jsonl(path, [{"job_id": "j", "evidence_document_ids": ["doc"], "status": "queued"}])
    ready, release, started, finished = (context.Event() for _ in range(4))
    human = context.Process(target=_human_job_writer, args=(path, ready, release))
    builder = context.Process(target=_rebuild, args=(path, started, finished))
    human.start()
    try:
        assert ready.wait(10)
        builder.start()
        assert started.wait(10)
        assert not finished.wait(0.15), "Rebuild entered a human transaction from another process"
        release.set()
        _join(human)
        _join(builder)
    finally:
        release.set()
        for process in (human, builder):
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert list(read_jsonl(path))[0]["resolved_by"] == "human"


def test_append_reports_retains_every_independent_process(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "review" / "reports.jsonl"
    workers = [context.Process(target=_append, args=(path, index)) for index in range(4)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            _join(worker)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    assert {row["report_key"] for row in read_jsonl(path)} == {"0", "1", "2", "3"}


def test_lock_times_out_and_is_released_when_owner_process_crashes(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "review" / "jobs.jsonl"
    ready = context.Event()
    owner = context.Process(target=_hold_lock, args=(path, ready))
    owner.start()
    try:
        assert ready.wait(10)
        with pytest.raises(ReviewConflict, match="busy"):
            with review_lock(path, timeout=0.05):
                pytest.fail("Acquired another process's lock")
    finally:
        owner.terminate()
        owner.join(5)
    with review_lock(path, timeout=0.5):
        # Nested writers share the lock inode, including review/persona paths.
        write_jsonl(tmp_path / "personas" / "profiles.jsonl", [])


def test_real_repository_decisions_from_two_processes_preserve_both_events(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "review" / "narrative_relation_candidates.jsonl"
    write_jsonl(path, [
        {"candidate_id": key, "subject": "A", "object": "B", "relation_type": "ALLY_OF",
         "review_status": "pending_review"} for key in ("a", "b")
    ])
    workers = [context.Process(target=_decide, args=(tmp_path, key)) for key in ("a", "b")]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            _join(worker)
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
    assert all(row["review_status"] == "rejected" for row in read_jsonl(path))
    events = list(read_jsonl(path.with_name("relation_review_events.jsonl")))
    assert {row["candidate_id"] for row in events} == {"a", "b"}


def test_extractor_merges_new_candidates_without_reverting_human_review(tmp_path):
    jobs_path = tmp_path / "review" / "jobs.jsonl"
    candidates_path = tmp_path / "review" / "candidates.jsonl"
    original = {"job_id": "j", "status": "queued", "evidence_document_ids": ["doc"]}
    other = {"job_id": "other", "status": "completed_no_relation", "reviewer": "human"}
    reviewed = {"candidate_id": "same", "review_status": "approved", "reviewer": "human"}
    write_jsonl(jobs_path, [original, other])
    write_jsonl(candidates_path, [reviewed])
    checkpoint_extraction(jobs_path, candidates_path, original, {**original, "status": "completed"}, [
        {"candidate_id": "same", "review_status": "pending_review"},
        {"candidate_id": "new", "review_status": "pending_review"},
    ])
    assert list(read_jsonl(candidates_path)) == [
        reviewed, {"candidate_id": "new", "review_status": "pending_review"}
    ]
    assert list(read_jsonl(jobs_path))[1] == other


def test_extractor_preserves_paid_result_and_refuses_changed_job(tmp_path):
    jobs_path = tmp_path / "review" / "jobs.jsonl"
    candidates_path = tmp_path / "review" / "candidates.jsonl"
    original = {"job_id": "j", "status": "queued", "evidence_document_ids": ["doc"]}
    human = {**original, "status": "completed_no_relation", "resolved_by": "human"}
    write_jsonl(jobs_path, [human])
    write_jsonl(candidates_path, [])
    with pytest.raises(ReviewConflict, match="result saved"):
        checkpoint_extraction(jobs_path, candidates_path, original, {**original, "status": "completed"}, [
            {"candidate_id": "paid-output", "review_status": "pending_review"},
        ])
    assert list(read_jsonl(jobs_path)) == [human]
    assert list(read_jsonl(candidates_path)) == []
    conflict = list(read_jsonl(candidates_path.with_name("extraction_conflicts.jsonl")))[0]
    assert conflict["candidates"][0]["candidate_id"] == "paid-output"
