from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import TestCase

from scripts.report_trivy_findings import _workflow_escape, collect_findings

APP = Path(__file__).resolve().parents[1]
SCRIPT = APP / "scripts" / "report_trivy_findings.py"


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


DIGEST = "sha256:" + "ab" * 32
EXCEPTION = {"artifact_digest": DIGEST, "package": "certifi", "installed": "2026.7.22", "fixed": "2026.10.19",
             "advisory": "GHSA-test", "expires": "2026-11-04", "reason": "synthetic", "approved": "synthetic"}


class TrivyExceptionTests(TestCase):
    def run_gate(self, *, digest=DIGEST, installed="2026.7.22", today="2026-10-21", entries=None, exceptions=True):
        report = {"ArtifactName": "ghcr.io/example/embedding@" + digest, "Results": [{"Target": "Python", "Vulnerabilities": [
            {"VulnerabilityID": "GHSA-test", "PkgName": "certifi", "InstalledVersion": installed,
             "FixedVersion": "2026.10.19", "Severity": "CRITICAL"}]}]}
        with tempfile.TemporaryDirectory() as directory:
            report_path, exception_path = Path(directory) / "trivy.json", Path(directory) / "exceptions.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            exception_path.write_text(json.dumps({"schema": "project-snow-trivy-exceptions-1",
                                                  "exceptions": [EXCEPTION] if entries is None else entries}), encoding="utf-8")
            command = [sys.executable, str(SCRIPT), str(report_path), "--today", today]
            if exceptions:
                command += ["--exceptions", str(exception_path)]
            return subprocess.run(command, capture_output=True, text=True, timeout=30)

    def test_exact_unexpired_exception_waives_with_a_warning(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("::warning title=Trivy waived CRITICAL GHSA-test::", result.stdout)
        self.assertIn("passed with 1 reviewed exception", result.stdout)

    def test_exception_never_applies_to_another_image_version_or_after_expiry(self) -> None:
        for case in ({"digest": "sha256:" + "cd" * 32}, {"installed": "2026.7.21"}, {"today": "2026-11-05"}, {"exceptions": False}):
            with self.subTest(case=case):
                result = self.run_gate(**case)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("Trivy vulnerability gate failed with 1 finding(s).", result.stdout)
        self.assertIn("exception expired 2026-11-04", self.run_gate(today="2026-11-05").stdout)

    def test_malformed_exception_file_is_rejected_not_skipped(self) -> None:
        for broken in ({**EXCEPTION, "artifact_digest": "latest"}, {k: v for k, v in EXCEPTION.items() if k != "reason"},
                       {**EXCEPTION, "expires": "soon"}, {**EXCEPTION, "extra": "x"}):
            with self.subTest(broken=sorted(broken)):
                result = self.run_gate(entries=[broken])
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("passed", result.stdout)

    def test_only_the_reused_embedding_gate_reads_the_reviewed_exceptions(self) -> None:
        workflows = APP.parent / ".github" / "workflows"
        for name in ("ci.yml", "publish-images.yml", "nightly-image-security.yml"):
            text = (workflows / name).read_text(encoding="utf-8")
            for line in text.splitlines():
                if "report_trivy_findings.py" in line and "--exceptions" in line:
                    self.assertEqual(name, "publish-images.yml", line)
                    self.assertIn("trivy-embedding.json --exceptions App/config/trivy-exceptions.json", line)
        payload = json.loads((APP / "config" / "trivy-exceptions.json").read_text(encoding="utf-8"))
        for entry in payload["exceptions"]:
            self.assertFalse(entry["approved"].lower().startswith("pending"), "an exception must be approved before merge")
