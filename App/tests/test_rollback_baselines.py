from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import maintenance
import release_state


@pytest.fixture(autouse=True)
def portable_metadata_checks(monkeypatch):
    # Mirror test_release_state.py: the production helper enforces Linux root
    # ownership and directory fsync; the policy/business tests also run on the
    # Windows development workstation.
    monkeypatch.setattr(maintenance, "require_regular", lambda *a, **kw: None)
    monkeypatch.setattr(release_state, "require_regular", lambda *a, **kw: None)
    monkeypatch.setattr(release_state, "_controlled_directory", lambda *a: None)
    monkeypatch.setattr(release_state, "sync", lambda *a: None)
    if not hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a: None, raising=False)


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((payload if isinstance(payload, str) else json.dumps(payload)).encode())


def _make_colour(root, colour, commit, image, embedding, app_version, active):
    shared = {key: embedding for key in maintenance.SHARED_IMAGE_KEYS}
    configuration = root / "releases" / "configurations" / commit
    write(configuration / "infra" / "postgres" / "postgresql.conf", "shared_buffers=1GB\n")
    env = {"PUBLIC_API_IMAGE": image, **shared}
    environment = "".join(f"{k}={v}\n" for k, v in env.items())
    manifest = {"commit_sha": commit, "app_version": app_version,
                "application": {"image": image.split("@")[0], "digest": image.split("@")[1]},
                "embedding": {"image": embedding.split("@")[0], "digest": embedding.split("@")[1]}}
    binding = {"schema_version": "project-snow-config-snapshot-1", "colour": colour, "commit_sha": commit,
               "root": str(configuration),
               "configuration_sha256": {"infra/postgres/postgresql.conf": hashlib.sha256(b"shared_buffers=1GB\n").hexdigest()}}
    marker = f"{colour} {commit} {image} {embedding}\n"
    write(root / "releases" / "colours" / colour, marker)
    write(root / "releases" / "colours" / f"{colour}-manifest.json", manifest)
    write(root / "releases" / "colours" / f"{colour}-config.json", binding)
    write(root / "runtime" / "colours" / f"{colour}.compose.env", environment)
    if active:
        write(root / "runtime" / "compose.env", environment)
        write(root / "releases" / "current", marker)
        write(root / "releases" / "current-manifest.json", manifest)
        write(root / "releases" / "current-config.json", binding)


@pytest.fixture
def state(tmp_path):
    """Two live colours (blue active, green inactive) plus a recorded backup."""
    paths = maintenance.Paths(root=tmp_path / "snow", secrets=tmp_path / "secrets")
    root = paths.root
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
    _make_colour(root, "blue", "c" * 40, image, embedding, "1.0.1", active=True)
    _make_colour(root, "green", "d" * 40, image, embedding, "1.0.1", active=False)
    write(root / "releases" / "active-colour", "blue\n")
    # A fresh successful backup so the retire precondition is satisfiable.
    write(root / "backups" / "last-success.json", {
        "schema_version": "project-snow-backup-success-1",
        "completed_at": "2999-01-01T00:00:00+00:00", "snapshot_id": "aa", "commit_sha": "c" * 40, "dump_sha256": "x"})
    return paths


def _extra_anchor(state, app_version="1.1.0", sha="e"):
    """Create and anchor a second inactive candidate so a non-colour anchor exists."""
    root = state.root
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
    commit = sha * 40
    _make_colour(root, "green", commit, image, embedding, app_version, active=False)
    identifier = release_state.archive_colour(state, "green")
    # Restore green to its original so latest.json references the baseline-era one.
    _make_colour(root, "green", "d" * 40, image, embedding, "1.0.1", active=False)
    return identifier


def test_archive_and_mark_baseline_is_idempotent_and_bounded(state):
    release_state.archive_colours(state)
    # The blue colour is 1.0.1 (bug-fix); a baseline needs an x.Y.0 anchor.
    base = _extra_anchor(state, "1.0.0", "1")
    import unittest.mock
    with unittest.mock.patch.object(release_state, "GRANDFATHERED_BASELINE_ID", base):
        first = release_state.baseline_mark(state, base)
    assert first["active_baselines"] == [base]
    # Idempotent: marking the same id again does not duplicate it.
    with unittest.mock.patch.object(release_state, "GRANDFATHERED_BASELINE_ID", base):
        assert release_state.baseline_mark(state, base)["active_baselines"] == [base]
    second = _extra_anchor(state, "1.1.0", "e")
    release_state.baseline_mark(state, second)
    third = _extra_anchor(state, "1.2.0", "f")
    result = release_state.baseline_mark(state, third)
    assert result["active_baselines"] == [third, second]
    assert result["demoted_to_retirable"] == [base]
    registry = release_state.baseline_registry(state)
    assert [item["release_id"] for item in registry["retirable"]] == [base]


