"""Bind the complete /app file inventory to this checkout's Docker COPY inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


SCHEMA = "project-snow-public-image-inventory-1"
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
DIRECTORIES = (
    ("backend", "backend"),
    ("config/public_knowledge", "config/public_knowledge"),
    ("migrations", "migrations"),
    (".build/public-ui/public_frontend", "public_frontend"),
    (".build/public-ui/frontend/shared", "frontend/shared"),
    (".build/public-ui/frontend/assets/immersive", "frontend/assets/immersive"),
)
FILES = (
    ("requirements-public.txt", "requirements-public.txt"),
    ("alembic.ini", "alembic.ini"),
    (".build/public-ui/frontend-identity.json", "frontend-identity.json"),
    (".build/public-ui/frontend-bundle-manifest.json", "frontend-bundle-manifest.json"),
    ("scripts/prepare_public_frontend.py", "scripts/prepare_public_frontend.py"),
    ("infra/public_smoke.py", "public_smoke.py"),
    ("infra/public-entrypoint.sh", "infra/public-entrypoint.sh"),
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    require(stat.S_ISREG(path.lstat().st_mode), "inventory input must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ignored_context_path(path: Path) -> bool:
    # These exclusions match the Docker context rules for the copied trees.
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo", ".pyd"}


def file_inventory(root: Path, *, context: bool = False) -> dict[str, str]:
    require(stat.S_ISDIR(root.lstat().st_mode), "inventory root must be a directory")
    result: dict[str, str] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in names[:]:
            path = current / name
            if context and ignored_context_path(path.relative_to(root)):
                names.remove(name)
                continue
            require(stat.S_ISDIR(path.lstat().st_mode), "inventory cannot include linked directories")
        for name in files:
            path = current / name
            relative = path.relative_to(root)
            if context and ignored_context_path(relative):
                continue
            result[relative.as_posix()] = digest(path)
    require(len(result) <= 20000, "inventory exceeds file limit")
    return dict(sorted(result.items()))


def build_inventory(app_root: Path, source_commit: str) -> dict:
    require(COMMIT.fullmatch(source_commit) is not None, "invalid source commit")
    files: dict[str, str] = {}
    for source, destination in DIRECTORIES:
        for relative, value in file_inventory(app_root / source, context=True).items():
            target = str(PurePosixPath(destination) / relative)
            require(target not in files, "duplicate image destination")
            files[target] = value
    for source, target in FILES:
        require(target not in files, "duplicate image destination")
        files[target] = digest(app_root / source)
    identity = json.loads((app_root / ".build/public-ui/frontend-bundle-manifest.json").read_bytes())
    require(identity.get("source_commit") == source_commit, "frontend source commit differs")
    return {"schema": SCHEMA, "source_commit": source_commit, "files": dict(sorted(files.items()))}


def validate_manifest(manifest: dict) -> dict[str, str]:
    require(isinstance(manifest, dict) and set(manifest) == {"schema", "source_commit", "files"},
            "invalid inventory schema")
    require(manifest["schema"] == SCHEMA and isinstance(manifest["source_commit"], str)
            and COMMIT.fullmatch(manifest["source_commit"]) is not None, "invalid inventory identity")
    files = manifest["files"]
    require(isinstance(files, dict) and 1 <= len(files) <= 20000, "invalid inventory file count")
    for name, value in files.items():
        require(isinstance(name, str) and name and "\\" not in name
                and not name.startswith("/") and all(part not in {"", ".", ".."} for part in name.split("/")),
                "invalid inventory path")
        require(isinstance(value, str) and SHA.fullmatch(value) is not None, "invalid inventory digest")
    return files


def verify_inventory(image_root: Path, manifest: dict, revision: str | None) -> dict:
    expected = validate_manifest(manifest)
    require(revision == manifest["source_commit"], "image source commit differs")
    actual = file_inventory(image_root)
    require(actual.keys() == expected.keys(), "image contains missing or stale application files")
    require(actual == expected, "image application bytes differ")
    return {"status": "verified", "source_commit": manifest["source_commit"], "file_count": len(actual)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-source-commit")
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.app_root:
        require(args.output is not None and args.image_root is None and args.manifest is None,
                "invalid inventory creation arguments")
        source = args.expected_source_commit
        if source is None:
            source = subprocess.run(["git", "rev-parse", "HEAD"], cwd=args.app_root,
                                    check=True, text=True, capture_output=True).stdout.strip()
        manifest = build_inventory(args.app_root, source)
        validate_manifest(manifest)
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(manifest, output, sort_keys=True, indent=2)
            output.write("\n")
        print(json.dumps({"status": "prepared", "source_commit": source, "file_count": len(manifest["files"])}))
    else:
        require(args.image_root is not None and args.manifest is not None and args.output is None
                and args.expected_source_commit is None, "invalid inventory verification arguments")
        require(args.manifest.stat().st_size <= 8 * 1024**2, "inventory manifest exceeds byte limit")
        print(json.dumps(verify_inventory(args.image_root, json.loads(args.manifest.read_bytes()),
                                          os.getenv("APP_REVISION"))))


if __name__ == "__main__":
    main()
