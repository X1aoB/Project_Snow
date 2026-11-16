"""Resolve only an explicitly approved shared embedding digest.

The configured digest selects a comparison/reuse baseline, never authorizes
promotion. Do not fall back to tags, the latest build, or a Git ancestor.
"""
from __future__ import annotations

import argparse
import re


def reference(owner: str, digest: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", owner):
        raise ValueError("A valid repository owner is required for the embedding baseline")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("An approved embedding baseline must be an exact sha256 digest")
    return f"ghcr.io/{owner.lower()}/project_snow-embedding@{digest}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--digest", required=True)
    args = parser.parse_args()
    try:
        resolved = reference(args.owner, args.digest)
    except ValueError as error:
        parser.error(str(error))
    print(resolved)


if __name__ == "__main__":
    main()
