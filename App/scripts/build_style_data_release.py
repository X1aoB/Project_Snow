"""Extend a verified public data release with its versioned dialogue-style derivative."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from backend.snow_app.data_release import file_sha256, verify_data_release

STYLE_FILES = {
    "character_lines.jsonl", "dialogue_pairs.jsonl", "examples.sqlite3",
    "register_terms.jsonl", "shared_topics.jsonl", "signature_terms.jsonl", "situational_cues.jsonl",
}


def build(base: Path, base_digest: str, style: Path, output: Path, version: str, generated_at: str):
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", version):
        raise ValueError("Invalid data version")
    if file_sha256(base / "manifest.json") != base_digest:
        raise ValueError("Parent manifest digest mismatch")
    manifest = verify_data_release(base)
    profile = json.loads((style / "manifest.json").read_text(encoding="utf8"))
    if set(profile["files"]) != STYLE_FILES or profile["version"] != "1.4.0":
        raise ValueError("Unexpected style data inventory or version")
    bindings = {"public_review_sha256": APP / "config/public_knowledge/data_license_review.json",
                "public_denylist_sha256": APP / "config/content_denylist.json"}
    if any(profile.get(key) != file_sha256(path) for key, path in bindings.items()):
        raise ValueError("Style corpus does not bind the current public source review and exclusions")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / version
    if destination.exists():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(dir=output) as tmp:
        stage = Path(tmp) / "package"
        for record in manifest["files"]:
            target = stage / record["path"]
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(base / record["path"], target)
        prefix = Path("style") / profile["version"]
        (stage / prefix).mkdir(parents=True)
        for filename in sorted(STYLE_FILES | {"manifest.json"}):
            target = stage / prefix / filename
            shutil.copyfile(style / filename, target)
            digest = file_sha256(target)
            if filename != "manifest.json" and digest != profile["files"][filename]:
                raise ValueError(f"Style data checksum mismatch for {filename}")
            manifest["files"].append({"path": target.relative_to(stage).as_posix(),
                                      "bytes": target.stat().st_size, "sha256": digest})
        manifest.update(data_version=version, parent_manifest_sha256=base_digest, generated_at=generated_at)
        manifest["files"].sort(key=lambda entry: entry["path"])
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf8", newline="\n")
        verify_data_release(stage, version)
        os.rename(stage, destination)
    return file_sha256(destination / "manifest.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--base-digest", required=True)
    parser.add_argument("--style", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--generated-at", required=True, help="Fixed ISO UTC build timestamp for reproducibility")
    print(build(**vars(parser.parse_args())))
