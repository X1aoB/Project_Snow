"""Create and validate immutable Project Snow deployment manifests."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(
    r'^revision(?:\s*:\s*str)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)
DOWN_REVISION_PATTERN = re.compile(
    r'^down_revision(?:\s*:\s*(?:str\s*\|\s*None|Union\[[^\]]+\]))?\s*=\s*(.+)$',
    re.MULTILINE,
)


def read_public_versions(app_root: Path) -> tuple[str, str]:
    public_env = app_root / "ops" / "public.env.example"
    values: dict[str, str] = {}
    for raw_line in public_env.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    app_version = values.get("PUBLIC_APP_VERSION", "")
    data_pointer = json.loads(
        (app_root / "config" / "public_knowledge" / "data_release.json").read_text(
            encoding="utf-8"
        )
    )
    data_version = str(data_pointer.get("data_version") or "")
    if not app_version or not data_version:
        raise ValueError("public environment must define application and data versions")
    return app_version, data_version


def migration_heads(versions_directory: Path) -> list[str]:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions_directory.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        revision_match = REVISION_PATTERN.search(source)
        if not revision_match:
            continue
        revisions.add(revision_match.group(1))
        down_match = DOWN_REVISION_PATTERN.search(source)
        if not down_match:
            continue
        value = down_match.group(1).strip()
        if value == "None":
            continue
        parents.update(re.findall(r'["\']([^"\']+)["\']', value))
    heads = sorted(revisions - parents)
    if not heads:
        raise ValueError("no Alembic migration head found")
    return heads


def create_manifest(
    *,
    commit_sha: str,
    public_image: str,
    public_digest: str,
    embedding_image: str,
    embedding_digest: str,
    app_root: Path,
) -> dict[str, Any]:
    if not SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("commit SHA must contain 40 lowercase hexadecimal characters")
    for digest in (public_digest, embedding_digest):
        if not DIGEST_PATTERN.fullmatch(digest):
            raise ValueError("image digests must use sha256:<64 lowercase hexadecimal characters>")
    app_version, data_version = read_public_versions(app_root)
    return {
        "schema_version": "project-snow-release-1",
        "commit_sha": commit_sha,
        "app_version": app_version,
        "data_version": data_version,
        "migration_heads": migration_heads(app_root / "migrations" / "versions"),
        "application": {"image": public_image, "digest": public_digest},
        "embedding": {"image": embedding_image, "digest": embedding_digest},
        "generated_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--public-image", required=True)
    parser.add_argument("--public-digest", required=True)
    parser.add_argument("--embedding-image", required=True)
    parser.add_argument("--embedding-digest", required=True)
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = create_manifest(
        commit_sha=args.sha,
        public_image=args.public_image,
        public_digest=args.public_digest,
        embedding_image=args.embedding_image,
        embedding_digest=args.embedding_digest,
        app_root=args.app_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
