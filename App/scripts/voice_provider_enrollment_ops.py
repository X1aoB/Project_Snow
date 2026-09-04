"""Prepare and operate fail-closed Qwen custom-voice enrollment.

The offline ``prepare`` command pins the current four-candidate preflight to a
region/model/cost contract. ``inspect`` builds a redacted request without
reading credentials. Provider mutations are deliberately one candidate at a
time and require exact confirmations; every attempt is durably recorded before
the network call so an uncertain outcome cannot be retried silently.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import io
import json
import os
import re
import ssl
import stat
import sys
import urllib.error
import urllib.request
import uuid
import wave
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_provider_preflight_ops as preflight
else:
    import voice_paralinguistic_ops as base
    import voice_provider_preflight_ops as preflight


SCHEMA = "project-snow-private-voice-provider-enrollment-run-1"
POLICY_VERSION = "project-snow-qwen-one-candidate-at-a-time-enrollment-1"
AUDIT_SCHEMA = "project-snow-private-voice-provider-enrollment-audit-1"
OUTPUT_DIRECTORY = "tts_provider_enrollment_runs"
AUDIT_DIRECTORY = "tts_provider_enrollment_audits"
RUN_ID_PATTERN = re.compile(r"voice-provider-enrollment-run-[0-9a-f]{20}\Z")
WORKSPACE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{2,127}\Z")
VOICE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
PREFERRED_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,16}\Z")
ATTEMPT_ID_PATTERN = re.compile(r"provider-attempt-[0-9a-f]{32}\Z")
MODEL = "qwen3-tts-vc-realtime-2026-01-15"
ENROLLMENT_MODEL = "qwen-voice-enrollment"
REGION = "ap-southeast-1"
REGION_NAME = "Singapore"
CHINA_REGION = "cn-beijing"
ENDPOINT_PATH = "/api/v1/services/audio/tts/customization"
REGION_CONTRACTS = {
    REGION: {
        "region_name": REGION_NAME,
        "endpoint_suffix": "ap-southeast-1.maas.aliyuncs.com",
        "workspace_blocker": "singapore_workspace_id_not_bound",
        "inspect_workspace_blocker": "singapore_workspace_id_missing",
        "upload_confirmation_label": "external upload to Singapore",
        "dotenv_base_url_host": "dashscope-intl.aliyuncs.com",
        "free_quota_may_reduce_actual_charge": True,
    },
    CHINA_REGION: {
        "region_name": "China (Beijing)",
        "endpoint_suffix": "cn-beijing.maas.aliyuncs.com",
        "workspace_blocker": "beijing_workspace_id_not_bound",
        "inspect_workspace_blocker": "beijing_workspace_id_missing",
        "upload_confirmation_label": "external upload to China (Beijing)",
        "dotenv_base_url_host": "dashscope.aliyuncs.com",
        "free_quota_may_reduce_actual_charge": False,
    },
}
CREATE_UNIT_PRICE_USD = Decimal("0.01")
PLANNED_CREATE_COUNT = 4
CREATE_COST_CEILING_USD = Decimal("0.04")
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_AUDIT_BYTES = 1024 * 1024
MAX_DOTENV_BYTES = 64 * 1024
SOURCE_RETENTION_STATUS = "not_identified_in_reviewed_official_documentation"
RIGHTS_BASIS = "unverified_fanwork_source"
SCOPE_LIMIT_KEYS = (
    "provider_mutation_authorized_by_offline_package",
    "credentials_embedded_or_read",
    "source_audio_uploaded",
    "provider_voice_created",
    "synthesis_or_blind_test_authorized",
    "publication_authorized",
    "rollout_authorized",
)
PREFERRED_NAME_PREFIX = {
    "vidya-a": "psvda",
    "vidya-b": "psvdb",
    "chenxing-a": "pscxa",
    "chenxing-b": "pscxb",
}
OFFICIAL_SOURCES = {
    "voice_clone_http_api": "https://help.aliyun.com/en/model-studio/voice-clone-design-http-api",
    "voice_cloning_guide": "https://www.alibabacloud.com/help/zh/model-studio/voice-cloning-user-guide",
    "realtime_tts_guide": "https://www.alibabacloud.com/help/zh/model-studio/realtime-tts-user-guide",
    "pricing": "https://www.alibabacloud.com/help/ja/model-studio/model-pricing",
}


class VoiceProviderEnrollmentError(base.VoiceParalinguisticError):
    """Raised when enrollment cannot be proven safe and correctly bound."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise VoiceProviderEnrollmentError("provider redirects are not permitted")


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceProviderEnrollmentError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceProviderEnrollmentError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceProviderEnrollmentError(f"{label} must be an array")
    return value


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _scope_limits() -> dict[str, bool]:
    return {key: False for key in SCOPE_LIMIT_KEYS}


def _region_contract(region: Any) -> dict[str, Any]:
    if not isinstance(region, str) or region not in REGION_CONTRACTS:
        raise VoiceProviderEnrollmentError(
            f"unsupported provider region: {region!r}; expected one of {tuple(REGION_CONTRACTS)!r}"
        )
    return REGION_CONTRACTS[region]


