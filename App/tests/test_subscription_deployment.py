"""Personal subscription settings survive staging and exact rollback snapshots."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase

from tests.test_release_state import maintenance, portable_metadata_checks, release_state, state


APP_ROOT = Path(__file__).resolve().parents[1]
BASE_SETTINGS = "PUBLIC_ENABLED_PROVIDERS=openai\nTURNSTILE_SITE_KEY=fixture-key\n"


class SubscriptionDeploymentTests(TestCase):
    @classmethod
    def setUpClass(cls):
        deploy = (APP_ROOT / "ops/deploy.sh").read_text(encoding="utf-8")
        start = deploy.index("build_candidate_public_env() {")
        end = deploy.index('\n}\n\nif [ ! -r "$static_env" ]', start) + 2
        cls.build_function = deploy[start:end]
        candidates = ("C:/Program Files/Git/bin/bash.exe", shutil.which("sh"), shutil.which("bash"))
        cls.shell = next((value for value in candidates if value and Path(value).is_file()), None)
        if cls.shell is None:
            raise AssertionError("A POSIX shell is required for deployment contract tests.")

    def build(self, source, *, existing=""):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.env").write_text(source, encoding="utf-8", newline="\n")
            output = root / "candidate.env"
            if existing:
                output.write_text(existing, encoding="utf-8", newline="\n")
            script = root / "contract.sh"
            script.write_text("""set -u
test_root=$1
candidate_app_version=1.0.0
candidate_data_version=fixture-data
candidate_media_version=fixture-media
candidate_media_root=/fixture/media
candidate_sticker_version=fixture-stickers
candidate_sticker_root=/fixture/stickers
stat() {
  case "$2" in
    %u) printf '%s\\n' 0 ;;
    %a) printf '%s\\n' 600 ;;
    %h) printf '%s\\n' 1 ;;
    *) command stat "$@" ;;
  esac
}
""" + self.build_function + '\nbuild_candidate_public_env "$test_root/source.env" "$test_root/candidate.env"\n',
                              encoding="utf-8", newline="\n")
            result = subprocess.run([self.shell, str(script), root.as_posix()],
                                    capture_output=True, text=True, timeout=30, check=False)
            return result, output.read_text(encoding="utf-8") if output.exists() else None

    def test_old_settings_default_to_disabled_single_worker_without_changing_source(self):
        result, output = self.build(BASE_SETTINGS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PUBLIC_SUBSCRIPTION_ENABLED=false\nWEB_CONCURRENCY=1\n", output)
        self.assertIn("PUBLIC_ENABLED_PROVIDERS=openai\n", output)

    def test_enabled_and_disabled_candidates_keep_their_own_explicit_settings(self):
        snapshots = []
        for enabled in ("true", "false", "true"):
            result, output = self.build(BASE_SETTINGS + f"PUBLIC_SUBSCRIPTION_ENABLED={enabled}\nWEB_CONCURRENCY=1\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"PUBLIC_SUBSCRIPTION_ENABLED={enabled}\nWEB_CONCURRENCY=1\n", output)
            self.assertEqual(output.count("PUBLIC_SUBSCRIPTION_ENABLED="), 1)
            snapshots.append(output)
        self.assertEqual(snapshots[0], snapshots[2])
        self.assertNotEqual(snapshots[0], snapshots[1])

    def test_invalid_flags_workers_and_duplicates_fail_before_writing_candidate(self):
        invalid = [f"PUBLIC_SUBSCRIPTION_ENABLED={value}\n" for value in
                   ("", "TRUE", "False", "1", "yes", " true", "true ", '"true"')]
        invalid += [f"WEB_CONCURRENCY={value}\n" for value in ("", "0", "2", "01", "1 ", "invalid")]
        invalid += ["PUBLIC_SUBSCRIPTION_ENABLED=true\nPUBLIC_SUBSCRIPTION_ENABLED=false\n",
                    "WEB_CONCURRENCY=1\nWEB_CONCURRENCY=1\n"]
        for setting in invalid:
            with self.subTest(setting=setting):
                result, output = self.build(BASE_SETTINGS + setting, existing="preserved candidate\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "preserved candidate\n")
        deploy = (APP_ROOT / "ops/deploy.sh").read_text(encoding="utf-8")
        self.assertLess(deploy.index('build_candidate_public_env "$public_env_source"'),
                        deploy.index('compose run --rm --no-deps "$service"'))


def test_legacy_and_subscription_public_environments_remain_exact_in_recovery_anchors(state):
    # The recovery reader accepts generic env keys and copies public.env as
    # opaque bytes. Do not rebuild an old release with the new release's flags.
    paths = state
    root = paths.root
    snapshots = (BASE_SETTINGS, BASE_SETTINGS + "PUBLIC_SUBSCRIPTION_ENABLED=true\nWEB_CONCURRENCY=1\n")
    compose_path = root / "runtime/colours/green.compose.env"
    for index, snapshot in enumerate(snapshots):
        public_path = root / f"runtime/fixture-public-{index}.env"
        public_path.write_text(snapshot, encoding="utf-8", newline="\n")
        environment = maintenance.read_environment(compose_path)
        environment["PUBLIC_ENV_FILE"] = str(public_path)
        compose_path.write_text("".join(f"{key}={value}\n" for key, value in environment.items()),
                                encoding="utf-8", newline="\n")
        identifier = release_state.archive_colour(paths, "green")
        anchor, _ = release_state.verify_anchor(paths, identifier)
        self_contained = (anchor / "public.env").read_bytes()
        assert self_contained == snapshot.encode()
        # Later operator configuration cannot change this colour's rollback.
        public_path.write_text("PUBLIC_SUBSCRIPTION_ENABLED=false\n", encoding="utf-8", newline="\n")
        release_state.restore_colour(paths, identifier, "green")
        restored = maintenance.read_environment(compose_path)
        assert Path(restored["PUBLIC_ENV_FILE"]).read_bytes() == snapshot.encode()
        assert (anchor / "public.env").read_bytes() == self_contained
