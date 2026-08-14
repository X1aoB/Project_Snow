from __future__ import annotations

import argparse
import json
from pathlib import Path


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose Trivy image findings as GitHub check annotations.")
    parser.add_argument("report", type=Path)
    parser.add_argument("--max-annotations", type=int, default=20)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    findings = collect_findings(report)
    if not findings:
        print("Trivy found no HIGH or CRITICAL vulnerabilities.")
        return 0

    for finding in findings[: max(args.max_annotations, 0)]:
        title = _workflow_escape(f"Trivy {finding['severity']} {finding['id']}")
        message = _workflow_escape(
            f"{finding['package']} {finding['installed']} in {finding['target']}; fixed in {finding['fixed']}"
        )
        print(f"::error title={title}::{message}")

    omitted = max(len(findings) - max(args.max_annotations, 0), 0)
    suffix = f" ({omitted} additional findings omitted from annotations)" if omitted else ""
    print(f"Trivy vulnerability gate failed with {len(findings)} finding(s){suffix}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
