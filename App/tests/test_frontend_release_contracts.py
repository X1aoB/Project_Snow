from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from scripts import release_manifest

APP = Path(__file__).resolve().parents[1]
IDENTITY = {
    "schema_version": "project-snow-frontend-identity-1",
    "track": "compat",
    "version": "compat-096-r1",
    "bundle_sha256": "a" * 64,
}
COMMIT = "b" * 40


@pytest.fixture
def builder(monkeypatch):
    from scripts import prepare_public_frontend

    calls = []
    monkeypatch.setattr(prepare_public_frontend, "prepare", lambda app_root, output: dict(IDENTITY))

    def verify(output, *, expected_source_commit=None):
        calls.append((output, expected_source_commit))
        return dict(IDENTITY)

    monkeypatch.setattr(prepare_public_frontend, "verify_bundle", verify)
    return prepare_public_frontend, calls


def test_host_reconstructs_identity_without_any_existing_build_directory(builder, tmp_path):
    binding = release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT)
    assert binding == {key: IDENTITY[key] for key in ("track", "version", "bundle_sha256")}
    assert len(builder[1]) == 1 and builder[1][0][1] == COMMIT
    assert not (tmp_path / ".build").exists()
    assert not builder[1][0][0].exists()  # Temporary reconstruction is removed.


def test_ci_verifies_both_reconstructed_source_and_actual_docker_input(builder, tmp_path):
    artifact = tmp_path / "docker-input"
    release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT, frontend_bundle=artifact)
    assert len(builder[1]) == 2
    assert builder[1][-1] == (artifact, COMMIT)


def test_drifted_but_internally_valid_docker_input_is_rejected(builder, monkeypatch, tmp_path):
    artifact = tmp_path / "docker-input"
    monkeypatch.setattr(
        builder[0],
        "verify_bundle",
        lambda output, **kwargs: {**IDENTITY, "track": "current"} if output == artifact else dict(IDENTITY),
    )
    with pytest.raises(ValueError, match="differs from the trusted source"):
        release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT, frontend_bundle=artifact)


def test_missing_or_corrupt_prepared_input_does_not_fall_back_to_reconstruction(
    builder, monkeypatch, tmp_path
):
    artifact = tmp_path / "missing"

    def verify(output, **kwargs):
        if output == artifact:
            raise ValueError("Missing prepared input")
        return dict(IDENTITY)

    monkeypatch.setattr(builder[0], "verify_bundle", verify)
    with pytest.raises(ValueError, match="Missing prepared input"):
        release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT, frontend_bundle=artifact)


def test_wrong_source_commit_fails_before_manifest_can_be_signed(builder, monkeypatch, tmp_path):
    def verify(output, *, expected_source_commit=None):
        assert expected_source_commit == COMMIT
        raise ValueError("Source commit mismatch")

    monkeypatch.setattr(builder[0], "verify_bundle", verify)
    with pytest.raises(ValueError, match="Source commit mismatch"):
        release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT)


@pytest.mark.parametrize("failure", [False, True])
def test_manifest_reconstruction_suppresses_new_checkout_caches_and_restores_policy(
    builder, monkeypatch, tmp_path, failure
):
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    def prepare(*args):
        assert sys.dont_write_bytecode is True
        if failure:
            raise ValueError("fixture build failure")
        return dict(IDENTITY)

    monkeypatch.setattr(builder[0], "prepare", prepare)
    if failure:
        with pytest.raises(ValueError, match="fixture build failure"):
            release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT)
    else:
        release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT)
    assert sys.dont_write_bytecode is False


@pytest.mark.parametrize(
    "change",
    [
        {"track": "runtime-env"},
        {"version": ""},
        {"bundle_sha256": "invalid"},
        {"schema_version": "unknown"},
        {"extra": "field"},
    ],
)
def test_unrecognized_frontend_identity_is_not_emitted(builder, monkeypatch, tmp_path, change):
    invalid = {**IDENTITY, **change}
    monkeypatch.setattr(builder[0], "prepare", lambda *args: invalid)
    monkeypatch.setattr(builder[0], "verify_bundle", lambda *args, **kwargs: invalid)
    with pytest.raises(ValueError, match="Invalid selected frontend identity"):
        release_manifest.read_frontend_binding(tmp_path, commit_sha=COMMIT)


def test_all_public_image_workflows_prepare_one_verified_bundle_before_building():
    builds = []
    for name in ("ci.yml", "publish-images.yml", "nightly-image-security.yml"):
        workflow = yaml.safe_load((APP.parent / ".github/workflows" / name).read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            if any(
                "test_frontend_release_contracts.py" in entry.get("run", "")
                or "test_public_frontend_bundle.py" in entry.get("run", "")
                for entry in steps
            ):
                checkout = next(
                    entry for entry in steps if entry.get("uses", "").startswith("actions/checkout@")
                )
                assert checkout["with"]["fetch-depth"] == 0, (name, job_name)
            for index, step in enumerate(steps):
                if step.get("with", {}).get("file") != "App/infra/public-api.Dockerfile":
                    continue
                builds.append((name, job_name))
                prepare = [
                    entry for entry in steps[:index] if "prepare_public_frontend.py" in entry.get("run", "")
                ]
                assert len(prepare) == 1 and "if" not in prepare[0]
                assert "--app-root App --output App/.build/public-ui" in prepare[0]["run"]
                checkout = next(
                    entry for entry in steps[:index] if entry.get("uses", "").startswith("actions/checkout@")
                )
                assert checkout["with"]["fetch-depth"] == 0
                assert step["with"]["context"] == "App"
                assert "APP_REVISION=${{ github.sha }}" in step["with"]["build-args"]
            for step in steps:
                if "python App/scripts/release_manifest.py" in step.get("run", ""):
                    assert "--frontend-bundle App/.build/public-ui" in step["run"]
    assert len(builds) == 3


def test_docker_copies_only_selected_ui_and_verifies_final_bytes():
    dockerfile = (APP / "infra/public-api.Dockerfile").read_text(encoding="utf-8")
    copies = [line.split()[1:] for line in dockerfile.splitlines() if line.startswith("COPY ")]
    for destination in ("public_frontend", "frontend/shared", "frontend/assets/immersive"):
        selected = [parts for parts in copies if parts[-1] == "./" + destination]
        assert selected == [[".build/public-ui/" + destination, "./" + destination]]
    assert "--verify --output /app" in dockerfile
    assert "fingerprint_public_frontend.py" not in dockerfile
    assert "ARG FRONTEND" not in dockerfile and "ENV FRONTEND" not in dockerfile


def test_real_committed_bundle_and_host_manifest_reconstruction_agree(tmp_path):
    from scripts.prepare_public_frontend import prepare
    from tests.test_public_frontend_bundle import fixture_repo, git

    repo = fixture_repo(tmp_path)
    commit = git(repo, "rev-parse", "HEAD")
    output = tmp_path / "docker-ui"
    prepared = prepare(repo / "App", output)
    actual = release_manifest.read_frontend_binding(repo / "App", commit_sha=commit, frontend_bundle=output)
    assert actual == {key: prepared[key] for key in ("track", "version", "bundle_sha256")}
    assert actual["track"] == "compat" and actual["version"] == "compat-096-r1"
    # Changing a served byte without rebuilding identity cannot be signed.
    asset = output / "public_frontend/index.html"
    asset.write_bytes(asset.read_bytes() + b"\n<!-- unexpected build drift -->")
    with pytest.raises(ValueError):
        release_manifest.read_frontend_binding(repo / "App", commit_sha=commit, frontend_bundle=output)
