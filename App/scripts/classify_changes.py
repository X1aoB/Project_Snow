"""Classify repository changes into CI risk tiers.

The command is intentionally dependency-free so the first CI job can run
before installing the application environment.
"""

from __future__ import annotations

import argparse
from fnmatch import fnmatch
import json
import os
from pathlib import Path
import subprocess
from typing import Iterable


CATEGORIES = ("ui", "api", "data", "embedding", "deploy")

DOC_PATTERNS = (
    "*.md",
    "docs/**",
    ".github/ISSUE_TEMPLATE/**",
    ".github/PULL_REQUEST_TEMPLATE*",
    "LICENSE*",
)

UI_PATTERNS = (
    "App/public_frontend/**",
    "App/frontend/**",
    "App/tests/test_ui_*.py",
    "App/tests/test_public_frontend_e2e.py",
)

API_PATTERNS = (
    "App/backend/snow_app/**",
    "App/config/public_knowledge/**",
    "App/migrations/**",
    "App/alembic.ini",
    "App/requirements-public.txt",
    "App/requirements.txt",
    "App/tests/test_public_*.py",
    "App/tests/test_application_layer.py",
    "App/tests/test_feedback_regressions.py",
    "App/tests/test_migration_secret_config.py",
)

DATA_PATTERNS = (
    "Data/**",
    "App/config/public_knowledge/data_release.json",
    "App/config/character_relationships.v1.json",
    "App/scripts/build_data_release.py",
    "App/scripts/export_publishable_graph.py",
    "App/scripts/validate_architecture.py",
    "App/scripts/verify_data_release.py",
    "App/backend/snow_app/data_loader.py",
    "App/backend/snow_app/data_release.py",
    "App/tests/test_data_*.py",
    "App/tests/test_graph_*.py",
)

EMBEDDING_PATTERNS = (
    "App/infra/embedding.Dockerfile",
    "App/infra/embedding_service.py",
)

DEPLOY_PATTERNS = (
    ".github/workflows/**",
    "App/compose*.yml",
    "App/infra/Caddyfile",
    "App/infra/egress-squid.conf",
    "App/infra/neo4j-entrypoint.sh",
    "App/infra/postgres/**",
    "App/infra/public-entrypoint.sh",
    "App/infra/public_smoke.py",
    "App/ops/**",
    "App/scripts/deploy.ps1",
    "App/scripts/rollback.ps1",
    "App/scripts/release_manifest.py",
    "App/scripts/validate_shared_design.py",
    "App/tests/test_deployment_contracts.py",
    "App/tests/test_shared_design.py",
)

APP_IMAGE_PATTERNS = (
    "App/backend/snow_app/**",
    "App/config/public_knowledge/**",
    "App/migrations/**",
    "App/alembic.ini",
    "App/public_frontend/**",
    # The public image copies these shared immersive assets. A change here
    # must build and smoke-test the image, not only run browser assertions.
    "App/frontend/shared/**",
    "App/frontend/assets/immersive/**",
    "App/requirements-public.txt",
    "App/infra/public-api.Dockerfile",
    "App/infra/public-entrypoint.sh",
    "App/infra/public_smoke.py",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch(path, pattern) for pattern in patterns)


def classify(paths: Iterable[str], *, force_full: bool = False) -> dict[str, bool]:
    normalized = sorted({str(path).replace("\\", "/").lstrip("./") for path in paths if path})
    result = {category: False for category in CATEGORIES}
    result.update({"docs_only": False, "app_image": False, "full": force_full})
    if not normalized:
        if force_full:
            result.update({category: True for category in CATEGORIES})
            result["app_image"] = True
        return result

    result["docs_only"] = all(_matches(path, DOC_PATTERNS) for path in normalized)
    for path in normalized:
        result["ui"] |= _matches(path, UI_PATTERNS)
        result["api"] |= _matches(path, API_PATTERNS)
        result["data"] |= _matches(path, DATA_PATTERNS)
        result["embedding"] |= _matches(path, EMBEDDING_PATTERNS)
        result["deploy"] |= _matches(path, DEPLOY_PATTERNS)
        result["app_image"] |= _matches(path, APP_IMAGE_PATTERNS)

        known = result["docs_only"] or any(
            _matches(path, patterns)
            for patterns in (
                UI_PATTERNS,
                API_PATTERNS,
                DATA_PATTERNS,
                EMBEDDING_PATTERNS,
                DEPLOY_PATTERNS,
            )
        )
        if not known:
            # Unknown source/config changes receive the conservative API and
            # application-image tier instead of silently skipping coverage.
            result["api"] = True
            result["app_image"] = True

    if force_full:
        result.update({category: True for category in CATEGORIES if category != "embedding"})
        result["app_image"] = True
    return result


def changed_files(base: str, head: str, repository: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, head, "--"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _write_github_output(values: dict[str, bool], output_path: str) -> None:
    with Path(output_path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={'true' if value else 'false'}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--event", default=os.getenv("GITHUB_EVENT_NAME", "local"))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--files", nargs="*")
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT", ""))
    args = parser.parse_args()

    paths = args.files
    if paths is None:
        if not args.base:
            parser.error("--base is required when --files is not provided")
        paths = changed_files(args.base, args.head, args.repository)

    force_full = args.event in {"push", "schedule", "workflow_dispatch"}
    values = classify(paths, force_full=force_full)
    if args.event in {"schedule", "workflow_dispatch"}:
        values["embedding"] = True
    if args.github_output:
        _write_github_output(values, args.github_output)
    print(json.dumps({"files": paths, **values}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