def _load_preflight(
    root: Path,
    preflight_id: str,
    *,
    expected_manifest_byte_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_hash = base._require_sha256(
        expected_manifest_byte_sha256,
        label="expected preflight manifest byte SHA-256",
    )
    validation = preflight.validate_preflight(
        root,
        preflight_id,
        expected_manifest_byte_sha256=expected_hash,
    )
    path = root / preflight.OUTPUT_DIRECTORY / preflight_id / "manifest.json"
    manifest, payload = base._read_json(root, path, label="provider preflight manifest")
    _expect(base._sha256_bytes(payload), expected_hash, label="preflight manifest byte SHA-256")
    _expect(validation.get("manifest_sha256"), manifest.get("manifest_sha256"), label="preflight hash")
    _expect(validation.get("candidate_count"), PLANNED_CREATE_COUNT, label="preflight candidate count")
    _expect(validation.get("provider_interactions_performed"), False, label="preflight provider state")
    return manifest, validation


def _candidate_contracts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = _array(manifest.get("candidates"), label="preflight candidates")
    output: list[dict[str, Any]] = []
    for raw in candidates:
        item = _object(raw, label="preflight candidate")
        key = base._require_string(item.get("candidate_key"), label="candidate key")
        prefix = PREFERRED_NAME_PREFIX.get(key)
        if prefix is None:
            raise VoiceProviderEnrollmentError(f"unexpected provider candidate: {key!r}")
        audio = _object(item.get("reference_audio"), label=f"{key} reference audio")
        text = _object(item.get("enrollment_text"), label=f"{key} enrollment text")
        character = _object(item.get("character"), label=f"{key} character")
        audio_sha256 = base._require_sha256(audio.get("wav_sha256"), label=f"{key} audio SHA-256")
        preferred_name = f"{prefix}{audio_sha256[:8]}"
        if not PREFERRED_NAME_PATTERN.fullmatch(preferred_name):
            raise VoiceProviderEnrollmentError(f"invalid derived preferred name for {key}")
        output.append(
            {
                "candidate_id": item.get("candidate_id"),
                "candidate_key": key,
                "preferred_name": preferred_name,
                "character": {
                    "runtime_character_id": character.get("runtime_character_id"),
                    "runtime_character_name": character.get("runtime_character_name"),
                    "character_slug": character.get("character_slug"),
                },
                "source": {
                    "reference_audio_relative_path": audio.get("relative_path"),
                    "reference_audio_sha256": audio_sha256,
                    "reference_audio_byte_count": audio.get("byte_count"),
                    "audio_format": audio.get("audio_format"),
                    "transcript_relative_path": text.get("relative_path"),
                    "transcript_utf8_sha256": base._require_sha256(
                        text.get("utf8_sha256"), label=f"{key} transcript SHA-256"
                    ),
                    "transcript_byte_count": text.get("byte_count"),
                },
                "registration_state": "not_submitted",
                "provider_voice_id": None,
            }
        )
    _expect(
        [item["candidate_key"] for item in output],
        ["vidya-a", "vidya-b", "chenxing-a", "chenxing-b"],
        label="enrollment candidate order",
    )
    return output


def _readme(run_id: str, preflight_id: str, *, region: str = REGION) -> str:
    contract = _region_contract(region)
    region_name = contract["region_name"]
    quota_note = (
        "before any applicable free quota"
        if contract["free_quota_may_reduce_actual_charge"]
        else "with no Singapore-only free quota assumed"
    )
    return f"""# Project Snow provider enrollment run

Run ID: `{run_id}`
Source preflight: `{preflight_id}`

This is an immutable, offline-only execution contract for four independent
Qwen voice-clone candidates in {region_name}. Creating this directory did not read
an API key, upload audio, create a voice, incur cost, synthesize a test, or
authorize publication.

The provider mutation command is intentionally one candidate at a time. Before
each network call it revalidates this run, the source preflight, transcript and
WAV; requires exact model/region/run/candidate/cost confirmations; and commits
an attempt record. An attempt without a result is an uncertain provider state:
list/reconcile it before retrying. Never batch-retry an uncertain create.

The conservative direct creation ceiling is USD 0.04 for all four candidates
(USD 0.01 per successful create {quota_note}). Synthesis
and blind-test usage are outside that ceiling and remain unauthorized.

The reviewed official documentation did not establish a precise retention
period for the uploaded source sample. Live upload therefore requires a
separate explicit acceptance of that documented uncertainty. Provider voice
IDs and audit receipts remain private under `Data/Voice`.
"""


def build_readiness(
    voice_root: Path,
    *,
    preflight_id: str,
    expected_preflight_manifest_byte_sha256: str,
    prepared_at: str,
    region: str = REGION,
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    prepared = base._parse_recorded_at(prepared_at)
    region_details = _region_contract(region)
    source_manifest, validation = _load_preflight(
        root,
        preflight_id,
        expected_manifest_byte_sha256=expected_preflight_manifest_byte_sha256,
    )
    candidates = _candidate_contracts(source_manifest)
    source_anchor = {
        "preflight_id": preflight_id,
        "relative_path": f"{preflight.OUTPUT_DIRECTORY}/{preflight_id}/manifest.json",
        "manifest_sha256": validation["manifest_sha256"],
        "manifest_byte_sha256": validation["manifest_byte_sha256"],
    }
    provider_contract = {
        "provider_family": preflight.PROVIDER_FAMILY,
        "enrollment_model": ENROLLMENT_MODEL,
        "target_model": MODEL,
        "region": region,
        "region_name": region_details["region_name"],
        "workspace_specific_endpoint_template": (
            f"https://{{workspace_id}}.{region_details['endpoint_suffix']}" + ENDPOINT_PATH
        ),
        "one_ephemeral_voice_per_candidate": True,
        "batch_create_permitted": False,
    }
    cost_contract = {
        "currency": "USD",
        "successful_create_unit_price_usd": str(CREATE_UNIT_PRICE_USD),
        "planned_successful_create_count": PLANNED_CREATE_COUNT,
        "direct_creation_cost_ceiling_usd": str(CREATE_COST_CEILING_USD),
        "free_quota_may_reduce_actual_charge": region_details["free_quota_may_reduce_actual_charge"],
        "failed_create_documented_as_not_charged": True,
        "delete_does_not_restore_free_quota": True,
        "synthesis_and_blind_test_cost_included": False,
        "actual_charge_known_from_local_receipt": False,
    }
    stable_basis = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "prepared_at": prepared,
        "source_preflight": source_anchor,
        "provider_contract": provider_contract,
        "cost_contract": cost_contract,
        "candidate_anchors": [
            {
                "candidate_key": item["candidate_key"],
                "preferred_name": item["preferred_name"],
                "audio_sha256": item["source"]["reference_audio_sha256"],
                "transcript_sha256": item["source"]["transcript_utf8_sha256"],
            }
            for item in candidates
        ],
    }
    stable_identity = base._semantic_sha256(stable_basis)
    run_id = f"voice-provider-enrollment-run-{stable_identity[:20]}"
    readme = _readme(run_id, preflight_id, region=region)
    readme_payload = readme.encode("utf-8")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "prepared_at": prepared,
        "policy_version": POLICY_VERSION,
        "artifact_purpose": "offline_only_provider_execution_contract",
        "source_preflight": source_anchor,
        "provider_contract": provider_contract,
        "pricing_and_quota_contract": cost_contract,
        "privacy_and_retention_contract": {
            "source_audio_external_upload_region": region_details["region_name"],
            "source_audio_retention_status": SOURCE_RETENTION_STATUS,
            "source_audio_retention_uncertainty_acceptance_required_per_live_create": True,
            "custom_voice_unused_for_one_year_documented_as_auto_cleaned": True,
            "provider_deletion_supported": True,
        },
        "rights_and_risk_contract": {
            "rights_basis": RIGHTS_BASIS,
            "written_source_authorization_present": False,
            "risk_route_must_be_explicitly_reconfirmed_per_live_create": True,
            "provider_terms_and_voice_cloning_consent_must_be_confirmed_per_live_create": True,
            "publication_or_rollout_implied": False,
        },
        "credentials": {
            "workspace_id": None,
            "api_key": None,
            "credential_source": None,
            "credentials_read": False,
            "credentials_embedded": False,
        },
        "candidate_count": PLANNED_CREATE_COUNT,
        "candidates": candidates,
        "mutation_protocol": {
            "one_candidate_per_command": True,
            "attempt_receipt_committed_before_network": True,
            "uncertain_attempt_blocks_retry": True,
            "automatic_retry": False,
            "automatic_batch_rollback": False,
            "reconcile_with_provider_list_before_uncertain_retry": True,
            "operator_list_command_implemented": True,
            "operator_delete_one_command_implemented": True,
        },
        "official_source_urls": OFFICIAL_SOURCES,
        "artifacts": {
            "operator_readme": {
                "relative_path": "README.md",
                "byte_sha256": base._sha256_bytes(readme_payload),
                "byte_count": len(readme_payload),
            }
        },
        "live_execution_blockers": [
            region_details["workspace_blocker"],
            "api_key_not_read_or_bound",
            "source_audio_retention_uncertainty_not_explicitly_accepted",
            "per_candidate_external_upload_and_cost_confirmation_not_supplied",
        ],
        "next_status": "offline_ready_live_provider_execution_blocked",
        "scope_limits": _scope_limits(),
        "provider_interactions_performed": False,
        "stable_identity": stable_identity,
    }
    manifest["manifest_sha256"] = base._semantic_sha256(manifest)
    destination = root / OUTPUT_DIRECTORY / run_id
    return {"manifest": manifest, "readme": readme}, destination


