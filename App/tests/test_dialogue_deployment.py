"""Execute the real candidate flag parser with temporary, synthetic files."""
from __future__ import annotations

from itertools import product
import os
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory

import pytest

from tests.test_subscription_deployment import BASE_SETTINGS, SubscriptionDeploymentTests
from tests.test_release_state import maintenance, portable_metadata_checks, release_state, state


FLAGS = ("PUBLIC_DIALOGUE_DOMAINS_V2", "PUBLIC_SCENE_ACTIONS_V2", "PUBLIC_SESSION_MEMORY_V2")
SHA = "a" * 40


@pytest.fixture(scope="module")
def builder():
    SubscriptionDeploymentTests.setUpClass()
    return SubscriptionDeploymentTests


def build(builder, *, source=BASE_SETTINGS, override=None, commit=SHA, existing="", file_kind="file",
          file_metadata="0:0:600", directory_metadata="0:0:700", override_sha=SHA):
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        source_file = root / "source.env"
        source_file.write_text(source, encoding="utf-8", newline="\n")
        output = root / "candidate.env"
        if existing:
            output.write_text(existing, encoding="utf-8", newline="\n")
        configuration = root / "config"
        overrides = configuration / "candidate-flags"
        overrides.mkdir(parents=True)
        if override is not None:
            override_file = overrides / f"{override_sha}.env"
            if file_kind == "directory":
                override_file.mkdir()
            elif file_kind in {"symlink", "hardlink"}:
                target = root / "target.env"
                target.write_text(override, encoding="utf-8", newline="\n")
                try:
                    if file_kind == "symlink":
                        override_file.symlink_to(target)
                    else:
                        os.link(target, override_file)
                except OSError:
                    pytest.skip("Fixture link creation unavailable; Linux CI verifies the rejection")
            else:
                override_file.write_text(override, encoding="utf-8", newline="\n")
        function = builder.build_function.replace(
            "public_override_root=/etc/project-snow/candidate-flags",
            "public_override_root=" + shlex.quote(overrides.as_posix()),
        ).replace("for public_override_parent in /etc /etc/project-snow", 'for public_override_parent in "$test_root" "$test_root/config"')
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
    %h) command stat "$@" ;;
    %u:%g:%a) printf '%s\\n' "$fixture_directory_metadata" ;;
    %u:%g:%a:%h) printf '%s:%s\\n' "$fixture_file_metadata" "$(command stat -c %h "$3")" ;;
    *) command stat "$@" ;;
  esac
}
""" + "fixture_file_metadata=" + shlex.quote(file_metadata) + "\n"
            + "fixture_directory_metadata=" + shlex.quote(directory_metadata) + "\n"
            + function + '\nbuild_candidate_public_env "$test_root/source.env" "$test_root/candidate.env" '
            + shlex.quote(commit) + "\n", encoding="utf-8", newline="\n")
        result = subprocess.run([builder.shell, str(script), root.as_posix()],
            capture_output=True, text=True, timeout=30, check=False)
        assert source_file.read_text(encoding="utf-8") == source
        assert not (root / "would-execute").exists()
        return result, output.read_text(encoding="utf-8") if output.exists() else None


def settings(output):
    return dict(line.split("=", 1) for line in output.splitlines())


def test_old_source_and_unmatched_override_keep_default_false(builder):
    for override, override_sha in ((None, SHA), (f"{FLAGS[0]}=true\n", "b" * 40)):
        result, output = build(builder, override=override, override_sha=override_sha)
        assert result.returncode == 0, result.stderr
        assert tuple(settings(output)[key] for key in FLAGS) == ("false",) * 3


@pytest.mark.parametrize("values", tuple(product(("true", "false"), repeat=3)))
def test_independent_exact_sha_flags_are_frozen_into_the_generated_env(builder, values):
    override = "".join(f"{key}={value}\n" for key, value in zip(FLAGS, values))
    result, output = build(builder, override=override)
    assert result.returncode == 0, result.stderr
    assert tuple(settings(output)[key] for key in FLAGS) == values
    assert all(output.count(f"{key}=") == 1 for key in FLAGS)
    # Rebuilding this snapshot without an override retains exact opt-ins.
    rebuilt, second = build(builder, source=output)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert second == output


def test_partial_override_changes_only_the_requested_flag(builder):
    source = BASE_SETTINGS + "".join(f"{key}=true\n" for key in FLAGS)
    result, output = build(builder, source=source, override=f"{FLAGS[1]}=false\n")
    assert result.returncode == 0, result.stderr
    assert tuple(settings(output)[key] for key in FLAGS) == ("true", "false", "true")


@pytest.mark.parametrize("invalid", ("", "TRUE", "False", "1", "yes", " true", "true ", '"true"'))
def test_invalid_source_or_override_boolean_preserves_the_existing_env(builder, invalid):
    for source, override in ((BASE_SETTINGS + f"{FLAGS[0]}={invalid}\n", None),
                             (BASE_SETTINGS, f"{FLAGS[0]}={invalid}\n")):
        result, output = build(builder, source=source, override=override, existing="prior snapshot\n")
        assert result.returncode != 0
        assert "must be true or false" in result.stderr
        assert output == "prior snapshot\n"


@pytest.mark.parametrize("override", (
    "", "# no enabled setting\n", "malformed\n", "PUBLIC_ORIGIN=https://invalid.example\n",
    "PUBLIC_VERIFIED_BLOCK_STREAM=true\n", "PUBLIC_ENV_FILE=/outside.env\n",
    f"{FLAGS[0]}=true\n{FLAGS[0]}=false\n", f"{FLAGS[0]}=true\r\n",
    f"{FLAGS[0]}=$(touch would-execute)\n", "#" + "x" * 1024,
))
def test_only_three_bounded_data_keys_are_allowed(builder, override):
    result, output = build(builder, override=override, existing="prior snapshot\n")
    assert result.returncode != 0
    assert output == "prior snapshot\n"


@pytest.mark.parametrize("commit", ("../outside", "A" * 40, "a" * 39, "a" * 41, "main"))
def test_release_sha_cannot_choose_an_uncontrolled_file(builder, commit):
    result, output = build(builder, commit=commit, override=f"{FLAGS[0]}=true\n", existing="prior snapshot\n")
    assert result.returncode != 0
    assert "exact lowercase release SHA" in result.stderr
    assert output == "prior snapshot\n"


@pytest.mark.parametrize("file_kind", ("directory", "symlink", "hardlink"))
def test_override_requires_one_regular_file_without_links(builder, file_kind):
    result, output = build(builder, file_kind=file_kind, override=f"{FLAGS[0]}=true\n", existing="prior snapshot\n")
    assert result.returncode != 0
    assert "single regular file" in result.stderr
    assert output == "prior snapshot\n"


@pytest.mark.parametrize("file_metadata", ("1000:0:600", "0:1000:600", "0:0:644", "0:0:400", "0:0:666"))
def test_root_only_file_metadata_is_required(builder, file_metadata):
    result, output = build(builder, file_metadata=file_metadata, override=f"{FLAGS[0]}=true\n", existing="prior snapshot\n")
    assert result.returncode != 0
    assert "root-owned mode-0600" in result.stderr
    assert output == "prior snapshot\n"


@pytest.mark.parametrize("directory_metadata", ("1000:0:700", "0:1000:700", "0:0:777", "0:0:775"))
def test_override_directory_is_root_controlled_without_shared_writes(builder, directory_metadata):
    result, output = build(builder, directory_metadata=directory_metadata, override=f"{FLAGS[0]}=true\n", existing="prior snapshot\n")
    assert result.returncode != 0
    assert "without group/world writes" in result.stderr
    assert output == "prior snapshot\n"


def test_production_runner_stages_using_fixed_exact_sha_without_new_operator_environment():
    app = Path(__file__).parents[1]
    deploy = (app / "ops/deploy.sh").read_text(encoding="utf-8")
    runner = (app / "ops/project-snow-release").read_text(encoding="utf-8")
    assert 'build_candidate_public_env "$public_env_source" "$candidate_public_env" "$sha"' in deploy
    assert "public_override_root=/etc/project-snow/candidate-flags" in deploy
    assert "PROJECT_SNOW_DIALOGUE_OVERRIDE" not in runner
    assert "exec /usr/bin/env -i" in runner


def test_old_new_old_recovery_retains_each_exact_feature_snapshot(state):
    paths = state
    root = paths.root
    compose_path = root / "runtime/colours/green.compose.env"
    snapshots = [BASE_SETTINGS, BASE_SETTINGS + "".join(f"{key}=true\n" for key in FLAGS), BASE_SETTINGS]
    for number, snapshot in enumerate(snapshots):
        public_path = root / f"runtime/public-synthetic-{number}.env"
        public_path.write_text(snapshot, encoding="utf-8", newline="\n")
        environment = maintenance.read_environment(compose_path)
        environment["PUBLIC_ENV_FILE"] = str(public_path)
        compose_path.write_text("".join(f"{key}={value}\n" for key, value in environment.items()), encoding="utf-8", newline="\n")
        identifier = release_state.archive_colour(paths, "green")
        anchor, _ = release_state.verify_anchor(paths, identifier)
        assert (anchor / "public.env").read_bytes() == snapshot.encode()
        public_path.write_text("later operator change\n", encoding="utf-8", newline="\n")
        release_state.restore_colour(paths, identifier, "green")
        restored = maintenance.read_environment(compose_path)
        assert Path(restored["PUBLIC_ENV_FILE"]).read_bytes() == snapshot.encode()

