"""PUBLIC_UI_SNOW_ADV through the real candidate env builder and acceptance receipts.

Synthetic files only; reuses the dialogue-flag harness that executes the real
build_candidate_public_env function from ops/deploy.sh.
"""
from __future__ import annotations

import pytest

from tests.test_candidate_acceptance import CURRENT, NOW, acceptance, maintenance, ready, staged  # noqa: F401  (fixture)
from tests.test_dialogue_deployment import BASE_SETTINGS, FLAGS, build, builder, settings  # noqa: F401  (fixture)

FLAG = "PUBLIC_UI_SNOW_ADV"


def test_absent_flag_is_written_as_false_exactly_once(builder):
    result, output = build(builder)
    assert result.returncode == 0, result.stderr
    assert settings(output)[FLAG] == "false"
    assert output.count(f"{FLAG}=") == 1


@pytest.mark.parametrize("value", ("true", "false"))
def test_source_value_is_frozen_and_rebuilds_identically(builder, value):
    result, output = build(builder, source=BASE_SETTINGS + f"{FLAG}={value}\n")
    assert result.returncode == 0, result.stderr
    assert settings(output)[FLAG] == value
    rebuilt, second = build(builder, source=output)
    assert rebuilt.returncode == 0, rebuilt.stderr
    assert second == output


def test_exact_sha_override_enables_only_the_interface_skin(builder):
    result, output = build(builder, override=f"{FLAG}=true\n")
    assert result.returncode == 0, result.stderr
    values = settings(output)
    assert values[FLAG] == "true"
    assert tuple(values[key] for key in FLAGS) == ("false",) * 3


@pytest.mark.parametrize("invalid", ("", "TRUE", "1", "yes", " true", '"true"'))
def test_invalid_value_in_source_or_override_preserves_the_existing_env(builder, invalid):
    for source, override in ((BASE_SETTINGS + f"{FLAG}={invalid}\n", None), (BASE_SETTINGS, f"{FLAG}={invalid}\n")):
        result, output = build(builder, source=source, override=override, existing="prior snapshot\n")
        assert result.returncode != 0
        assert "must be true or false" in result.stderr
        assert output == "prior snapshot\n"


@pytest.mark.parametrize("enabled", (False, True))
def test_receipt_names_the_exact_interface_flag(staged, enabled):  # noqa: F811
    paths, execute, _, _, evidence, _, write = staged
    write("runtime/public-candidate.env", f"{FLAG}={str(enabled).lower()}\n")
    evidence["interface_features"] = {"ui_snow_adv": enabled}
    receipt = ready(staged)
    document = acceptance.load_receipt(paths, receipt["receipt_id"])
    assert document["evidence"]["interface_features"] == {"ui_snow_adv": enabled}
    assert document["snapshot"]["public_environments"]["candidate"]["interface_features"] == {"ui_snow_adv": enabled}
    assert "interface_features" not in document["snapshot"]["public_environments"]["current"]
    # Flipping the candidate env after the receipt invalidates the approval.
    write("runtime/public-candidate.env", f"{FLAG}={str(not enabled).lower()}\n")
    with pytest.raises(maintenance.MaintenanceError):
        acceptance.approve(paths, receipt["receipt_id"], CURRENT, now=NOW, execute=execute)


@pytest.mark.parametrize("expected", (None, {}, {"ui_snow_adv": "true"}, {"ui_snow_adv": False}))
def test_receipt_rejects_missing_or_inexact_interface_flag(staged, expected):  # noqa: F811
    _, _, _, _, evidence, _, write = staged
    write("runtime/public-candidate.env", f"{FLAG}=true\n")
    if expected is not None:
        evidence["interface_features"] = expected
    with pytest.raises(maintenance.MaintenanceError):
        ready(staged)


def test_receipt_rejects_a_non_boolean_interface_value(staged):  # noqa: F811
    _, _, _, _, evidence, _, write = staged
    write("runtime/public-candidate.env", f"{FLAG}=yes\n")
    evidence["interface_features"] = {"ui_snow_adv": True}
    with pytest.raises(maintenance.MaintenanceError):
        ready(staged)