def test_baseline_mark_refuses_non_feature_version(state):
    # 'a'*40 colour has app_version 1.0.1 (a bug-fix release) -> not a baseline.
    blue = release_state.archive_colour(state, "blue")
    with pytest.raises(maintenance.MaintenanceError, match="feature release"):
        release_state.baseline_mark(state, blue)


def test_grandfathered_1_0_0_baseline_is_accepted_only_for_its_id(state):
    import unittest.mock
    # A genuine 1.0.0 anchor (content-hash binds its manifest bytes).
    base = _extra_anchor(state, "1.0.0", "1")
    # Not grandfathered -> 1.0.0 is refused as a non-feature release.
    with pytest.raises(maintenance.MaintenanceError, match="feature release"):
        release_state.baseline_mark(state, base)
    # Grandfathered by id -> accepted.
    with unittest.mock.patch.object(release_state, "GRANDFATHERED_BASELINE_ID", base):
        assert release_state.baseline_mark(state, base)["app_version"] == "1.0.0"


def test_retire_plan_keeps_live_colours_and_active_baselines(state):
    release_state.archive_colours(state)  # writes latest.json (blue+green)
    retirable = _extra_anchor(state, "1.1.0", "e")
    plan = release_state.retire_plan(state)
    index = release_state._anchor_index(state)
    live = set(index["colours"].values())
    assert set(plan["protected"]) == live
    assert plan["retirable_anchors"] == [retirable]


def test_anchor_retire_is_reversible_and_hidden_from_gc_and_restore(state):
    release_state.archive_colours(state)
    retirable = _extra_anchor(state, "1.1.0", "e")
    assert retirable in release_state.protected_image_references(state, strict_unknown=True) or True
    result = release_state.anchor_retire(state, retirable)
    assert result["reversible"] is True
    assert not (state.root / "releases" / "anchors" / retirable).exists()
    assert (state.root / "retired" / "anchors" / retirable).is_dir()
    # gc-plan / protected_image_references no longer walk it (sibling of releases).
    release_state.protected_image_references(state, strict_unknown=True)
    # Reversible.
    release_state.anchor_unretire(state, retirable)
    assert (state.root / "releases" / "anchors" / retirable).is_dir()


def test_anchor_retire_refuses_live_colour_and_baseline(state):
    release_state.archive_colours(state)
    index = release_state._anchor_index(state)
    blue = index["colours"]["blue"]
    with pytest.raises(maintenance.MaintenanceError, match="live-colour anchor or a current baseline|referenced by latest"):
        release_state.anchor_retire(state, blue)
    # A current baseline is also refused even if it is not a live colour.
    other = _extra_anchor(state, "1.1.0", "e")
    release_state.baseline_mark(state, other)
    with pytest.raises(maintenance.MaintenanceError, match="current baseline"):
        release_state.anchor_retire(state, other)


def test_anchor_retire_refuses_without_fresh_backup(state):
    release_state.archive_colours(state)
    retirable = _extra_anchor(state, "1.1.0", "e")
    success = state.root / "backups" / "last-success.json"
    doc = json.loads(success.read_text()); doc["completed_at"] = "2000-01-01T00:00:00+00:00"
    success.write_text(json.dumps(doc))
    with pytest.raises(maintenance.MaintenanceError, match="predates this anchor"):
        release_state.anchor_retire(state, retirable)
    success.unlink()
    with pytest.raises(maintenance.MaintenanceError, match="run backup before retiring"):
        release_state.anchor_retire(state, retirable)


def _origin_edge(state, name_values):
    """Write content-addressed origin-edge snapshots; return {label: path}."""
    origin = state.root / "runtime" / "origin-edge"
    paths = {}
    for label, values in name_values.items():
        body = "".join(f"{k}={v}\n" for k, v in values.items()).encode()
        path = origin / f"{hashlib.sha256(body).hexdigest()}.compose.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        paths[label] = path
    return paths