def _validate_manifest_shape(manifest: dict[str, Any]) -> str:
    _expect(manifest.get("schema_version"), SCHEMA, label="enrollment run schema")
    run_id = base._safe_identifier(manifest.get("run_id"), RUN_ID_PATTERN, label="run_id")
    identity = base._require_sha256(manifest.get("stable_identity"), label="stable identity")
    _expect(run_id, f"voice-provider-enrollment-run-{identity[:20]}", label="run ID derivation")
    base._verify_semantic_hash(manifest, field="manifest_sha256", label="enrollment run manifest")
    limits = base._require_all_false(manifest.get("scope_limits"), label="enrollment run scope_limits")
    _expect(tuple(limits), SCOPE_LIMIT_KEYS, label="enrollment run scope-limit keys")
    _expect(manifest.get("provider_interactions_performed"), False, label="offline provider state")
    credentials = _object(manifest.get("credentials"), label="credentials")
    for key in ("workspace_id", "api_key", "credential_source"):
        _expect(credentials.get(key), None, label=f"offline credentials.{key}")
    for key in ("credentials_read", "credentials_embedded"):
        _expect(credentials.get(key), False, label=f"offline credentials.{key}")
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    _expect(provider.get("target_model"), MODEL, label="target model")
    _expect(provider.get("enrollment_model"), ENROLLMENT_MODEL, label="enrollment model")
    region = base._require_string(provider.get("region"), label="provider region")
    region_details = _region_contract(region)
    _expect(provider.get("region_name"), region_details["region_name"], label="region name")
    _expect(
        provider.get("workspace_specific_endpoint_template"),
        f"https://{{workspace_id}}.{region_details['endpoint_suffix']}{ENDPOINT_PATH}",
        label="workspace-specific endpoint template",
    )
    cost = _object(manifest.get("pricing_and_quota_contract"), label="cost contract")
    _expect(cost.get("direct_creation_cost_ceiling_usd"), str(CREATE_COST_CEILING_USD), label="cost ceiling")
    _expect(
        cost.get("free_quota_may_reduce_actual_charge"),
        region_details["free_quota_may_reduce_actual_charge"],
        label="regional free-quota contract",
    )
    _expect(manifest.get("candidate_count"), PLANNED_CREATE_COUNT, label="candidate count")
    candidates = _array(manifest.get("candidates"), label="run candidates")
    _expect(
        [item.get("candidate_key") if isinstance(item, dict) else None for item in candidates],
        ["vidya-a", "vidya-b", "chenxing-a", "chenxing-b"],
        label="run candidate order",
    )
    for raw in candidates:
        item = _object(raw, label="run candidate")
        key = base._require_string(item.get("candidate_key"), label="run candidate key")
        source = _object(item.get("source"), label=f"{key} run source")
        audio_sha256 = base._require_sha256(
            source.get("reference_audio_sha256"), label=f"{key} run audio SHA-256"
        )
        base._require_sha256(source.get("transcript_utf8_sha256"), label=f"{key} run transcript SHA-256")
        _expect(
            item.get("preferred_name"),
            f"{PREFERRED_NAME_PREFIX[key]}{audio_sha256[:8]}",
            label=f"{key} preferred name",
        )
        _expect(item.get("registration_state"), "not_submitted", label=f"{key} offline state")
        _expect(item.get("provider_voice_id"), None, label=f"{key} offline provider voice ID")
    return run_id


