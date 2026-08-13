"""Create a versioned, attributable data release manifest without raw Wiki media."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil


APP_ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def copy_file(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {"path": destination.name, "bytes": destination.stat().st_size, "sha256": digest(destination)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--runtime-root", type=Path, default=APP_ROOT / "runtime")
    parser.add_argument("--output-root", type=Path, default=APP_ROOT / "runtime" / "releases")
    parser.add_argument("--denylist", type=Path, default=APP_ROOT / "config" / "content_denylist.json")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    usage = shutil.disk_usage(runtime)
    used_ratio = (usage.total - usage.free) / usage.total
    if used_ratio >= 0.70 or usage.free < 12 * 1024**3:
        raise SystemExit(
            f"refusing data release: disk used={used_ratio:.1%}, free={usage.free / 1024**3:.1f} GiB"
        )
    output = (args.output_root / args.version).resolve()
    output.mkdir(parents=True, exist_ok=False)

    required = {
        "fts5.sqlite3": runtime / "indexes" / "lexical.sqlite3",
        "graph_nodes.jsonl": runtime / "release" / "graph" / "nodes.jsonl",
        "graph_edges.jsonl": runtime / "release" / "graph" / "edges.jsonl",
        "persona_profiles.jsonl": runtime / "personas" / "persona_profiles.jsonl",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise SystemExit("missing release artifacts: " + ", ".join(missing))
    files = [copy_file(source, output / name) for name, source in required.items()]
    denylist = json.loads(args.denylist.read_text(encoding="utf-8")) if args.denylist.exists() else {}
    license_manifest = {
        "code": {"license": "GPL-3.0-only", "scope": "application source code"},
        "content": {
            "license": "CC BY-NC-SA",
            "version": "version unspecified by source",
            "policy": "Page-specific notices and denylist entries take precedence.",
        },
        "denylist": denylist,
    }
    (output / "LICENSES.json").write_text(
        json.dumps(license_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files.append({"path": "LICENSES.json", "bytes": (output / "LICENSES.json").stat().st_size, "sha256": digest(output / "LICENSES.json")})
    manifest = {
        "schema_version": "project-snow-data-release-1",
        "data_version": args.version,
        "generated_at": datetime.now(UTC).isoformat(),
        "contains_raw_wiki_media": False,
        "files": sorted(files, key=lambda item: item["path"]),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
