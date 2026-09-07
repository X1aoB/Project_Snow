"""Fail closed unless a signed proof binds this manifest to a successful main CI.

The unsigned inbox and image tags are not a trust source. GitHub's Sigstore
attestation signs the proof; the proof binds every manifest byte, and GitHub's
live API independently confirms the exact required-checks attempt succeeded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

REPOSITORY = "X1aoB/Project_Snow"
CI_WORKFLOW = ".github/workflows/ci.yml"
SIGNER_WORKFLOW = ".github/workflows/release-proof.yml"
SCHEMA = "project-snow-release-proof-1"


def github_json(path: str) -> dict:
    request = Request(
        "https://api.github.com/" + path,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "project-snow-release-verifier"},
    )
    with urlopen(request, timeout=20) as response:
        body = response.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("GitHub verification response exceeds limit")
    return json.loads(body)


def validate_bindings(manifest_bytes: bytes, proof: dict, expected_sha: str, run: dict) -> None:
    if not re.fullmatch("[0-9a-f]{40}", expected_sha):
        raise ValueError("Invalid expected commit")
    manifest = json.loads(manifest_bytes)
    if (
        proof.get("schema_version") != SCHEMA
        or proof.get("repository") != REPOSITORY
        or proof.get("workflow") != CI_WORKFLOW
        or proof.get("commit_sha") != expected_sha
        or manifest.get("commit_sha") != expected_sha
        or proof.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        raise ValueError("Release proof does not bind the requested manifest")
    if (
        type(proof.get("run_id")) is not int
        or type(proof.get("run_attempt")) is not int
        or proof["run_id"] <= 0
        or proof["run_attempt"] <= 0
    ):
        raise ValueError("Invalid workflow identity")
    if (
        run.get("id") != proof["run_id"]
        or run.get("run_attempt") != proof["run_attempt"]
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_sha") != expected_sha
        or run.get("head_branch") != "main"
        or run.get("event") != "push"
        or run.get("path") != CI_WORKFLOW
        or run.get("repository", {}).get("full_name") != REPOSITORY
        or run.get("head_repository", {}).get("full_name") != REPOSITORY
    ):
        raise ValueError("Required main CI attempt is not successful or has a different identity")


def verify(
    manifest: Path, proof_path: Path, expected_sha: str, fetch=github_json, execute=subprocess.run
) -> dict:
    manifest_bytes = manifest.read_bytes()
    proof_bytes = proof_path.read_bytes()
    if len(proof_bytes) > 16384 or len(manifest_bytes) > 1024 * 1024:
        raise ValueError("Oversized release metadata")
    proof = json.loads(proof_bytes)
    # Validate numeric identifiers before constructing any request path.
    for field in ("run_id", "run_attempt"):
        if type(proof.get(field)) is not int or proof[field] <= 0:
            raise ValueError("Invalid CI run identity")
    run = fetch(f"repos/{REPOSITORY}/actions/runs/{proof['run_id']}/attempts/{proof['run_attempt']}")
    validate_bindings(manifest_bytes, proof, expected_sha, run)
    latest = fetch(f"repos/{REPOSITORY}/actions/runs/{proof['run_id']}")
    # A later failed/in-progress rerun revokes candidate preparation even when
    # an earlier attempt succeeded. Keep the first immutable signed candidate
    # after a successful rerun; changed artifacts require a new commit.
    latest_proof = {**proof, "run_attempt": latest.get("run_attempt")}
    validate_bindings(manifest_bytes, latest_proof, expected_sha, latest)
    if latest_proof["run_attempt"] < proof["run_attempt"]:
        raise ValueError("CI run history predates the signed attempt")
    digest = hashlib.sha256(proof_bytes).hexdigest()
    attestations = fetch(f"repos/{REPOSITORY}/attestations/sha256:{digest}").get("attestations")
    if not isinstance(attestations, list) or not attestations:
        raise ValueError("No signed release proof exists")
    # A local bundle avoids depending on an authenticated gh profile on the
    # production host. Trust roots and certificate policies remain gh's job.
    with tempfile.TemporaryDirectory(prefix="snow-proof-") as directory:
        bundle = Path(directory) / "attestations.jsonl"
        bundle.write_text(
            "".join(json.dumps(item["bundle"]) + "\n" for item in attestations), encoding="utf-8"
        )
        result = execute(
            [
                "gh",
                "attestation",
                "verify",
                str(proof_path),
                "--bundle",
                str(bundle),
                "--repo",
                REPOSITORY,
                "--signer-workflow",
                f"{REPOSITORY}/{SIGNER_WORKFLOW}",
                "--source-ref",
                "refs/heads/main",
                "--source-digest",
                expected_sha,
                "--signer-digest",
                expected_sha,
                "--deny-self-hosted-runners",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
        verified = json.loads(result.stdout)
        if not isinstance(verified, list) or not verified:
            raise ValueError("No attestation passed the release signing policy")
    return {
        "status": "verified",
        "commit_sha": expected_sha,
        "run_id": proof["run_id"],
        "run_attempt": proof["run_attempt"],
        "manifest_sha256": proof["manifest_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.manifest, args.proof, args.expected_sha), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