def validate_readiness(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    safe_id = base._safe_identifier(run_id, RUN_ID_PATTERN, label="run_id")
    directory = root / OUTPUT_DIRECTORY / safe_id
    base._require_safe_existing_path(root, directory, label="enrollment run directory", directory=True)
    manifest, payload = base._read_json(root, directory / "manifest.json", label="enrollment run manifest")
    _expect(_validate_manifest_shape(manifest), safe_id, label="enrollment run directory ID")
    payload_hash = base._sha256_bytes(payload)
    if expected_manifest_byte_sha256 is not None:
        _expect(
            payload_hash,
            base._require_sha256(expected_manifest_byte_sha256, label="expected run manifest byte SHA-256"),
            label="run manifest byte SHA-256",
        )
    readme = base._read_stable_bytes(root, directory / "README.md", label="enrollment run README")
    readme_anchor = _object(
        _object(manifest.get("artifacts"), label="artifacts").get("operator_readme"),
        label="README anchor",
    )
    _expect(base._sha256_bytes(readme), readme_anchor.get("byte_sha256"), label="README byte SHA-256")
    _expect(len(readme), readme_anchor.get("byte_count"), label="README byte count")
    source = _object(manifest.get("source_preflight"), label="source preflight")
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    rebuilt, rebuilt_destination = build_readiness(
        root,
        preflight_id=source.get("preflight_id"),
        expected_preflight_manifest_byte_sha256=source.get("manifest_byte_sha256"),
        prepared_at=manifest.get("prepared_at"),
        region=provider.get("region"),
    )
    _expect(rebuilt_destination, directory, label="rebuilt enrollment run path")
    _expect(rebuilt["manifest"], manifest, label="enrollment run/source reconstruction")
    _expect(rebuilt["readme"].encode("utf-8"), readme, label="README/source reconstruction")
    return {
        "status": "valid",
        "run_id": safe_id,
        "path": str(directory),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_byte_sha256": payload_hash,
        "candidate_count": PLANNED_CREATE_COUNT,
        "all_scope_gates_closed": True,
        "provider_interactions_performed": False,
        "next_status": manifest["next_status"],
    }


def _remove_partial(directory: Path) -> None:
    for name in ("manifest.json", "README.md"):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass


def write_readiness(
    voice_root: Path,
    artifacts: dict[str, Any],
    destination: Path,
) -> tuple[str, dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    manifest = _object(artifacts.get("manifest"), label="manifest")
    readme = artifacts.get("readme")
    if not isinstance(readme, str) or not readme:
        raise VoiceProviderEnrollmentError("README must be non-empty text")
    run_id = _validate_manifest_shape(manifest)
    expected_destination = root / OUTPUT_DIRECTORY / run_id
    _expect(base._absolute_lexical(destination), expected_destination, label="enrollment run destination")
    source = _object(manifest.get("source_preflight"), label="source preflight")
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    rebuilt, rebuilt_destination = build_readiness(
        root,
        preflight_id=source.get("preflight_id"),
        expected_preflight_manifest_byte_sha256=source.get("manifest_byte_sha256"),
        prepared_at=manifest.get("prepared_at"),
        region=provider.get("region"),
    )
    _expect(rebuilt_destination, destination, label="rebuilt write destination")
    _expect(rebuilt, artifacts, label="enrollment artifacts/source reconstruction")
    output = root / OUTPUT_DIRECTORY
    if output.exists():
        base._require_safe_existing_path(root, output, label="enrollment output directory", directory=True)
    else:
        try:
            output.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(root, output, label="enrollment output directory", directory=True)
    if expected_destination.exists():
        validation = validate_readiness(root, run_id)
        existing, _ = base._read_json(
            root, expected_destination / "manifest.json", label="existing run manifest"
        )
        if existing != manifest:
            return "existing_conflict", existing
        _expect(validation["manifest_sha256"], manifest["manifest_sha256"], label="existing run hash")
        return "existing_valid", existing
    temporary = output / f".{run_id}.{uuid.uuid4().hex}.partial"
    temporary.mkdir()
    created_destination = False
    try:
        for name, data in (
            ("README.md", readme.encode("utf-8")),
            ("manifest.json", _pretty_json_bytes(manifest)),
        ):
            path = temporary / name
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            base._require_safe_existing_path(root, path, label=f"temporary {name}", directory=False)
        try:
            os.rename(temporary, expected_destination)
        except OSError:
            if expected_destination.exists():
                return write_readiness(root, artifacts, destination)
            raise
        created_destination = True
        validation = validate_readiness(root, run_id)
        _expect(validation["manifest_sha256"], manifest["manifest_sha256"], label="written run hash")
        return "created", manifest
    except Exception:
        if created_destination:
            _remove_partial(expected_destination)
        raise
    finally:
        if temporary.exists():
            _remove_partial(temporary)


def enrollment_endpoint(workspace_id: str, *, region: str = REGION) -> str:
    region_details = _region_contract(region)
    normalized = workspace_id.strip().lower()
    if not WORKSPACE_PATTERN.fullmatch(normalized):
        raise VoiceProviderEnrollmentError(f"invalid {region_details['region_name']} workspace id")
    return f"https://{normalized}.{region_details['endpoint_suffix']}{ENDPOINT_PATH}"


def _read_bounded_file(path: Path, *, limit: int, message: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise VoiceProviderEnrollmentError(message)
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise VoiceProviderEnrollmentError(message)
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            value = stream.read(limit + 1)
            opened_after = os.fstat(stream.fileno())
        after = path.stat()
    except OSError as error:
        raise VoiceProviderEnrollmentError(message) from error
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(value) > limit
        or path.is_symlink()
        or identity
        != (opened_before.st_dev, opened_before.st_ino, opened_before.st_size, opened_before.st_mtime_ns)
        or identity
        != (opened_after.st_dev, opened_after.st_ino, opened_after.st_size, opened_after.st_mtime_ns)
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise VoiceProviderEnrollmentError(message)
    return value


def _safe_source_path(root: Path, value: Any, *, label: str) -> Path:
    relative = base._safe_relative_path(value, label=label)
    path = root.joinpath(*relative.parts)
    base._require_safe_existing_path(root, path, label=label, directory=False)
    return path


def _validate_wav(audio: bytes, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            frames = reader.getnframes()
            compression = reader.getcomptype()
    except (EOFError, wave.Error) as error:
        raise VoiceProviderEnrollmentError("reference audio must remain a valid PCM WAV") from error
    duration = frames / max(rate, 1)
    actual = {
        "encoding": "pcm_s16le" if compression == "NONE" and width == 2 else "unsupported",
        "sample_rate_hz": rate,
        "channels": channels,
        "sample_width_bytes": width,
        "frame_count": frames,
        "duration_seconds": round(duration, 6),
    }
    for key in ("encoding", "sample_rate_hz", "channels", "sample_width_bytes", "frame_count"):
        _expect(actual.get(key), expected.get(key), label=f"reference WAV {key}")
    if abs(float(actual["duration_seconds"]) - float(expected.get("duration_seconds"))) > 0.000001:
        raise VoiceProviderEnrollmentError("reference WAV duration changed")
    if not 10 <= duration <= 20:
        raise VoiceProviderEnrollmentError("reference WAV must remain within the pinned 10-20 second window")
    return actual


def build_create_payload(
    *,
    preferred_name: str,
    audio: bytes,
    transcript: str,
) -> dict[str, Any]:
    if not PREFERRED_NAME_PATTERN.fullmatch(preferred_name):
        raise VoiceProviderEnrollmentError("preferred name must be 1-16 alphanumeric/underscore characters")
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise VoiceProviderEnrollmentError("enrollment audio must be non-empty and at most 10 MiB")
    exact_text = transcript.strip()
    if not exact_text or "\x00" in exact_text or len(exact_text.encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
        raise VoiceProviderEnrollmentError("an exact bounded UTF-8 transcript is required")
    encoded = base64.b64encode(audio).decode("ascii")
    return {
        "model": ENROLLMENT_MODEL,
        "input": {
            "action": "create",
            "target_model": MODEL,
            "preferred_name": preferred_name,
            "audio": {"data": f"data:audio/wav;base64,{encoded}"},
            "text": exact_text,
            "language": "zh",
        },
    }


def build_list_payload(*, page_index: int = 0, page_size: int = 50) -> dict[str, Any]:
    if page_index < 0 or not 1 <= page_size <= 100:
        raise VoiceProviderEnrollmentError("invalid provider list pagination")
    return {
        "model": ENROLLMENT_MODEL,
        "input": {"action": "list", "page_size": page_size, "page_index": page_index},
    }


def build_delete_payload(voice: str) -> dict[str, Any]:
    if not VOICE_PATTERN.fullmatch(voice):
        raise VoiceProviderEnrollmentError("invalid provider voice id")
    return {"model": ENROLLMENT_MODEL, "input": {"action": "delete", "voice": voice}}


def _load_candidate_request(
    voice_root: Path,
    run_id: str,
    candidate_key: str,
    *,
    expected_run_manifest_byte_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    validate_readiness(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_run_manifest_byte_sha256,
    )
    manifest, _ = base._read_json(
        root,
        root / OUTPUT_DIRECTORY / run_id / "manifest.json",
        label="enrollment run manifest",
    )
    provider_contract = _object(manifest.get("provider_contract"), label="provider contract")
    run_region = base._require_string(provider_contract.get("region"), label="provider region")
    _region_contract(run_region)
    matches = [
        item
        for item in _array(manifest.get("candidates"), label="run candidates")
        if isinstance(item, dict) and item.get("candidate_key") == candidate_key
    ]
    if len(matches) != 1:
        raise VoiceProviderEnrollmentError("candidate key is not uniquely present in the enrollment run")
    candidate = matches[0]
    source = _object(candidate.get("source"), label="candidate source")
    audio_path = _safe_source_path(root, source.get("reference_audio_relative_path"), label="reference audio")
    text_path = _safe_source_path(root, source.get("transcript_relative_path"), label="enrollment transcript")
    audio = _read_bounded_file(audio_path, limit=MAX_AUDIO_BYTES, message="reference audio is unavailable")
    transcript_bytes = _read_bounded_file(
        text_path,
        limit=MAX_TRANSCRIPT_BYTES,
        message="enrollment transcript is unavailable",
    )
    _expect(base._sha256_bytes(audio), source.get("reference_audio_sha256"), label="reference audio SHA-256")
    _expect(len(audio), source.get("reference_audio_byte_count"), label="reference audio bytes")
    _expect(
        base._sha256_bytes(transcript_bytes), source.get("transcript_utf8_sha256"), label="transcript SHA-256"
    )
    _expect(len(transcript_bytes), source.get("transcript_byte_count"), label="transcript bytes")
    try:
        transcript = transcript_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VoiceProviderEnrollmentError("enrollment transcript is not UTF-8") from error
    wav = _validate_wav(audio, _object(source.get("audio_format"), label="pinned audio format"))
    payload = build_create_payload(
        preferred_name=base._require_string(candidate.get("preferred_name"), label="preferred name"),
        audio=audio,
        transcript=transcript,
    )
    context = {
        "run_id": run_id,
        "run_manifest_sha256": manifest.get("manifest_sha256"),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_key": candidate_key,
        "preferred_name": candidate.get("preferred_name"),
        "target_model": MODEL,
        "region": run_region,
        "reference_audio_sha256": base._sha256_bytes(audio),
        "reference_audio_byte_count": len(audio),
        "submitted_transcript_sha256": base._sha256_bytes(transcript.strip().encode("utf-8")),
        "wav": wav,
    }
    return manifest, payload, context


def _redacted_create(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    clone = copy.deepcopy(payload)
    clone["input"]["audio"]["data"] = f"<redacted source-audio-sha256={context['reference_audio_sha256']}>"
    clone["input"]["text"] = (
        f"<redacted submitted-transcript-sha256={context['submitted_transcript_sha256']}>"
    )
    return clone


def inspect_candidate(
    voice_root: Path,
    run_id: str,
    candidate_key: str,
    *,
    workspace_id: str = "",
    expected_run_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    _, payload, context = _load_candidate_request(
        voice_root,
        run_id,
        candidate_key,
        expected_run_manifest_byte_sha256=expected_run_manifest_byte_sha256,
    )
    run_region = context["region"]
    region_details = _region_contract(run_region)
    endpoint = enrollment_endpoint(workspace_id, region=run_region) if workspace_id.strip() else None
    blockers = []
    if endpoint is None:
        blockers.append(region_details["inspect_workspace_blocker"])
    blockers.extend(
        [
            "api_key_availability_intentionally_not_checked_in_offline_inspect",
            "live_create_confirmations_not_supplied",
            "source_audio_retention_uncertainty_not_accepted",
        ]
    )
    return {
        "status": "offline_inspection_complete",
        "dry_run": True,
        "provider_interactions_performed": False,
        "credentials_read": False,
        "endpoint": endpoint,
        "request": _redacted_create(payload, context),
        "context": context,
        "direct_create_unit_price_ceiling_usd": str(CREATE_UNIT_PRICE_USD),
        "whole_run_direct_creation_cost_ceiling_usd": str(CREATE_COST_CEILING_USD),
        "blockers": blockers,
    }


def _dotenv_values(path: Path) -> dict[str, str]:
    try:
        payload = _read_bounded_file(
            path,
            limit=MAX_DOTENV_BYTES,
            message="Provider dotenv file is unavailable or too large",
        )
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VoiceProviderEnrollmentError("Provider dotenv file is not UTF-8") from error
    provider_keys = {
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "EVIDENCE_REVIEW_API_KEY",
        "EVIDENCE_REVIEW_BASE_URL",
    }
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if match is None:
            continue
        name, raw = match.groups()
        if name not in provider_keys:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name in values:
            existing = values[name]
            if existing and value and existing != value:
                raise VoiceProviderEnrollmentError(
                    f"Provider dotenv contains conflicting duplicate key {name!r} "
                    f"at line {line_number}"
                )
            if not existing and value:
                values[name] = value
            continue
        values[name] = value
    return values


def _dotenv_host(values: dict[str, str], name: str) -> str | None:
    raw = values.get(name, "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise VoiceProviderEnrollmentError(f"{name} must be an absolute HTTPS URL without userinfo")
    return parsed.hostname.lower()


def _default_dotenv_path(voice_root: Path) -> Path:
    root = base._absolute_lexical(voice_root)
    if root.name.casefold() != "voice" or root.parent.name.casefold() != "data":
        raise VoiceProviderEnrollmentError(
            "automatic Provider dotenv discovery requires <project>/Data/Voice"
        )
    return root.parent.parent / "App" / ".env"


def _read_secret(
    path: Path | None,
    *,
    voice_root: Path | None = None,
    region: str = REGION,
    dotenv_file: Path | None = None,
) -> str:
    region_details = _region_contract(region)
    value = ""
    if path is not None:
        if path.is_symlink():
            raise VoiceProviderEnrollmentError("API key file must not be a symlink")
        try:
            value = (
                _read_bounded_file(
                    path.resolve(),
                    limit=512,
                    message="API key file is unavailable or too large",
                )
                .decode("utf-8")
                .strip()
            )
        except UnicodeDecodeError as error:
            raise VoiceProviderEnrollmentError("API key file is not UTF-8") from error
    else:
        value = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not value and voice_root is not None:
            selected_dotenv = (
                base._absolute_lexical(dotenv_file)
                if dotenv_file is not None
                else _default_dotenv_path(voice_root)
            )
            values = _dotenv_values(selected_dotenv)
            expected_host = region_details["dotenv_base_url_host"]
            dashscope_host = _dotenv_host(values, "DASHSCOPE_BASE_URL")
            value = values.get("DASHSCOPE_API_KEY", "").strip()
            if value and dashscope_host != expected_host:
                raise VoiceProviderEnrollmentError(
                    "DASHSCOPE_API_KEY dotenv region does not match the enrollment run"
                )
            if not value and region == CHINA_REGION:
                evidence_host = _dotenv_host(values, "EVIDENCE_REVIEW_BASE_URL")
                alias_value = values.get("EVIDENCE_REVIEW_API_KEY", "").strip()
                if alias_value:
                    if evidence_host != expected_host or dashscope_host != expected_host:
                        raise VoiceProviderEnrollmentError(
                            "EVIDENCE_REVIEW_API_KEY is not bound to the expected Beijing host"
                        )
                    value = alias_value
    if not value or len(value) > 512 or any(character.isspace() for character in value):
        raise VoiceProviderEnrollmentError(
            f"a valid {region_details['region_name']} DashScope API key is required"
        )
    return value


def provider_request(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    opener: Any | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Project-Snow-voice-enrollment/1.0",
        },
        method="POST",
    )
    client = opener or urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        response = client.open(request, timeout=60)
    except (urllib.error.URLError, TimeoutError) as error:
        raise VoiceProviderEnrollmentError("provider request failed; reconcile before retry") from error
    with response:
        if getattr(response, "status", 200) != 200 or response.geturl() != endpoint:
            raise VoiceProviderEnrollmentError(
                "provider returned an invalid response; reconcile before retry"
            )
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise VoiceProviderEnrollmentError(
            "provider response exceeded the safety limit; reconcile before retry"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VoiceProviderEnrollmentError(
            "provider response was not valid JSON; reconcile before retry"
        ) from error
    if not isinstance(value, dict) or not isinstance(value.get("output"), dict):
        raise VoiceProviderEnrollmentError(
            "provider response lacked an output object; reconcile before retry"
        )
    return value


def validate_create_response(value: dict[str, Any]) -> dict[str, Any]:
    output = value.get("output") if isinstance(value.get("output"), dict) else {}
    voice = str(output.get("voice") or "")
    if not VOICE_PATTERN.fullmatch(voice) or output.get("target_model") != MODEL:
        raise VoiceProviderEnrollmentError(
            "provider created an invalid or mismatched voice; reconcile before retry"
        )
    if output.get("fallback_mode") is True:
        reason = str(output.get("fallback_reason") or "unknown")
        raise VoiceProviderEnrollmentError(
            f"provider reported a fallback voice; reconcile/delete it before retry ({reason})"
        )
    return {
        "voice": voice,
        "target_model": MODEL,
        "request_id": str(value.get("request_id") or ""),
        "fallback_mode": False,
    }


def _audit_root(root: Path, run_id: str) -> Path:
    safe_id = base._safe_identifier(run_id, RUN_ID_PATTERN, label="run_id")
    output = root / AUDIT_DIRECTORY
    if output.exists():
        base._require_safe_existing_path(root, output, label="provider audit directory", directory=True)
    else:
        try:
            output.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(root, output, label="provider audit directory", directory=True)
    directory = output / safe_id
    if directory.exists():
        base._require_safe_existing_path(root, directory, label="run audit directory", directory=True)
    else:
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(root, directory, label="run audit directory", directory=True)
    return directory


def _write_audit(directory: Path, record: dict[str, Any], name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{8,180}\.json", name):
        raise VoiceProviderEnrollmentError("invalid audit record name")
    document = dict(record)
    document["record_sha256"] = base._semantic_sha256(document)
    path = directory / name
    try:
        with path.open("xb") as stream:
            stream.write(_pretty_json_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise VoiceProviderEnrollmentError("provider audit record could not be committed") from error
    return path


def _audit_state(directory: Path) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        payload = _read_bounded_file(path, limit=MAX_AUDIT_BYTES, message="provider audit record is invalid")
        try:
            record = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VoiceProviderEnrollmentError("provider audit record is invalid") from error
        if not isinstance(record, dict) or record.get("schema_version") != AUDIT_SCHEMA:
            raise VoiceProviderEnrollmentError("provider audit record is invalid")
        base._verify_semantic_hash(record, field="record_sha256", label="provider audit record")
        _expect(record.get("run_id"), directory.name, label="provider audit run ID")
        attempt_id = base._safe_identifier(
            record.get("attempt_id"), ATTEMPT_ID_PATTERN, label="provider attempt ID"
        )
        stage = record.get("stage")
        target = attempts if stage == "attempt_started" else results if stage == "result_committed" else None
        if target is None or attempt_id in target:
            raise VoiceProviderEnrollmentError("provider audit history is ambiguous")
        target[attempt_id] = record
    orphan_results = set(results) - set(attempts)
    if orphan_results:
        raise VoiceProviderEnrollmentError("provider audit result lacks its attempt record")
    pending = [record for attempt_id, record in attempts.items() if attempt_id not in results]
    successful = [
        record
        for record in results.values()
        if record.get("action") == "create" and record.get("outcome") == "voice_created"
    ]
    return {"attempts": attempts, "results": results, "pending": pending, "successful_creates": successful}


def _validate_live_confirmations(
    *,
    run_id: str,
    candidate_key: str,
    expected_region: str,
    confirm_run_id: str,
    confirm_candidate_key: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_external_upload_to_singapore: bool,
    confirm_external_upload_to_region: bool,
    confirm_unverified_fanwork_source_risk: bool,
    confirm_provider_terms_and_voice_cloning_consent: bool,
    confirm_undocumented_source_audio_retention: bool,
) -> None:
    _expect(confirm_run_id, run_id, label="confirmed run ID")
    _expect(confirm_candidate_key, candidate_key, label="confirmed candidate key")
    _expect(confirm_model, MODEL, label="confirmed model")
    _expect(confirm_region, expected_region, label="confirmed region")
    try:
        confirmed_cost = Decimal(confirm_cost_ceiling_usd)
    except InvalidOperation as error:
        raise VoiceProviderEnrollmentError("confirmed cost ceiling must be a decimal USD value") from error
    _expect(confirmed_cost, CREATE_COST_CEILING_USD, label="confirmed whole-run direct creation cost ceiling")
    region_details = _region_contract(expected_region)
    upload_confirmed = confirm_external_upload_to_region or (
        expected_region == REGION and confirm_external_upload_to_singapore
    )
    required = {
        region_details["upload_confirmation_label"]: upload_confirmed,
        "unverified fanwork source risk": confirm_unverified_fanwork_source_risk,
        "provider terms and voice-cloning consent": confirm_provider_terms_and_voice_cloning_consent,
        "undocumented source-audio retention": confirm_undocumented_source_audio_retention,
    }
    for label, value in required.items():
        if value is not True:
            raise VoiceProviderEnrollmentError(f"live create requires explicit confirmation of {label}")


def create_one(
    voice_root: Path,
    run_id: str,
    candidate_key: str,
    *,
    workspace_id: str,
    api_key_file: Path | None,
    expected_run_manifest_byte_sha256: str | None,
    confirm_run_id: str,
    confirm_candidate_key: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_external_upload_to_singapore: bool,
    confirm_unverified_fanwork_source_risk: bool,
    confirm_provider_terms_and_voice_cloning_consent: bool,
    confirm_undocumented_source_audio_retention: bool,
    confirm_external_upload_to_region: bool = False,
    dotenv_file: Path | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    _, payload, context = _load_candidate_request(
        root,
        run_id,
        candidate_key,
        expected_run_manifest_byte_sha256=expected_run_manifest_byte_sha256,
    )
    run_region = context["region"]
    _validate_live_confirmations(
        run_id=run_id,
        candidate_key=candidate_key,
        expected_region=run_region,
        confirm_run_id=confirm_run_id,
        confirm_candidate_key=confirm_candidate_key,
        confirm_model=confirm_model,
        confirm_region=confirm_region,
        confirm_cost_ceiling_usd=confirm_cost_ceiling_usd,
        confirm_external_upload_to_singapore=confirm_external_upload_to_singapore,
        confirm_external_upload_to_region=confirm_external_upload_to_region,
        confirm_unverified_fanwork_source_risk=confirm_unverified_fanwork_source_risk,
        confirm_provider_terms_and_voice_cloning_consent=confirm_provider_terms_and_voice_cloning_consent,
        confirm_undocumented_source_audio_retention=confirm_undocumented_source_audio_retention,
    )
    endpoint = enrollment_endpoint(workspace_id, region=run_region)
    directory = _audit_root(root, run_id)
    state = _audit_state(directory)
    if any(record.get("candidate_key") == candidate_key for record in state["pending"]):
        raise VoiceProviderEnrollmentError(
            "candidate has an uncertain provider attempt; list/reconcile before retry"
        )
    if any(record.get("candidate_key") == candidate_key for record in state["successful_creates"]):
        raise VoiceProviderEnrollmentError("candidate already has a successful provider create receipt")
    if len(state["successful_creates"]) >= PLANNED_CREATE_COUNT:
        raise VoiceProviderEnrollmentError("whole-run successful-create ceiling is already exhausted")
    api_key = _read_secret(
        api_key_file,
        voice_root=root,
        region=run_region,
        dotenv_file=dotenv_file,
    )
    attempt_id = f"provider-attempt-{uuid.uuid4().hex}"
    recorded_at = dt.datetime.now(dt.UTC).isoformat()
    attempt = {
        "schema_version": AUDIT_SCHEMA,
        "attempt_id": attempt_id,
        "stage": "attempt_started",
        "action": "create",
        "recorded_at": recorded_at,
        **context,
        "workspace_id": workspace_id.strip().lower(),
        "endpoint": endpoint,
        "rights_basis": RIGHTS_BASIS,
        "written_source_authorization_present": False,
        "source_audio_retention_status": SOURCE_RETENTION_STATUS,
        "whole_run_direct_creation_cost_ceiling_usd": str(CREATE_COST_CEILING_USD),
        "successful_create_unit_price_upper_bound_usd": str(CREATE_UNIT_PRICE_USD),
        "request": _redacted_create(payload, context),
    }
    attempt_path = _write_audit(directory, attempt, f"{attempt_id}-attempt.json")
    response = validate_create_response(
        provider_request(endpoint=endpoint, api_key=api_key, payload=payload, opener=opener)
    )
    result = {
        "schema_version": AUDIT_SCHEMA,
        "attempt_id": attempt_id,
        "stage": "result_committed",
        "action": "create",
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
        "run_id": run_id,
        "candidate_key": candidate_key,
        "outcome": "voice_created",
        "provider_voice_id": response["voice"],
        "provider_request_id": response["request_id"],
        "target_model": response["target_model"],
        "fallback_mode": False,
        "actual_provider_charge_usd": None,
        "charge_status": "unknown_until_provider_billing_or_free_quota_reconciliation",
        "direct_create_unit_price_upper_bound_usd": str(CREATE_UNIT_PRICE_USD),
        "attempt_record_relative_path": attempt_path.name,
    }
    result_path = _write_audit(directory, result, f"{attempt_id}-result.json")
    return {
        "status": "voice_created",
        "run_id": run_id,
        "candidate_key": candidate_key,
        "provider_voice_id": response["voice"],
        "provider_request_id": response["request_id"],
        "attempt_audit_path": str(attempt_path),
        "result_audit_path": str(result_path),
        "next_action": "render_same_prompt_pair_for_blind_test_then_delete_loser_after_decision",
    }


def list_provider_voices(
    voice_root: Path,
    run_id: str,
    *,
    workspace_id: str,
    api_key_file: Path | None,
    page_index: int,
    page_size: int,
    dotenv_file: Path | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    validate_readiness(root, run_id)
    manifest, _ = base._read_json(
        root,
        root / OUTPUT_DIRECTORY / run_id / "manifest.json",
        label="enrollment run manifest",
    )
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    run_region = base._require_string(provider.get("region"), label="provider region")
    endpoint = enrollment_endpoint(workspace_id, region=run_region)
    response = provider_request(
        endpoint=endpoint,
        api_key=_read_secret(
            api_key_file,
            voice_root=root,
            region=run_region,
            dotenv_file=dotenv_file,
        ),
        payload=build_list_payload(page_index=page_index, page_size=page_size),
        opener=opener,
    )
    return {
        "status": "provider_list_received",
        "region": run_region,
        "page_index": page_index,
        "page_size": page_size,
        "request_id": str(response.get("request_id") or ""),
        "output": response["output"],
        "warning": "provider_voice_ids_are_private_operator_data",
    }


def inspect_delete(
    voice_root: Path,
    run_id: str,
    voice: str,
    *,
    workspace_id: str = "",
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    safe_run_id = base._safe_identifier(run_id, RUN_ID_PATTERN, label="run_id")
    validate_readiness(root, safe_run_id)
    manifest, _ = base._read_json(
        root,
        root / OUTPUT_DIRECTORY / safe_run_id / "manifest.json",
        label="enrollment run manifest",
    )
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    run_region = base._require_string(provider.get("region"), label="provider region")
    region_details = _region_contract(run_region)
    payload = build_delete_payload(voice)
    endpoint = enrollment_endpoint(workspace_id, region=run_region) if workspace_id.strip() else None
    blockers = []
    if endpoint is None:
        blockers.append(region_details["inspect_workspace_blocker"])
    blockers.extend(
        [
            "api_key_availability_intentionally_not_checked_in_offline_inspect",
            "exact_voice_deletion_confirmation_not_supplied",
            "delete_does_not_restore_free_quota_confirmation_not_supplied",
        ]
    )
    return {
        "status": "offline_delete_inspection_complete",
        "dry_run": True,
        "run_id": safe_run_id,
        "provider_interactions_performed": False,
        "credentials_read": False,
        "endpoint": endpoint,
        "request": payload,
        "blockers": blockers,
    }


def delete_one(
    voice_root: Path,
    run_id: str,
    voice: str,
    *,
    workspace_id: str,
    api_key_file: Path | None,
    confirm_voice: str,
    reason: str,
    confirm_delete_does_not_restore_free_quota: bool,
    dotenv_file: Path | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    validate_readiness(root, run_id)
    manifest, _ = base._read_json(
        root,
        root / OUTPUT_DIRECTORY / run_id / "manifest.json",
        label="enrollment run manifest",
    )
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    run_region = base._require_string(provider.get("region"), label="provider region")
    payload = build_delete_payload(voice)
    _expect(confirm_voice, voice, label="confirmed provider voice ID")
    exact_reason = reason.strip()
    if not 3 <= len(exact_reason) <= 500 or "\x00" in exact_reason:
        raise VoiceProviderEnrollmentError("provider voice deletion requires a bounded reason")
    if confirm_delete_does_not_restore_free_quota is not True:
        raise VoiceProviderEnrollmentError(
            "provider voice deletion requires confirmation that deletion does not restore free quota"
        )
    endpoint = enrollment_endpoint(workspace_id, region=run_region)
    directory = _audit_root(root, run_id)
    state = _audit_state(directory)
    if any(
        record.get("action") == "delete" and record.get("provider_voice_id") == voice
        for record in state["pending"]
    ):
        raise VoiceProviderEnrollmentError(
            "voice has an uncertain provider delete attempt; list/reconcile before retry"
        )
    if any(
        record.get("action") == "delete"
        and record.get("outcome") == "voice_deleted"
        and record.get("provider_voice_id") == voice
        for record in state["results"].values()
    ):
        raise VoiceProviderEnrollmentError("voice already has a successful provider delete receipt")
    api_key = _read_secret(
        api_key_file,
        voice_root=root,
        region=run_region,
        dotenv_file=dotenv_file,
    )
    attempt_id = f"provider-attempt-{uuid.uuid4().hex}"
    attempt = {
        "schema_version": AUDIT_SCHEMA,
        "attempt_id": attempt_id,
        "stage": "attempt_started",
        "action": "delete",
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
        "run_id": run_id,
        "provider_voice_id": voice,
        "workspace_id": workspace_id.strip().lower(),
        "endpoint": endpoint,
        "reason": exact_reason,
        "delete_does_not_restore_free_quota": True,
        "request": payload,
    }
    attempt_path = _write_audit(directory, attempt, f"{attempt_id}-attempt.json")
    response = provider_request(endpoint=endpoint, api_key=api_key, payload=payload, opener=opener)
    output = _object(response.get("output"), label="provider delete output")
    _expect(output.get("voice"), voice, label="provider deleted voice ID")
    result = {
        "schema_version": AUDIT_SCHEMA,
        "attempt_id": attempt_id,
        "stage": "result_committed",
        "action": "delete",
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
        "run_id": run_id,
        "provider_voice_id": voice,
        "outcome": "voice_deleted",
        "provider_request_id": str(response.get("request_id") or ""),
        "delete_does_not_restore_free_quota": True,
        "attempt_record_relative_path": attempt_path.name,
    }
    result_path = _write_audit(directory, result, f"{attempt_id}-result.json")
    return {
        "status": "voice_deleted",
        "run_id": run_id,
        "provider_voice_id": voice,
        "provider_request_id": str(response.get("request_id") or ""),
        "attempt_audit_path": str(attempt_path),
        "result_audit_path": str(result_path),
        "free_quota_restored": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", type=Path, required=True)
    parser.add_argument("--workspace-id", default=os.getenv("DASHSCOPE_WORKSPACE_ID", ""))
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--dotenv-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="dry-run or write an immutable offline run contract")
    prepare.add_argument("--preflight-id", required=True)
    prepare.add_argument("--expect-preflight-manifest-byte-sha256", required=True)
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--region", choices=tuple(REGION_CONTRACTS), default=REGION)
    prepare.add_argument("--confirm-offline-only", action="store_true")
    prepare.add_argument("--execute", action="store_true")

    validate = commands.add_parser("validate", help="revalidate an immutable run and all source assets")
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--expect-run-manifest-byte-sha256")

    inspect = commands.add_parser("inspect", help="build one fully redacted create request offline")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--candidate-key", choices=tuple(PREFERRED_NAME_PREFIX), required=True)
    inspect.add_argument("--expect-run-manifest-byte-sha256")

    create = commands.add_parser("create-one", help="create exactly one provider voice")
    create.add_argument("--run-id", required=True)
    create.add_argument("--candidate-key", choices=tuple(PREFERRED_NAME_PREFIX), required=True)
    create.add_argument("--expect-run-manifest-byte-sha256")
    create.add_argument("--confirm-run-id", default="")
    create.add_argument("--confirm-candidate-key", default="")
    create.add_argument("--confirm-model", default="")
    create.add_argument("--confirm-region", default="")
    create.add_argument("--confirm-cost-ceiling-usd", default="")
    create.add_argument("--confirm-external-upload-to-singapore", action="store_true")
    create.add_argument("--confirm-external-upload-to-region", action="store_true")
    create.add_argument("--confirm-unverified-fanwork-source-risk", action="store_true")
    create.add_argument("--confirm-provider-terms-and-voice-cloning-consent", action="store_true")
    create.add_argument("--confirm-undocumented-source-audio-retention", action="store_true")
    create.add_argument("--execute", action="store_true")

    listing = commands.add_parser("list", help="list provider voices for operator reconciliation")
    listing.add_argument("--run-id", required=True)
    listing.add_argument("--page-index", type=int, default=0)
    listing.add_argument("--page-size", type=int, default=50)

    delete = commands.add_parser("delete-one", help="delete exactly one provider voice")
    delete.add_argument("--run-id", required=True)
    delete.add_argument("--voice", required=True)
    delete.add_argument("--confirm-voice", default="")
    delete.add_argument("--reason", default="")
    delete.add_argument("--confirm-delete-does-not-restore-free-quota", action="store_true")
    delete.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            if arguments.execute and not arguments.confirm_offline_only:
                raise VoiceProviderEnrollmentError("--execute requires --confirm-offline-only")
            artifacts, destination = build_readiness(
                arguments.voice_root,
                preflight_id=arguments.preflight_id,
                expected_preflight_manifest_byte_sha256=(arguments.expect_preflight_manifest_byte_sha256),
                prepared_at=arguments.prepared_at,
                region=arguments.region,
            )
            write_status = None
            if arguments.execute:
                write_status, existing = write_readiness(arguments.voice_root, artifacts, destination)
                if write_status == "existing_conflict":
                    raise VoiceProviderEnrollmentError(
                        "an immutable enrollment run already exists with conflicting content"
                    )
                artifacts["manifest"] = existing
            manifest = artifacts["manifest"]
            result = {
                "status": "ok",
                "mode": "execute" if arguments.execute else "dry_run",
                "write_status": write_status,
                "path": str(destination),
                "run_id": manifest["run_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "candidate_keys": [item["candidate_key"] for item in manifest["candidates"]],
                "provider_interactions_performed": False,
                "credentials_read": False,
                "next_status": manifest["next_status"],
            }
        elif arguments.command == "validate":
            result = validate_readiness(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
        elif arguments.command == "inspect":
            result = inspect_candidate(
                arguments.voice_root,
                arguments.run_id,
                arguments.candidate_key,
                workspace_id=arguments.workspace_id,
                expected_run_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
        elif arguments.command == "create-one":
            if not arguments.execute:
                result = inspect_candidate(
                    arguments.voice_root,
                    arguments.run_id,
                    arguments.candidate_key,
                    workspace_id=arguments.workspace_id,
                    expected_run_manifest_byte_sha256=(arguments.expect_run_manifest_byte_sha256),
                )
                result["note"] = "create-one remains a dry run unless --execute is present"
            else:
                result = create_one(
                    arguments.voice_root,
                    arguments.run_id,
                    arguments.candidate_key,
                    workspace_id=arguments.workspace_id,
                    api_key_file=arguments.api_key_file,
                    expected_run_manifest_byte_sha256=(arguments.expect_run_manifest_byte_sha256),
                    confirm_run_id=arguments.confirm_run_id,
                    confirm_candidate_key=arguments.confirm_candidate_key,
                    confirm_model=arguments.confirm_model,
                    confirm_region=arguments.confirm_region,
                    confirm_cost_ceiling_usd=arguments.confirm_cost_ceiling_usd,
                    confirm_external_upload_to_singapore=(arguments.confirm_external_upload_to_singapore),
                    confirm_external_upload_to_region=(arguments.confirm_external_upload_to_region),
                    confirm_unverified_fanwork_source_risk=(arguments.confirm_unverified_fanwork_source_risk),
                    confirm_provider_terms_and_voice_cloning_consent=(
                        arguments.confirm_provider_terms_and_voice_cloning_consent
                    ),
                    confirm_undocumented_source_audio_retention=(
                        arguments.confirm_undocumented_source_audio_retention
                    ),
                    dotenv_file=arguments.dotenv_file,
                )
        elif arguments.command == "list":
            result = list_provider_voices(
                arguments.voice_root,
                arguments.run_id,
                workspace_id=arguments.workspace_id,
                api_key_file=arguments.api_key_file,
                page_index=arguments.page_index,
                page_size=arguments.page_size,
                dotenv_file=arguments.dotenv_file,
            )
        elif not arguments.execute:
            result = inspect_delete(
                arguments.voice_root,
                arguments.run_id,
                arguments.voice,
                workspace_id=arguments.workspace_id,
            )
            result["note"] = "delete-one remains a dry run unless --execute is present"
        else:
            result = delete_one(
                arguments.voice_root,
                arguments.run_id,
                arguments.voice,
                workspace_id=arguments.workspace_id,
                api_key_file=arguments.api_key_file,
                confirm_voice=arguments.confirm_voice,
                reason=arguments.reason,
                confirm_delete_does_not_restore_free_quota=(
                    arguments.confirm_delete_does_not_restore_free_quota
                ),
                dotenv_file=arguments.dotenv_file,
            )
    except (OSError, base.VoiceParalinguisticError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
