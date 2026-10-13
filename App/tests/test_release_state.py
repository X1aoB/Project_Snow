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

CONTROLLED_DIRECTORY = release_state._controlled_directory
REQUIRE_REGULAR = maintenance.require_regular


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


@pytest.fixture
def legacy_baseline(state):
    root = state.root / "releases" / "anchors" / release_state.LEGACY_BASELINE_ID
    configuration = state.root / "releases" / "configurations" / release_state.LEGACY_BASELINE_SHA
    hashes = {}
    for name in release_state.LEGACY_BASELINE_CONFIGURATION:
        # Bytes, rather than newline-normalized text, are the retained identity.
        payload = ("synthetic " + name + "\r\n").encode()
        path = configuration / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "e" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "f" * 64
    manifest = {"schema_version": "project-snow-release-1", "commit_sha": release_state.LEGACY_BASELINE_SHA,
                "app_version": "0.9.6", "configuration_sha256": hashes,
                "application": dict(zip(("image", "digest"), image.split("@"))),
                "embedding": dict(zip(("image", "digest"), embedding.split("@")))}
    binding = {"schema_version": "project-snow-config-snapshot-1", "colour": "blue",
               "commit_sha": release_state.LEGACY_BASELINE_SHA, "root": str(configuration),
               "configuration_sha256": hashes}
    containers = [{"id": f"{index + 100:064x}", "image": f"sha256:{index % 19 + 200:064x}",
                   "name": f"/synthetic-{index}", "status": "running" if index % 2 else "exited"}
                  for index in range(25)]
    write(root / "active-colour", "blue\n")
    write(root / "current", f"blue {release_state.LEGACY_BASELINE_SHA} {image} {embedding}\n")
    write(root / "current-config.json", binding)
    write(root / "current-manifest.json", manifest)
    write(root / "containers.json", containers)
    return root, containers, {image, embedding}


def test_exact_legacy_baseline_is_read_only_and_preserves_every_captured_image(state, legacy_baseline):
    root, containers, coordinates = legacy_baseline
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    references = release_state.protected_image_references(state)
    assert coordinates | {item["image"] for item in containers} <= references
    assert len({item["image"] for item in containers}) == 19
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert set(path.name for path in root.iterdir()) == release_state.LEGACY_BASELINE_FILES


def test_legacy_gc_retains_captured_and_resolved_oci_identities(state, legacy_baseline):
    _root, containers, _coordinates = legacy_baseline
    captured = {item["image"] for item in containers}
    original = sorted(captured)[0]
    live, alias, old = (f"sha256:{index:064x}" for index in (300, 301, 302))
    all_ids = captured | {live, alias, old}
    calls = []
    def image(identifier):
        return {"Id": identifier, "RepoTags": ["ghcr.io/x1aob/project_snow-public:" + identifier[-8:]],
                "RepoDigests": [], "Size": 100}
    def execute(command):
        calls.append(command)
        if command[1] == "ps":
            return ""
        if command[1:3] == ["image", "ls"]:
            if "--format" in command:
                return "\n".join(f"{identifier} 0" for identifier in all_ids)
            return "\n".join(all_ids)
        if command[1:3] == ["image", "inspect"]:
            identifiers = command[3:]
            if len(identifiers) == 1:
                reference = identifiers[0]
                return json.dumps([image(live if "@" in reference else alias if reference == original else reference)])
            return json.dumps([image(identifier) for identifier in identifiers])
        raise AssertionError(command)
    plan = release_state.image_gc_plan(state, execute)
    assert captured | {live, alias} <= set(plan["protected_image_ids"])
    assert [item["image_id"] for item in plan["candidates"]] == [old]
    assert all(command[1:3] != ["image", "rm"] and command[1] != "rm" for command in calls)


def test_missing_captured_image_disables_gc_instead_of_forgetting_baseline(state, legacy_baseline):
    missing = sorted({row["image"] for row in legacy_baseline[1]})[0]
    calls = []
    def execute(command):
        calls.append(command)
        if command[1] == "ps" or command[1:3] == ["image", "ls"]:
            return ""
        if command[1:3] == ["image", "inspect"]:
            return "[]" if command[3] == missing else json.dumps([{"Id": "sha256:" + "0" * 64}])
        raise AssertionError(command)
    with pytest.raises(maintenance.MaintenanceError, match="retained release image is missing"):
        release_state.image_gc_plan(state, execute)
    assert ["docker", "image", "inspect", missing] in calls
    assert not any("rm" in command or "prune" in command for command in calls)


@pytest.mark.parametrize("fault", ["extra_file", "missing_file", "unknown_anchor", "wrong_colour", "wrong_sha",
                                  "bad_manifest_schema", "bad_app_version", "wrong_image", "bad_config_schema",
                                  "wrong_config_root", "hash_disagreement", "unsafe_config_path", "changed_config",
                                  "empty_inventory", "duplicate_container", "duplicate_name", "bad_container_id",
                                  "bad_image_id", "bad_status", "extra_container_field"])
