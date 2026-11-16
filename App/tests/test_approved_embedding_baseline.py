"""Approved embedding reuse is separate from CI scope and unpublished builds."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import yaml

from scripts.approved_embedding_baseline import reference


ROOT = Path(__file__).resolve().parents[2]
APPROVED = "sha256:" + "a" * 64
UNPROMOTED = "sha256:" + "b" * 64
FINGERPRINT = "c" * 64


class ApprovedEmbeddingBaselineTests(TestCase):
    def workflows(self):
        directory = ROOT / ".github" / "workflows"
        return tuple(yaml.safe_load((directory / name).read_text(encoding="utf-8"))
                     for name in ("ci.yml", "publish-images.yml"))

    def test_only_fixed_repository_image_and_complete_digest_are_accepted(self):
        self.assertEqual(reference("SyntheticOwner", APPROVED),
                         "ghcr.io/syntheticowner/project_snow-embedding@" + APPROVED)
        for digest in ("", "latest", "a" * 40, APPROVED + "\n", "sha256:" + "A" * 64,
                       "ghcr.io/other/image@" + APPROVED, APPROVED + " --other"):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                reference("SyntheticOwner", digest)
        for owner in ("", "other/image", "synthetic\nowner", "-owner", "owner;echo"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                reference(owner, APPROVED)

    def test_workflows_keep_validation_but_never_use_its_boolean_as_refresh_authority(self):
        ci, release = self.workflows()
        inputs = release.get("on", release.get(True))["workflow_call"]["inputs"]
        self.assertEqual(inputs["embedding_baseline_digest"], {"required": True, "type": "string"})
        self.assertFalse(inputs["force_rebuild"]["default"])
        self.assertFalse(inputs["force_rebuild"]["required"])
        self.assertEqual(ci["jobs"]["release"]["with"], {
            "embedding_baseline_digest": "${{ vars.PROJECT_SNOW_APPROVED_EMBEDDING_DIGEST }}",
        })
        self.assertIn("needs.changes.outputs.embedding == 'true'", ci["jobs"]["embedding-image"]["if"])
        steps = {step["id"]: step for step in release["jobs"]["publish"]["steps"] if "id" in step}
        self.assertEqual(steps["embedding-plan"]["env"]["FORCE_REBUILD"], "${{ inputs.force_rebuild }}")
        self.assertIn('--force-rebuild "$FORCE_REBUILD"', steps["embedding-plan"]["run"])
        self.assertEqual(steps["embedding-build"]["with"]["no-cache"], "${{ inputs.force_rebuild }}")
        pr_previous = next(step for step in ci["jobs"]["embedding-image"]["steps"]
                           if step.get("id") == "embedding-previous")
        self.assertEqual(pr_previous["env"]["BASELINE_DIGEST"],
                         "${{ vars.PROJECT_SNOW_APPROVED_EMBEDDING_DIGEST }}")
        for step in (steps["embedding-previous"], pr_previous):
            self.assertIn("approved_embedding_baseline.py", step["run"])
            self.assertNotIn("rev-list", step["run"])
            self.assertNotIn("imagetools inspect", step["run"])

    def execute_resolution(self, *, digest=APPROVED, pull_fails=False):
        """Run the actual workflow shell with an isolated synthetic registry."""
        _ci, workflow = self.workflows()
        step = next(step for step in workflow["jobs"]["publish"]["steps"]
                    if step.get("id") == "embedding-previous")
        candidates = ("C:/Program Files/Git/bin/bash.exe", shutil.which("bash"))
        shell = next((path for path in candidates if path and Path(path).is_file()), None)
        self.assertIsNotNone(shell, "A Bash shell is required for release contract tests")
        with TemporaryDirectory() as directory:
            directory = Path(directory)
            script = directory / "resolve.sh"
            script.write_text('''set -euo pipefail
python() { "$TEST_PYTHON" "$@"; }
git() { printf 'unpromoted-main-ancestor\\n'; }
docker() {
  printf '%s\\n' "$*" >> "$DOCKER_LOG"
  if [ "$1" = pull ]; then
    [ "$PULL_FAILS" != true ] || return 1
    return 0
  fi
  if [ "$1 $2" = 'image inspect' ]; then
    printf '%s\\n' "$TEST_FINGERPRINT"
    return 0
  fi
  if [ "$1 $2 $3" = 'buildx imagetools inspect' ]; then
    printf 'Digest: %s\\n' "$UNPROMOTED_DIGEST"
    return 0
  fi
  return 1
}
''' + step["run"], encoding="utf-8", newline="\n")
            output = directory / "output"
            calls = directory / "docker-calls"
            env = {**os.environ, "TEST_PYTHON": Path(sys.executable).as_posix(),
                   "GITHUB_REPOSITORY_OWNER": "SyntheticOwner", "BASELINE_DIGEST": digest,
                   "GITHUB_OUTPUT": output.as_posix(), "DOCKER_LOG": calls.as_posix(),
                   "TEST_FINGERPRINT": FINGERPRINT, "UNPROMOTED_DIGEST": UNPROMOTED,
                   "PULL_FAILS": str(pull_fails).lower()}
            result = subprocess.run([str(shell), script.as_posix()], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=30)
            return result, output.read_text() if output.exists() else "", calls.read_text() if calls.exists() else ""

    def test_unpromoted_ancestor_with_identical_inputs_cannot_replace_approved_baseline(self):
        result, output, calls = self.execute_resolution()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("digest=" + APPROVED, output)
        self.assertIn("inputs_sha256=" + FINGERPRINT, output)
        self.assertNotIn(UNPROMOTED, output + calls)
        self.assertNotIn("imagetools", calls)
        self.assertIn(reference("SyntheticOwner", APPROVED), calls)

    def test_missing_or_unavailable_baseline_never_falls_back_to_another_build(self):
        for digest, fails in (("", False), ("latest", False), (APPROVED, True)):
            with self.subTest(digest=digest, pull_fails=fails):
                result, output, calls = self.execute_resolution(digest=digest, pull_fails=fails)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")
                self.assertNotIn("imagetools", calls)
                self.assertNotIn(UNPROMOTED, calls)
