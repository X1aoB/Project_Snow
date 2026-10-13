from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

import pytest

from scripts import verify_public_image_inventory as inventory


COMMIT = "a" * 40


@pytest.fixture
def app_tree(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for source, _ in inventory.DIRECTORIES:
        directory = root / source
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "current.txt").write_text(source, encoding="utf-8")
    for source, _ in inventory.FILES:
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    (root / ".build/public-ui/frontend-bundle-manifest.json").write_text(
        json.dumps({"source_commit": COMMIT}), encoding="utf-8")
    return root


def assemble(app_tree: Path, destination: Path) -> dict:
    manifest = inventory.build_inventory(app_tree, COMMIT)
    for source, target in inventory.DIRECTORIES:
        for path in (app_tree / source).rglob("*"):
            if path.is_file():
                output = destination / target / path.relative_to(app_tree / source)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(path.read_bytes())
    for source, target in inventory.FILES:
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((app_tree / source).read_bytes())
    return manifest


def test_complete_fresh_image_is_bound_to_source_and_all_copy_inputs(app_tree, tmp_path):
    image = tmp_path / "image"
    manifest = assemble(app_tree, image)
    result = inventory.verify_inventory(image, manifest, COMMIT)
    assert result == {"status": "verified", "source_commit": COMMIT, "file_count": len(manifest["files"])}


@pytest.mark.parametrize("fault", ["old_backend", "old_hashed_ui", "missing", "changed_bytes", "wrong_revision"])
def test_stale_or_mismatched_application_fails(app_tree, tmp_path, fault):
    image = tmp_path / "image"
    manifest = assemble(app_tree, image)
    revision = COMMIT
    if fault == "old_backend":
        (image / "backend/obsolete.py").write_text("old", encoding="utf-8")
    elif fault == "old_hashed_ui":
        (image / "public_frontend/app.oldhash.js").write_text("old", encoding="utf-8")
    elif fault == "missing":
        (image / "requirements-public.txt").unlink()
    elif fault == "changed_bytes":
        (image / "backend/current.txt").write_text("wrong", encoding="utf-8")
    else:
        revision = "b" * 40
    with pytest.raises(ValueError):
        inventory.verify_inventory(image, manifest, revision)


def test_context_cache_exclusions_do_not_exclude_other_files(app_tree):
    cache = app_tree / "backend/__pycache__"
    cache.mkdir()
    (cache / "worker.pyc").write_bytes(b"cache")
    (app_tree / "backend/worker.pyo").write_bytes(b"cache")
    (app_tree / "backend/worker.py").write_bytes(b"source")
    files = inventory.build_inventory(app_tree, COMMIT)["files"]
    assert "backend/worker.py" in files
    assert not any("__pycache__" in path or path.endswith(".pyo") for path in files)


def test_frontend_from_other_commit_is_rejected(app_tree):
    with pytest.raises(ValueError, match="frontend source commit"):
        inventory.build_inventory(app_tree, "b" * 40)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "backend//file", "backend/./file", "backend\\file"])
def test_manifest_refuses_unsafe_paths(path):
    manifest = {"schema": inventory.SCHEMA, "source_commit": COMMIT,
                "files": {path: hashlib.sha256(b"x").hexdigest()}}
    with pytest.raises(ValueError, match="inventory path"):
        inventory.validate_manifest(manifest)


def test_runtime_links_are_rejected(app_tree, tmp_path, monkeypatch):
    image = tmp_path / "image"
    manifest = assemble(app_tree, image)
    path = image / "backend/current.txt"
    original = Path.lstat

    def linked_lstat(current):
        result = original(current)
        if current == path:
            values = list(result)
            values[0] = stat.S_IFLNK | 0o777
            return inventory.os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", linked_lstat)
    with pytest.raises(ValueError, match="regular file"):
        inventory.verify_inventory(image, manifest, COMMIT)