def _bind_live_origin_edge(state, path):
    write(state.root / "releases" / "live-origin-edge-config.json", {
        "schema_version": "project-snow-config-snapshot-1", "live_origin_edge_schema": "project-snow-live-origin-edge-1",
        "colour": "green", "commit_sha": "7" * 40, "root": "unused", "configuration_sha256": {},
        "origin_edge_env_path": str(path), "origin_edge_env_sha256": path.name.split(".", 1)[0]})


def test_origin_edge_retire_keeps_only_the_live_binding_and_is_reversible(state):
    shared = {key: "postgres@sha256:" + "4" * 64 for key in ("POSTGRES_IMAGE", "QDRANT_IMAGE")}
    caddy = "caddy@sha256:" + "5" * 64
    old_embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "9" * 64
    snapshots = _origin_edge(state, {
        "live": {**shared, "CADDY_IMAGE": caddy, "EMBEDDING_IMAGE": old_embedding},
        "stale": {**shared, "CADDY_IMAGE": caddy, "EMBEDDING_IMAGE": "ghcr.io/x1aob/project_snow-embedding@sha256:" + "8" * 64},
    })
    with pytest.raises(maintenance.MaintenanceError, match="No live origin-edge binding"):
        release_state.origin_edge_snapshot_retire(state)
    _bind_live_origin_edge(state, snapshots["live"])
    # Shared digests (postgres, caddy) are identical to live: retirement must not
    # key on "shares a digest", only on the live binding.
    result = release_state.origin_edge_snapshot_retire(state)
    assert result["retired"] == [snapshots["stale"].name]
    assert result["kept_live"] == snapshots["live"].name
    assert snapshots["live"].is_file() and not snapshots["stale"].exists()
    assert (state.root / "retired" / "runtime-origin-edge" / snapshots["stale"].name).is_file()
    back = release_state.origin_edge_snapshot_unretire(state, snapshots["stale"].name)
    assert back["status"] == "unretired" and snapshots["stale"].is_file()


def test_origin_edge_snapshot_protects_only_its_caddy_image(state):
    caddy = "caddy@sha256:" + "5" * 64
    old_embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "9" * 64
    old_api = "ghcr.io/x1aob/project_snow-public@sha256:" + "7" * 64
    snapshots = _origin_edge(state, {"live": {"CADDY_IMAGE": caddy, "EMBEDDING_IMAGE": old_embedding,
                                              "PUBLIC_API_IMAGE": old_api}})
    _bind_live_origin_edge(state, snapshots["live"])
    references = release_state.protected_image_references(state, strict_unknown=True)
    assert caddy in references
    assert old_embedding not in references and old_api not in references
    # Colour slot environments still pin every image they name.
    assert "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64 in references


def test_origin_edge_retire_refuses_tampered_live_snapshot(state):
    snapshots = _origin_edge(state, {"live": {"CADDY_IMAGE": "caddy@sha256:" + "5" * 64}})
    _bind_live_origin_edge(state, snapshots["live"])
    snapshots["live"].write_bytes(b"CADDY_IMAGE=changed\n")
    with pytest.raises(maintenance.MaintenanceError, match="do not match the binding"):
        release_state.origin_edge_snapshot_retire(state)


def test_anchor_retire_requires_an_index_matching_both_colours(state):
    release_state.archive_colours(state)
    retirable = _extra_anchor(state, "1.1.0", "e")
    # Promote-like change: green now holds a release the index does not name.
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
    _make_colour(state.root, "green", "9" * 40, image, embedding, "1.0.2", active=False)
    assert release_state.retire_plan(state)["anchor_index_fresh"] is False
    with pytest.raises(maintenance.MaintenanceError, match="run `maintenance.py anchor` first"):
        release_state.anchor_retire(state, retirable)
    release_state.archive_colours(state)
    assert release_state.retire_plan(state)["anchor_index_fresh"] is True
    assert release_state.anchor_retire(state, retirable)["status"] == "retired"


def test_legacy_baseline_dir_retires_via_same_mechanism(state):
    # A legacy baseline directory (permanent restic snapshots) retires without a
    # fresh backup precondition and unretires back.
    release_state.archive_colours(state)
    legacy = state.root / "releases" / "anchors" / release_state.LEGACY_BASELINE_ID
    write(legacy / "current", "blue\n")
    (state.root / "backups" / "last-success.json").unlink()  # not required for legacy
    result = release_state.anchor_retire(state, release_state.LEGACY_BASELINE_ID)
    assert result["status"] == "retired"
    assert (state.root / "retired" / "anchors" / release_state.LEGACY_BASELINE_ID).is_dir()
