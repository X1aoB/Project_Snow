from backend.snow_app.data_loader import _activate_neo4j_dataset
from pipelines.common import write_jsonl
from pipelines.review_state import initialize_candidates, preserve_profile_reviews, preserve_review_jobs


def test_rebuild_preserves_completed_jobs_and_candidates(tmp_path):
    path = tmp_path / "jobs.jsonl"
    old = {"job_id": "persona_a", "status": "completed", "evidence_document_ids": ["a"], "reviewer": "human"}
    write_jsonl(path, [old])
    fresh = {"job_id": "persona_a", "status": "queued", "evidence_document_ids": ["a"]}
    assert preserve_review_jobs(path, [fresh]) == [old]
    changed = {**fresh, "evidence_document_ids": ["b"]}
    result = preserve_review_jobs(path, [changed])
    assert result[0] == old and len(result) == 2
    assert result[1]["job_id"] != old["job_id"]
    assert preserve_review_jobs(path, [changed]) == result
    assert preserve_review_jobs(path, []) == result
    candidates = tmp_path / "candidates.jsonl"
    write_jsonl(candidates, [{"candidate_id": "approved", "review_status": "approved"}])
    original = candidates.read_bytes()
    initialize_candidates(candidates)
    assert candidates.read_bytes() == original


def test_profile_source_refresh_preserves_review_and_prior_evidence(tmp_path):
    path = tmp_path / "profiles.jsonl"
    old = {
        "profile_id": "p",
        "active_traits": ["approved-trait"],
        "review_status": "approved",
        "evidence": {"style": ["old"]},
    }
    write_jsonl(path, [old])
    fresh = {
        "profile_id": "p",
        "active_traits": [],
        "review_status": "evidence_ready",
        "evidence": {"style": ["new"]},
        "source_counts": {},
        "character_name": "a",
        "policy_version": "1",
        "relationship_invariant": {},
    }
    result = preserve_profile_reviews(path, [fresh])[0]
    assert result["active_traits"] == old["active_traits"]
    assert result["review_status"] == "approved"
    assert result["evidence_history"] == [old["evidence"]]
    assert preserve_profile_reviews(path, [fresh])[0] == result


def test_graph_activation_only_updates_pointer_and_never_removes_rollback_data():
    calls = []

    class Session:
        def run(self, query, **values):
            calls.append((query, values))
            return self

        def consume(self):
            return None

    assert _activate_neo4j_dataset(Session(), "v3", "v2") == "deferred"
    assert len(calls) == 1
    assert "DELETE" not in calls[0][0]
    assert calls[0][1]["version"] == "v3"
