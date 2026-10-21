from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
EXCEPTION_KEYS = {"artifact_digest", "package", "installed", "fixed", "advisory", "expires", "reason", "approved"}


def _workflow_escape(value: object) -> str:
    return str(value or "").replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def collect_findings(report: dict[str, object]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for result in report.get("Results", []) or []:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target") or "container image")
        for vulnerability in result.get("Vulnerabilities", []) or []:
            if not isinstance(vulnerability, dict):
                continue
            findings.append(
                {
                    "target": target,
                    "id": str(vulnerability.get("VulnerabilityID") or "UNKNOWN"),
                    "package": str(vulnerability.get("PkgName") or "unknown-package"),
                    "installed": str(vulnerability.get("InstalledVersion") or "unknown"),
                    "fixed": str(vulnerability.get("FixedVersion") or "not-published"),
                    "severity": str(vulnerability.get("Severity") or "UNKNOWN"),
                }
            )
    return findings


def artifact_digests(report: dict[str, object]) -> set[str]:
    """Digests the scanned artifact is known by: the digest-pinned reference and its repo digests."""
    names = [report.get("ArtifactName")]
    metadata = report.get("Metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("RepoDigests"), list):
        names.extend(metadata["RepoDigests"])
    return {match for name in names if isinstance(name, str) for match in DIGEST.findall(name)}


def load_exceptions(path: Path) -> list[dict[str, str]]:
    """A reviewed, expiring list; any malformed entry rejects the whole file instead of being skipped."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "project-snow-trivy-exceptions-1":
        raise ValueError("unsupported exception file")
    entries = payload.get("exceptions")
    if not isinstance(entries, list):
        raise ValueError("exceptions must be a list")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != EXCEPTION_KEYS or not all(isinstance(entry[k], str) and entry[k].strip() for k in EXCEPTION_KEYS):
            raise ValueError("each exception needs exactly the reviewed fields")
        if not DIGEST.fullmatch(entry["artifact_digest"]):
            raise ValueError("exceptions are bound to one exact image digest")
        date.fromisoformat(entry["expires"])
    return entries


def match_exception(finding: dict[str, str], exceptions: list[dict[str, str]], digests: set[str]) -> dict[str, str] | None:
    """Only the exact package version in the exact scanned image qualifies; the vulnerability ID is not trusted alone."""
    for entry in exceptions:
        if (entry["artifact_digest"] in digests and entry["package"] == finding["package"]
                and entry["installed"] == finding["installed"] and entry["fixed"] == finding["fixed"]):
            return entry
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose Trivy image findings as GitHub check annotations.")
    parser.add_argument("report", type=Path)
    parser.add_argument("--max-annotations", type=int, default=20)
    parser.add_argument("--exceptions", type=Path, help="Reviewed, expiring, digest-bound exception list")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    findings = collect_findings(report)
    if not findings:
        print("Trivy found no HIGH or CRITICAL vulnerabilities.")
        return 0

    today = args.today or datetime.now(timezone.utc).date()
    exceptions = load_exceptions(args.exceptions) if args.exceptions else []
    digests = artifact_digests(report)
    blocking, waived = [], []
    for finding in findings:
        entry = match_exception(finding, exceptions, digests)
        if entry and today <= date.fromisoformat(entry["expires"]):
            waived.append((finding, entry))
        else:
            if entry:
                finding = {**finding, "expired_exception": entry["expires"]}
            blocking.append(finding)

    for finding, entry in waived:
        title = _workflow_escape(f"Trivy waived {finding['severity']} {finding['id']}")
        message = _workflow_escape(
            f"{finding['package']} {finding['installed']} in {finding['target']} is waived until {entry['expires']} "
            f"({entry['advisory']}): {entry['reason']}"
        )
        print(f"::warning title={title}::{message}")

    if not blocking:
        print(f"Trivy vulnerability gate passed with {len(waived)} reviewed exception(s).")
        return 0

    for finding in blocking[: max(args.max_annotations, 0)]:
        title = _workflow_escape(f"Trivy {finding['severity']} {finding['id']}")
        expired = f" (exception expired {finding['expired_exception']})" if finding.get("expired_exception") else ""
        message = _workflow_escape(
            f"{finding['package']} {finding['installed']} in {finding['target']}; fixed in {finding['fixed']}{expired}"
        )
        print(f"::error title={title}::{message}")

    omitted = max(len(blocking) - max(args.max_annotations, 0), 0)
    suffix = f" ({omitted} additional findings omitted from annotations)" if omitted else ""
    print(f"Trivy vulnerability gate failed with {len(blocking)} finding(s){suffix}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
