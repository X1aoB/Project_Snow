"""Conservative Snow-only Docker image cleanup; inventory first, apply exact plan.

Run as root on the release host. Never prunes containers, networks, volumes or
unknown repositories. Revalidates every deletion against all container references
and release/configuration pins under the release lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

REPOSITORIES = {"ghcr.io/x1aob/project_snow-public", "ghcr.io/x1aob/project_snow-embedding"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def referenced_digests(roots: list[Path]) -> set[str]:
    protected: set[str] = set()
    for root in roots:
        if not root.is_dir():
            raise RuntimeError(f"Missing protection root: {root}")
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            if path.suffix in {"", ".json", ".env"}:
                protected.update(DIGEST.findall(path.read_text(encoding="utf-8", errors="replace")))
    return protected


def select_candidates(images: list[dict], container_images: set[str], protected: set[str]) -> list[dict]:
    candidates = []
    for item in images:
        image_id = item["Id"]
        refs = (item.get("RepoTags") or []) + (item.get("RepoDigests") or [])
        repositories = {
            ref.split("@")[0].rsplit(":", 1)[0] if "@" not in ref else ref.split("@")[0] for ref in refs
        }
        digests = {image_id, *(digest for ref in refs for digest in DIGEST.findall(ref))}
        if not refs or not repositories or not repositories.issubset(REPOSITORIES):
            continue
        if image_id in container_images or digests & protected:
            continue
        candidates.append({"id": image_id, "references": sorted(refs), "size_bytes": item.get("Size", 0)})
    return sorted(candidates, key=lambda item: item["id"])


def inventory(root: Path) -> dict:
    ids = sorted(set(command("docker", "image", "ls", "-aq", "--no-trunc").split()))
    images = json.loads(command("docker", "image", "inspect", *ids)) if ids else []
    containers = command("docker", "ps", "-aq", "--no-trunc").split()
    container_state = json.loads(command("docker", "inspect", *containers)) if containers else []
    used = {
        digest
        for item in container_state
        for value in (item["Image"], item.get("Config", {}).get("Image", ""))
        for digest in DIGEST.findall(value)
    }
    # The containerd image store may report several OCI indexes sharing a
    # runnable manifest. Protect Docker's own reference count as well as the
    # container's selected index; deleting by an alternate index is unsafe.
    for line in command(
        "docker", "image", "ls", "--no-trunc", "--format", "{{.ID}}={{.Containers}}"
    ).splitlines():
        identifier, count = line.split("=", 1)
        if int(count) > 0:
            used.add(identifier)
    protected = referenced_digests([root / "releases", root / "runtime"])
    result = {
        "schema_version": 1,
        "root": str(root),
        "protected_digests": sorted(protected | used),
        "candidates": select_candidates(images, used, protected),
    }
    result["plan_sha256"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/project-snow"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    args = parser.parse_args()
    import fcntl

    with Path("/run/lock/project-snow-release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = inventory(args.root)
        if args.apply_plan:
            approved = json.loads(args.apply_plan.read_text(encoding="utf-8"))
            digest = approved.pop("plan_sha256", None)
            if hashlib.sha256(json.dumps(approved, sort_keys=True).encode()).hexdigest() != digest:
                raise RuntimeError("Invalid plan checksum")
            if approved.get("root") != str(args.root):
                raise RuntimeError("Plan root does not match")
            allowed = {item["id"] for item in current["candidates"]}
            selected = [item["id"] for item in approved["candidates"]]
            if not set(selected).issubset(allowed):
                raise RuntimeError("Image references changed; generate and review a fresh plan")
            for image_id in selected:
                # No force: Docker must refuse an image that gained a container reference.
                command("docker", "image", "rm", image_id)
                print(json.dumps({"deleted_image_id": image_id}), flush=True)
        elif args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                json.dump(current, handle, indent=2)
            print(
                json.dumps(
                    {
                        "plan": str(args.output),
                        "plan_sha256": current["plan_sha256"],
                        "candidate_count": len(current["candidates"]),
                        "protected_count": len(current["protected_digests"]),
                    }
                )
            )
        else:
            print(json.dumps(current, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
