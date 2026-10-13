"""Run the production settings builder with synthetic candidate/rollback inputs."""
from itertools import product

import pytest

from tests import test_subscription_deployment as deployment


FLAGS = ("PUBLIC_CHAT_LATENCY_V2", "PUBLIC_VERIFIED_BLOCK_STREAM", "PUBLIC_MOTION_V2")


@pytest.fixture(scope="module")
def build():
    deployment.SubscriptionDeploymentTests.setUpClass()
    return deployment.SubscriptionDeploymentTests().build


def settings(output):
    return dict(line.split("=", 1) for line in output.splitlines())


def test_old_public_settings_keep_safe_defaults_and_existing_opt_ins(build):
    result, output = build(deployment.BASE_SETTINGS)
    assert result.returncode == 0, result.stderr
    candidate = settings(output)
    assert tuple(candidate[key] for key in FLAGS) == ("true", "false", "true")
    assert candidate["PUBLIC_SUBSCRIPTION_ENABLED"] == "false"
    assert candidate["PUBLIC_STATISTICS_ENABLED"] == "false"
    assert candidate["WEB_CONCURRENCY"] == "1"


@pytest.mark.parametrize("values", tuple(product(("true", "false"), repeat=3)))
def test_explicit_flags_survive_generation_and_repeated_candidate_builds(build, values):
    source = deployment.BASE_SETTINGS + "PUBLIC_SUBSCRIPTION_ENABLED=true\nPUBLIC_STATISTICS_ENABLED=true\nWEB_CONCURRENCY=1\n"
    source += "".join(f"{key}={value}\n" for key, value in zip(FLAGS, values))
    result, output = build(source)
    assert result.returncode == 0, result.stderr
    candidate = settings(output)
    assert tuple(candidate[key] for key in FLAGS) == values
    assert candidate["PUBLIC_SUBSCRIPTION_ENABLED"] == "true"
    assert candidate["PUBLIC_STATISTICS_ENABLED"] == "true"
    assert candidate["WEB_CONCURRENCY"] == "1"
    assert all(output.count(f"{key}=") == 1 for key in FLAGS)
    repeated, rebuilt = build(output)
    assert repeated.returncode == 0, repeated.stderr
    assert rebuilt == output


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("invalid", ("", "TRUE", "False", "1", "yes", " true", "true ", '"true"'))
def test_invalid_flag_leaves_existing_candidate_untouched(build, flag, invalid):
    result, output = build(deployment.BASE_SETTINGS + f"{flag}={invalid}\n", existing="previous candidate\n")
    assert result.returncode != 0
    assert "Public presentation flags must be true or false." in result.stderr
    assert output == "previous candidate\n"


@pytest.mark.parametrize("flag", FLAGS)
def test_duplicate_flags_fail_before_replacing_a_candidate(build, flag):
    result, output = build(deployment.BASE_SETTINGS + f"{flag}=true\n{flag}=false\n", existing="previous candidate\n")
    assert result.returncode != 0
    assert "duplicate key" in result.stderr
    assert output == "previous candidate\n"


@pytest.mark.parametrize("statistics", ("false", "TRUE", "1", ""))
def test_statistics_requires_its_existing_exact_opt_in(build, statistics):
    result, output = build(deployment.BASE_SETTINGS + f"PUBLIC_STATISTICS_ENABLED={statistics}\nPUBLIC_VERIFIED_BLOCK_STREAM=true\n")
    assert result.returncode == 0, result.stderr
    assert settings(output)["PUBLIC_STATISTICS_ENABLED"] == "false"
    assert settings(output)["PUBLIC_VERIFIED_BLOCK_STREAM"] == "true"


@pytest.mark.parametrize("invalid", ("PUBLIC_SUBSCRIPTION_ENABLED=TRUE\n", "WEB_CONCURRENCY=2\n"))
def test_presentation_flags_do_not_bypass_subscription_and_worker_gates(build, invalid):
    result, output = build(deployment.BASE_SETTINGS + "PUBLIC_MOTION_V2=false\n" + invalid, existing="previous candidate\n")
    assert result.returncode != 0
    assert output == "previous candidate\n"
