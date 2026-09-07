"""Create the subject signed only by the successful-CI follow-up workflow."""

import argparse
import hashlib
import json
from pathlib import Path

from verify_release_proof import CI_WORKFLOW, REPOSITORY, SCHEMA, validate_bindings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    event = json.loads(args.event.read_text(encoding="utf-8"))
    run = event["workflow_run"]
    manifest_bytes = args.manifest.read_bytes()
    proof = {
        "schema_version": SCHEMA,
        "repository": REPOSITORY,
        "workflow": CI_WORKFLOW,
        "run_id": run["id"],
        "run_attempt": run["run_attempt"],
        "commit_sha": run["head_sha"],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    validate_bindings(manifest_bytes, proof, run["head_sha"], run)
    args.output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
