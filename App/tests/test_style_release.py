import json
from pathlib import Path

import pytest

from backend.snow_app.data_release import file_sha256, verify_data_release
from scripts.build_style_data_release import APP, STYLE_FILES, build
from tests.test_data_release import _build_fixture


def test_style_release_copies_only_bound_derivatives_and_rejects_changes(tmp_path: Path):
    base = _build_fixture(tmp_path)
    (base / "unlisted.txt").write_text("synthetic extra")
    style = tmp_path / "style"
    style.mkdir()
    for name in STYLE_FILES:
        (style / name).write_bytes(b"synthetic derivative")
    profile = {"version": "1.4.0", "files": {name: file_sha256(style / name) for name in STYLE_FILES},
               "public_review_sha256": file_sha256(APP / "config/public_knowledge/data_license_review.json"),
               "public_denylist_sha256": file_sha256(APP / "config/content_denylist.json")}
    (style / "manifest.json").write_text(json.dumps(profile))
    output = tmp_path / "derived"
    args = (base, file_sha256(base / "manifest.json"), style, output)
    build(*args, "fixture1", "2026-11-23T13:00:00Z")
    manifest = verify_data_release(output / "fixture1", "fixture1")
    assert manifest["parent_manifest_sha256"] == args[1]
    assert not (output / "fixture1/unlisted.txt").exists()
    assert (output / "fixture1/style/1.4.0/manifest.json").read_bytes() == (style / "manifest.json").read_bytes()
    (style / "examples.sqlite3").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        build(*args, "fixture2", "2026-11-23T13:00:00Z")
    assert not (output / "fixture2").exists()