def test_legacy_faults_fail_before_any_docker_inventory(state, legacy_baseline, fault):
    root, containers, _coordinates = legacy_baseline
    manifest = json.loads((root / "current-manifest.json").read_text())
    binding = json.loads((root / "current-config.json").read_text())
    if fault == "extra_file":
        write(root / "unexpected", "x")
    elif fault == "missing_file":
        (root / "current").unlink()
    elif fault == "unknown_anchor":
        root.rename(root.with_name("baseline-unknown"))
    elif fault == "wrong_colour":
        write(root / "active-colour", "green\n")
    elif fault == "wrong_sha":
        write(root / "current", (root / "current").read_text().replace(release_state.LEGACY_BASELINE_SHA, "a" * 40))
    elif fault == "bad_manifest_schema":
        manifest["schema_version"] = "unknown"
    elif fault == "bad_app_version":
        manifest["app_version"] = "0.9.7"
    elif fault == "wrong_image":
        manifest["embedding"]["digest"] = "sha256:" + "0" * 64
    elif fault == "bad_config_schema":
        binding["schema_version"] = "unknown"
    elif fault == "wrong_config_root":
        binding["root"] = str(state.root / "elsewhere")
    elif fault == "hash_disagreement":
        binding["configuration_sha256"]["compose.prod.yml"] = "0" * 64
    elif fault == "unsafe_config_path":
        for document in (manifest, binding):
            document["configuration_sha256"]["../public.env"] = document["configuration_sha256"].pop("compose.prod.yml")
    elif fault == "changed_config":
        write(Path(binding["root"]) / "compose.prod.yml", "changed")
    elif fault == "empty_inventory":
        containers.clear()
    elif fault == "duplicate_container":
        containers[1]["id"] = containers[0]["id"]
    elif fault == "duplicate_name":
        containers[1]["name"] = containers[0]["name"].removeprefix("/")
    elif fault == "bad_container_id":
        containers[0]["id"] = "short"
    elif fault == "bad_image_id":
        containers[0]["image"] = "ghcr.io/x1aob/project_snow-public:latest"
    elif fault == "bad_status":
        containers[0]["status"] = "unknown"
    elif fault == "extra_container_field":
        containers[0]["unexpected"] = "x"
    if fault != "unknown_anchor":
        write(root / "current-manifest.json", manifest)
        write(root / "current-config.json", binding)
        write(root / "containers.json", containers)
    calls = []
    with pytest.raises(maintenance.MaintenanceError):
        release_state.image_gc_plan(state, lambda command: calls.append(command) or "")
    assert calls == []


@pytest.mark.parametrize("name,payload", [
    ("current-manifest.json", '{"schema_version":"project-snow-release-1","schema_version":"project-snow-release-1"}'),
    ("current-config.json", '{"configuration_sha256":{"compose.prod.yml":"a","compose.prod.yml":"b"}}'),
    ("containers.json", '[{"id":"a","id":"b"}]'),
    ("containers.json", '[NaN]'),
    ("containers.json", '[{"id":'),
])
def test_legacy_rejects_duplicate_and_malformed_json(state, legacy_baseline, name, payload):
    write(legacy_baseline[0] / name, payload)
    with pytest.raises(maintenance.MaintenanceError, match="JSON"):
        release_state.protected_image_references(state)


@pytest.mark.parametrize("kind", ["directory_owner", "directory_writable", "directory_symlink",
                                 "file_owner", "file_writable", "file_symlink", "file_hardlink"])
def test_legacy_requires_root_controlled_directories_and_single_link_files(state, legacy_baseline, monkeypatch, kind):
    from types import SimpleNamespace
    import stat
    root = legacy_baseline[0]
    target = root if kind.startswith("directory") else root / "containers.json"
    original = Path.lstat
    mode = (stat.S_IFDIR | 0o700) if kind.startswith("directory") else (stat.S_IFREG | 0o600)
    if kind.endswith("writable"):
        mode |= 0o020
    if kind.endswith("symlink"):
        mode = stat.S_IFLNK | 0o777
    def metadata(path):
        if path == target:
            return SimpleNamespace(st_mode=mode, st_uid=1 if kind.endswith("owner") else 0,
                                   st_gid=0, st_nlink=2 if kind.endswith("hardlink") else 1)
        result = original(path)
        # Synthetic ancestors in /tmp and on Windows need not be root-owned;
        # only the injected target supplies the production ownership fault.
        return SimpleNamespace(st_mode=result.st_mode & ~0o022, st_uid=0, st_gid=0, st_nlink=1)
    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(release_state, "_controlled_directory", CONTROLLED_DIRECTORY)
    monkeypatch.setattr(release_state, "require_regular", REQUIRE_REGULAR)
    with pytest.raises(maintenance.MaintenanceError, match="root-controlled|regular file"):
        release_state.protected_image_references(state)
