from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import maintenance
import release_state


@pytest.fixture(autouse=True)
def portable_metadata_checks(monkeypatch):
    # The production helper uses Linux root ownership and directory fsync.
    # Business/failure tests also run on the Windows development workstation.
    monkeypatch.setattr(maintenance, "require_regular", lambda *a, **kw: None)
    monkeypatch.setattr(release_state, "require_regular", lambda *a, **kw: None)
    monkeypatch.setattr(release_state, "_controlled_directory", lambda *a: None)
    monkeypatch.setattr(release_state, "sync", lambda *a: None)
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a: None, raising=False)


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((payload if isinstance(payload, str) else json.dumps(payload)).encode())


@pytest.fixture
def state(tmp_path):
    paths = maintenance.Paths(root=tmp_path / "snow", secrets=tmp_path / "secrets")
    root = paths.root
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
    shared = {key: embedding for key in maintenance.SHARED_IMAGE_KEYS}
    for colour, commit in (("blue", "c" * 40), ("green", "d" * 40)):
        configuration = root / "releases" / "configurations" / commit
        write(configuration / "infra" / "postgres" / "postgresql.conf", "shared_buffers=1GB\n")
        env = {"PUBLIC_API_IMAGE": image, **shared}
        environment = "".join(f"{k}={v}\n" for k, v in env.items())
        manifest = {"commit_sha": commit, "application": {"image": image.split("@")[0], "digest": image.split("@")[1]},
                    "embedding": {"image": embedding.split("@")[0], "digest": embedding.split("@")[1]}}
        binding = {"schema_version": "project-snow-config-snapshot-1", "colour": colour, "commit_sha": commit,
                   "root": str(configuration), "configuration_sha256": {"infra/postgres/postgresql.conf": hashlib.sha256(b"shared_buffers=1GB\n").hexdigest()}}
        marker = f"{colour} {commit} {image} {embedding}\n"
        write(root / "releases" / "colours" / colour, marker)
        write(root / "releases" / "colours" / f"{colour}-manifest.json", manifest)
        write(root / "releases" / "colours" / f"{colour}-config.json", binding)
        write(root / "runtime" / "colours" / f"{colour}.compose.env", environment)
        if colour == "blue":
            write(root / "runtime" / "compose.env", environment)
            write(root / "releases" / "current", marker)
            write(root / "releases" / "current-manifest.json", manifest)
            write(root / "releases" / "current-config.json", binding)
    write(root / "releases" / "active-colour", "blue\n")
    return paths


def test_anchors_survive_candidate_overwrite_and_prepare_exact_rollback(state):
    result = release_state.archive_colours(state)
    anchor = result["colours"]["green"]
    assert release_state.archive_colours(state)["colours"]["green"] == anchor
    active_before = (state.root / "releases" / "current").read_bytes()
    # Simulate an already-staged different SHA occupying the same inactive slot.
    for field in ("green", "green-manifest.json", "green-config.json"):
        path = state.root / "releases" / "colours" / field
        text = path.read_text().replace("d" * 40, "e" * 40)
        path.write_bytes(text.encode())
    prepared = release_state.restore_colour(state, anchor, "green")
    assert prepared["commit_sha"] == "d" * 40
    assert (state.root / "releases" / "current").read_bytes() == active_before
    assert (state.root / "releases" / "colours" / "green").read_text().split()[1] == "d" * 40
    assert release_state.verify_anchor(state, anchor)[1]["commit_sha"] == "d" * 40


def test_recovery_refuses_active_slot_and_missing_configuration(state):
    anchor = release_state.archive_colours(state)["colours"]["green"]
    with pytest.raises(maintenance.MaintenanceError, match="inactive"):
        release_state.restore_colour(state, anchor, "blue")
    configuration = state.root / "releases" / "configurations" / ("d" * 40) / "infra" / "postgres" / "postgresql.conf"
    configuration.write_bytes(b"tampered")
    with pytest.raises(maintenance.MaintenanceError, match="missing or changed"):
        release_state.restore_colour(state, anchor, "green")


def test_corrupt_anchor_is_never_adopted(state):
    identifier = release_state.archive_colours(state)["colours"]["green"]
    anchor = state.root / "releases" / "anchors" / identifier
    (anchor / "marker").chmod(0o600)
    (anchor / "marker").write_bytes(b"corrupt")
    with pytest.raises(maintenance.MaintenanceError, match="hash mismatch"):
        release_state.verify_anchor(state, identifier)


def test_gc_protects_other_projects_stopped_containers_oci_aliases_and_anchors(state):
    release_state.archive_colours(state)
    live, old, dify, stopped, alias, dangling = ["sha256:" + char * 64 for char in "123456"]
    all_ids = [live, old, dify, stopped, alias, dangling]
    def image(identifier):
        repo = "langgenius/dify-api" if identifier == dify else "ghcr.io/x1aob/project_snow-public"
        return {"Id": identifier, "RepoTags": [] if identifier == dangling else [repo + ":" + identifier[-8:]], "RepoDigests": [], "Size": 100}
    def execute(command):
        if command[1] == "ps":
            return "a" * 64 + "\n" + "b" * 64
        if command[1] == "inspect":
            return json.dumps([{"Image": live}, {"Image": stopped}])
        if command[1:3] == ["image", "ls"]:
            if "--format" in command:
                return "\n".join(f"{value} {1 if value == alias else 0}" for value in all_ids)
            return "\n".join(all_ids)
        if command[1:3] == ["image", "inspect"]:
            if "@sha256:" in command[3]:
                return json.dumps([image(live)])
            return json.dumps([image(identifier) for identifier in command[3:]])
        raise AssertionError(command)
    plan = release_state.image_gc_plan(state, execute)
    assert [entry["image_id"] for entry in plan["candidates"]] == [old]
    assert {live, stopped, alias} <= set(plan["protected_image_ids"])


def test_gc_rechecks_before_every_nonforced_deletion(state):
    first, second = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    calls = []
    plans = [{"candidates": [{"image_id": first}, {"image_id": second}]}, {"candidates": []}]
    with patch.object(release_state, "image_gc_plan", side_effect=plans):
        with pytest.raises(maintenance.MaintenanceError, match="no longer"):
            release_state.delete_planned_images(state, [first, second], lambda command: calls.append(command) or "")
    assert calls == [["docker", "image", "rm", first]]


def test_inactive_anchor_write_failure_does_not_change_active_release(state):
    identifier = release_state.archive_colours(state)["colours"]["green"]
    before = (state.root / "releases" / "current").read_bytes()
    with patch.object(release_state, "atomic_write", side_effect=OSError("injected disk fault")):
        with pytest.raises(OSError):
            release_state.restore_colour(state, identifier, "green")
    assert (state.root / "releases" / "current").read_bytes() == before
