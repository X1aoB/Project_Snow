import hashlib
import json
from types import SimpleNamespace

import pytest

from scripts.verify_release_proof import CI_WORKFLOW, REPOSITORY, SCHEMA, validate_bindings, verify

SHA = "a" * 40


def fixture():
    manifest = json.dumps({"commit_sha": SHA, "application": {"digest": "sha256:" + "b" * 64}}).encode()
    run = {
        "id": 123,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "path": CI_WORKFLOW,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
    }
    proof = {
        "schema_version": SCHEMA,
        "repository": REPOSITORY,
        "workflow": CI_WORKFLOW,
        "commit_sha": SHA,
        "run_id": 123,
        "run_attempt": 1,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }
    return manifest, proof, run


@pytest.mark.parametrize(
    "field,value",
    [
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("head_sha", "c" * 40),
        ("head_branch", "feature"),
        ("event", "pull_request"),
        ("path", ".github/workflows/other.yml"),
        ("run_attempt", 2),
        ("id", 456),
    ],
)
def test_proof_rejects_wrong_or_failed_ci(field, value):
    manifest, proof, run = fixture()
    run[field] = value
    with pytest.raises(ValueError):
        validate_bindings(manifest, proof, SHA, run)


def test_proof_rejects_manifest_change():
    manifest, proof, run = fixture()
    with pytest.raises(ValueError):
        validate_bindings(manifest + b" ", proof, SHA, run)


def test_verifier_requires_signature_and_exact_signer_policy(tmp_path):
    manifest, proof, run = fixture()
    m, p = tmp_path / "manifest.json", tmp_path / "proof.json"
    m.write_bytes(manifest)
    p.write_text(json.dumps(proof))
    commands = []

    def execute(command, **options):
        commands.append(command)
        assert options["check"] is True and options["timeout"] == 90
        return SimpleNamespace(stdout='[{"verified":true}]')

    def fetch(path):
        return run if "/actions/runs/" in path else {"attestations": [{"bundle": {}}]}
    assert verify(m, p, SHA, fetch=fetch, execute=execute)["status"] == "verified"
    assert "--deny-self-hosted-runners" in commands[0]
    assert REPOSITORY + "/.github/workflows/release-proof.yml" in commands[0]
    assert commands[0][commands[0].index("--source-digest") + 1] == SHA
    assert commands[0][commands[0].index("--signer-digest") + 1] == SHA
    assert str(p) in commands[0]
    with pytest.raises(ValueError, match="No signed"):
        verify(m, p, SHA, fetch=lambda path: run if "/actions/runs/" in path else {}, execute=execute)


@pytest.mark.parametrize("status,conclusion", [("in_progress", None), ("completed", "failure")])
def test_failed_or_running_later_attempt_revokes_old_candidate(tmp_path, status, conclusion):
    manifest, proof, run = fixture()
    m, p = tmp_path / "manifest.json", tmp_path / "proof.json"
    m.write_bytes(manifest)
    p.write_text(json.dumps(proof))

    def fetch(path):
        if "/attempts/" in path:
            return run
        return {**run, "run_attempt": 2, "status": status, "conclusion": conclusion}

    def never_execute(*args, **kwargs):
        pytest.fail("Rejected CI history must not reach a signing command")

    with pytest.raises(ValueError, match="not successful"):
        verify(m, p, SHA, fetch=fetch, execute=never_execute)
