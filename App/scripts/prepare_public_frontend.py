"""Build one immutable public UI from committed inputs; verify without Git in images.

S1 selects baseline + original compatibility patch + the separately hashed r1
delta. S2 is a normal main commit changing config/public_frontend_release.json.
All source bytes come from Git objects at HEAD, never untracked/modified assets.
Fingerprints are finalized before the identity is calculated. No runtime switch.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

BASELINE = "502ec99412bef843c37e4b31a53df8fa9faeb33c"
ORIGINAL_PATCH_SHA = "351c868c4b047f1775664ac964f6abb624caeba5df7b720c4af1641a66887d2d"
STATIC_ROOTS = ("public_frontend", "frontend/shared", "frontend/assets/immersive")
IDENTITY_FILE = "frontend-identity.json"
MANIFEST_FILE = "frontend-bundle-manifest.json"
SELECTOR = "config/public_frontend_release.json"
FINGERPRINT = "scripts/fingerprint_public_frontend.py"
SCHEMA = "project-snow-frontend-identity-1"
BUNDLE_SCHEMA = "project-snow-public-frontend-bundle-1"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _json(payload: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "duplicate JSON field")
            result[key] = value
        return result

    value = json.loads(payload, object_pairs_hook=unique)
    _require(isinstance(value, dict), "expected JSON object")
    return value


def _git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", *arguments],
        cwd=repo,
        input=data,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise ValueError("Git input unavailable or patch rejected; full baseline history is required")
    return result.stdout


def _blob(repo: Path, commit: str, path: str) -> bytes:
    return _git(repo, "show", f"{commit}:{path}")


def _path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(
        value
        and not path.is_absolute()
        and path.as_posix() == value
        and "\\" not in value
        and not any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _export(repo: Path, commit: str, destination: Path, paths: tuple[str, ...]) -> None:
    entries = []
    for row in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths).split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")
        _require(mode in {"100644", "100755"} and kind == "blob" and _path(path), "non-regular Git asset")
        entries.append((path, oid))
    _require(bool(entries), "empty static source tree")
    stream = io.BytesIO(
        _git(repo, "cat-file", "--batch", data="".join(oid + "\n" for _, oid in entries).encode())
    )
    for path, oid in entries:
        actual, kind, length = stream.readline().decode("ascii").strip().split()
        size = int(length)
        _require(actual == oid and kind == "blob" and 0 <= size <= 32 * 1024 * 1024, "invalid Git blob")
        payload = stream.read(size)
        _require(len(payload) == size and stream.read(1) == b"\n", "truncated Git blob")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def _patch(repo: Path, commit: str, workspace: Path, manifest_name: str, *, delta: bool) -> dict:
    manifest = _json(_blob(repo, commit, "App/compat/" + manifest_name))
    schema = "project-snow-public-rollback-delta-1" if delta else "project-snow-public-rollback-1"
    _require(manifest.get("schema") == schema, "unsupported compatibility manifest")
    filename = manifest.get("patch_file", "")
    _require(
        isinstance(filename, str) and bool(re.fullmatch(r"public-0\.9\.6(?:-r1)?\.patch", filename)),
        "invalid compatibility patch name",
    )
    payload = _blob(repo, commit, "App/compat/" + filename)
    _require(_sha(payload) == manifest.get("patch_sha256"), "compatibility patch hash mismatch")
    if delta:
        _require(manifest.get("parent_patch_sha256") == ORIGINAL_PATCH_SHA, "delta parent mismatch")
        _require(
            bool(re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_fix_commit", ""))),
            "invalid delta source commit",
        )
    else:
        _require(
            manifest.get("baseline_commit") == BASELINE and _sha(payload) == ORIGINAL_PATCH_SHA,
            "immutable baseline/patch mismatch",
        )
    before_key, after_key = (
        ("before_sha256", "after_sha256") if delta else ("baseline_sha256", "compatible_sha256")
    )
    files = manifest.get("files", [])
    _require(
        bool(files) and len({item["path"] for item in files}) == len(files), "invalid compatibility file list"
    )
    for item in files:
        path = item["path"]
        _require(
            _path(path) and (path.startswith("App/public_frontend/") or path == "App/" + FINGERPRINT),
            "patch path outside allowed scope",
        )
        target = workspace / path
        expected = item[before_key]
        _require(
            (not target.exists())
            if expected is None
            else target.is_file() and _sha(target.read_bytes()) == expected,
            "compatibility input hash mismatch",
        )
    patch_path = workspace / "input.patch"
    patch_path.write_bytes(payload)
    changed = []
    for row in _git(workspace, "apply", "--numstat", "-z", str(patch_path)).split(b"\0"):
        if not row:
            continue
        columns = row.split(b"\t", 2)
        _require(len(columns) == 3 and bool(columns[2]), "unsupported patch rename/path")
        changed.append(columns[2].decode("utf-8"))
    _require(
        sorted(changed) == sorted(item["path"] for item in files),
        "patch paths differ from declared manifest files",
    )
    _git(workspace, "apply", "--check", str(patch_path))
    _git(workspace, "apply", str(patch_path))
    for item in files:
        _require(
            _sha((workspace / item["path"]).read_bytes()) == item[after_key],
            "compatibility output hash mismatch",
        )
    return {
        "manifest_sha256": _sha(_blob(repo, commit, "App/compat/" + manifest_name)),
        "patch_sha256": _sha(payload),
        **({"source_fix_commit": manifest["source_fix_commit"]} if delta else {"baseline_commit": BASELINE}),
    }


def _inventory(root: Path) -> list[dict]:
    _require(root.is_dir() and not root.is_symlink(), "invalid bundle root")
    files = []
    for relative in STATIC_ROOTS:
        directory = root / relative
        _require(
            not any(
                parent.is_symlink()
                for parent in directory.parents
                if parent != root and parent.is_relative_to(root)
            ),
            "symlinked static ancestor",
        )
        _require(directory.is_dir() and not directory.is_symlink(), "missing or symlinked static root")
        for path in sorted(directory.rglob("*")):
            _require(not path.is_symlink(), "symlink in static bundle")
            if path.is_dir():
                continue
            _require(path.is_file(), "non-regular static file")
            payload = path.read_bytes()
            files.append(
                {"path": path.relative_to(root).as_posix(), "size": len(payload), "sha256": _sha(payload)}
            )
    return sorted(files, key=lambda item: item["path"])


def _identity(value: dict) -> None:
    _require(set(value) == {"schema_version", "track", "version", "bundle_sha256"}, "invalid identity fields")
    _require(
        value["schema_version"] == SCHEMA and value["track"] in {"compat", "current"},
        "invalid identity schema/track",
    )
    _require(
        isinstance(value["version"], str)
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value["version"])),
        "invalid frontend version",
    )
    _require(
        isinstance(value["bundle_sha256"], str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", value["bundle_sha256"])),
        "invalid bundle digest",
    )


def verify_bundle(output: Path, *, expected_source_commit: str | None = None) -> dict:
    """Validate the three exact static trees and image identity; no Git needed."""
    output = Path(output)
    for name in (IDENTITY_FILE, MANIFEST_FILE):
        _require(
            (output / name).is_file() and not (output / name).is_symlink(),
            "missing or symlinked bundle metadata",
        )
    identity = _json((output / IDENTITY_FILE).read_bytes())
    _identity(identity)
    manifest = _json((output / MANIFEST_FILE).read_bytes())
    _require(
        set(manifest) == {"schema_version", "source_commit", "edition", "inputs", "identity", "files"},
        "invalid bundle manifest fields",
    )
    _require(
        manifest["schema_version"] == BUNDLE_SCHEMA and manifest["identity"] == identity,
        "bundle metadata identity mismatch",
    )
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", ""))), "invalid source commit")
    if expected_source_commit is not None:
        _require(
            manifest["source_commit"] == expected_source_commit, "bundle belongs to another source commit"
        )
    _require(
        manifest["edition"] == ("compat-096" if identity["track"] == "compat" else "current"),
        "edition/track mismatch",
    )
    inputs = manifest["inputs"]
    compat = identity["track"] == "compat"
    _require(
        isinstance(inputs, dict)
        and set(inputs)
        == (
            {"static_commit", "selector_sha256", "compatibility", "delta"}
            if compat
            else {"static_commit", "selector_sha256"}
        ),
        "invalid bundle provenance fields",
    )
    _require(
        inputs["static_commit"] == (BASELINE if compat else manifest["source_commit"]),
        "static source commit mismatch",
    )
    _require(
        isinstance(inputs["selector_sha256"], str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", inputs["selector_sha256"])),
        "invalid selector digest",
    )
    if compat:
        for key, source_key in (("compatibility", "baseline_commit"), ("delta", "source_fix_commit")):
            item = inputs[key]
            _require(
                isinstance(item, dict) and set(item) == {"manifest_sha256", "patch_sha256", source_key},
                "invalid patch provenance fields",
            )
            _require(
                all(
                    isinstance(item[field], str) and bool(re.fullmatch(r"[0-9a-f]{64}", item[field]))
                    for field in ("manifest_sha256", "patch_sha256")
                ),
                "invalid patch provenance digest",
            )
            _require(
                isinstance(item[source_key], str) and bool(re.fullmatch(r"[0-9a-f]{40}", item[source_key])),
                "invalid patch source commit",
            )
        _require(
            inputs["compatibility"]["baseline_commit"] == BASELINE
            and inputs["compatibility"]["patch_sha256"] == ORIGINAL_PATCH_SHA,
            "original compatibility provenance mismatch",
        )
    files = _inventory(output)
    _require(files == manifest["files"], "bundle file inventory/hash mismatch")
    _require(_sha(_canonical(files)) == identity["bundle_sha256"], "bundle digest mismatch")
    return identity


def prepare(app_root: Path, output: Path) -> dict:
    """Build committed selector at checkout HEAD. Output must not already exist."""
    app_root, output = Path(app_root).resolve(), Path(output).absolute()
    repo = Path(_git(app_root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    _require(app_root == repo / "App", "App root must belong to the trusted checkout")
    source_commit = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", source_commit)), "invalid source commit")
    _require(not output.exists() and not output.is_symlink(), "bundle output already exists")
    config = _json(_blob(repo, source_commit, "App/" + SELECTOR))
    _require(
        set(config) == {"schema_version", "edition", "compat_version", "current_version"},
        "invalid UI selector fields",
    )
    _require(
        config["schema_version"] == "project-snow-public-frontend-release-1"
        and config["edition"] in {"compat-096", "current"},
        "invalid UI selector",
    )
    compat = config["edition"] == "compat-096"
    version = config["compat_version"] if compat else config["current_version"]
    identity = {
        "schema_version": SCHEMA,
        "track": "compat" if compat else "current",
        "version": version,
        "bundle_sha256": "0" * 64,
    }
    _identity(identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".public-ui-build-", dir=output.parent) as temporary:
        workspace = Path(temporary)
        # A nested, throwaway Git root makes patch target paths independent of
        # the caller's checkout. Never apply compatibility changes to the source.
        _git(workspace, "init", "--quiet")
        static_commit = BASELINE if compat else source_commit
        _export(
            repo,
            static_commit,
            workspace,
            tuple("App/" + path for path in STATIC_ROOTS) + ("App/" + FINGERPRINT,),
        )
        inputs = {
            "static_commit": static_commit,
            "selector_sha256": _sha(_blob(repo, source_commit, "App/" + SELECTOR)),
        }
        if compat:
            inputs["compatibility"] = _patch(
                repo, source_commit, workspace, "public-0.9.6.manifest.json", delta=False
            )
            delta_manifest = _json(_blob(repo, source_commit, "App/compat/public-0.9.6-r1.manifest.json"))
            _require(delta_manifest.get("version") == version, "compatibility delta version mismatch")
            inputs["delta"] = _patch(
                repo, source_commit, workspace, "public-0.9.6-r1.manifest.json", delta=True
            )
        selected = workspace / "App"
        subprocess.run(
            [sys.executable, "-I", str(selected / FINGERPRINT), "--app-root", str(selected)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        bundle = workspace / "bundle"
        bundle.mkdir()
        for relative in STATIC_ROOTS:
            (bundle / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(selected / relative, bundle / relative)
        files = _inventory(bundle)
        identity["bundle_sha256"] = _sha(_canonical(files))
        manifest = {
            "schema_version": BUNDLE_SCHEMA,
            "source_commit": source_commit,
            "edition": config["edition"],
            "inputs": inputs,
            "identity": identity,
            "files": files,
        }
        (bundle / IDENTITY_FILE).write_bytes(_canonical(identity) + b"\n")
        (bundle / MANIFEST_FILE).write_bytes(_canonical(manifest) + b"\n")
        verify_bundle(bundle, expected_source_commit=source_commit)
        bundle.rename(output)
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--output", type=Path, help="default: <app-root>/.build/public-ui; must not exist when preparing"
    )
    parser.add_argument("--verify", action="store_true", help="verify copied bundle without rebuilding/Git")
    parser.add_argument(
        "--expected-source-commit", help="verify provenance; defaults to nonempty APP_REVISION"
    )
    args = parser.parse_args()
    output = args.output or args.app_root / ".build/public-ui"
    try:
        result = (
            verify_bundle(
                output,
                expected_source_commit=args.expected_source_commit or os.getenv("APP_REVISION") or None,
            )
            if args.verify
            else prepare(args.app_root, output)
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"public frontend bundle failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
