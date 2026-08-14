from __future__ import annotations

from unittest import TestCase

from scripts.report_trivy_findings import _workflow_escape, collect_findings


class TrivyReportingTests(TestCase):
    def test_collect_findings_ignores_empty_results(self) -> None:
        self.assertEqual(collect_findings({"Results": [{"Target": "image", "Vulnerabilities": None}]}), [])

    def test_collect_findings_keeps_actionable_versions(self) -> None:
        report = {
            "Results": [
                {
                    "Target": "Python",
                    "Vulnerabilities": [
                        {
                            "VulnerabilityID": "CVE-2026-1234",
                            "PkgName": "example",
                            "InstalledVersion": "1.0.0",
                            "FixedVersion": "1.0.1",
                            "Severity": "HIGH",
                        }
                    ],
                }
            ]
        }

        self.assertEqual(
            collect_findings(report),
            [
                {
                    "target": "Python",
                    "id": "CVE-2026-1234",
                    "package": "example",
                    "installed": "1.0.0",
                    "fixed": "1.0.1",
                    "severity": "HIGH",
                }
            ],
        )

    def test_workflow_escape_prevents_command_injection(self) -> None:
        self.assertEqual(_workflow_escape("line%one\nline two\r"), "line%25one%0Aline two%0D")
