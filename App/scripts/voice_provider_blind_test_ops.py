# ruff: noqa: E501
"""Prepare, render, and finalize a fail-closed custom-voice blind test.

The private run manifest pins the reviewed prompt plan, four successful Beijing
voice-enrollment receipts, cryptographically random opaque labels, synthesis
parameters, and a whole-run character/cost ceiling. Each live synthesis writes
an attempt record before opening the WebSocket and a result record only after a
validated WAV is durably committed. An attempt without a result blocks the
entire run because realtime synthesis has no idempotency or output-list API.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import html
import io
import json
import math
import os
import re
import secrets
import sys
import time
import unicodedata
import uuid
import wave
from array import array
from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_provider_enrollment_ops as enrollment
    from . import voice_provider_preflight_ops as preflight
else:
    import voice_paralinguistic_ops as base
    import voice_provider_enrollment_ops as enrollment
    import voice_provider_preflight_ops as preflight


SCHEMA = "project-snow-private-voice-provider-blind-test-run-1"
PUBLIC_SCHEMA = "project-snow-local-voice-provider-blind-test-review-1"
RATING_SCHEMA = "project-snow-local-voice-provider-blind-test-ratings-1"
AUDIT_SCHEMA = "project-snow-private-voice-provider-blind-test-audit-1"
POLICY_VERSION = "project-snow-qwen-vc-blind-test-fail-closed-1"
OUTPUT_DIRECTORY = "tts_provider_blind_tests"
RUN_ID_PATTERN = re.compile(r"voice-provider-blind-test-run-[0-9a-f]{20}\Z")
OUTPUT_ID_PATTERN = re.compile(r"blind-output-[0-9a-f]{16}\Z")
ATTEMPT_ID_PATTERN = re.compile(r"synthesis-attempt-[0-9a-f]{32}\Z")
OPAQUE_ID_PATTERN = re.compile(r"sample-[a-z0-9]{4}\Z")

REGION = enrollment.CHINA_REGION
MODEL = enrollment.MODEL
WEBSOCKET_ENDPOINT = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
SAMPLE_RATE_HZ = 24_000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
RESPONSE_FORMAT = "pcm"
LANGUAGE_TYPE = "Chinese"
MODE = "commit"
PRICE_USD_PER_10K_CHARACTERS = Decimal("0.143353")
PRICE_CNY_PER_10K_CHARACTERS = Decimal("1")
COST_CEILING_USD = Decimal("0.02")
EXPECTED_OUTPUT_COUNT = 24
EXPECTED_INPUT_CODEPOINTS = 594
EXPECTED_BILLABLE_CHARACTERS = 1_052
MAX_AUDIO_BYTES = 12 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENT_COUNT = 20_000
EVENT_TIMEOUT_SECONDS = 60.0
WHOLE_EXCHANGE_TIMEOUT_SECONDS = 180.0

DEFAULT_PREFLIGHT_ID = "voice-provider-preflight-277b384f4a1451063562"
DEFAULT_PREFLIGHT_MANIFEST_BYTE_SHA256 = (
    "d9633af150a658183933941345524eeb265101a55a0b332dddf4c023529b383a"
)
DEFAULT_BLIND_PLAN_BYTE_SHA256 = "6cae1d4b0927816968e24fc5578c22615e4743724870add23ef5babc2f5ed321"
DEFAULT_ENROLLMENT_RUN_ID = "voice-provider-enrollment-run-955deef1ab01ba619f84"
DEFAULT_ENROLLMENT_MANIFEST_BYTE_SHA256 = (
    "eb388206cae3b9bae20c53e1ab49b866daa0d93c176b98fbd8422db287bba701"
)

CHARACTER_ORDER = ("vidya", "chenxing")
CANDIDATE_ORDER = ("vidya-a", "vidya-b", "chenxing-a", "chenxing-b")
RATING_DIMENSIONS = (
    "speaker_identity_similarity",
    "intelligibility",
    "naturalness",
    "character_fit",
    "prosody_and_breath_stability",
    "artifact_absence",
)
CRITICAL_FAILURES = (
    "wrong_or_unstable_voice_identity",
    "truncation_or_missing_content",
    "hallucinated_or_repeated_words",
    "clipping_discontinuity_or_audible_seam",
)
OFFICIAL_SOURCES = {
    "pricing": "https://www.alibabacloud.com/help/en/model-studio/model-pricing",
    "realtime_user_guide": (
        "https://www.alibabacloud.com/help/en/model-studio/realtime-tts-user-guide"
    ),
    "websocket_api": (
        "https://www.alibabacloud.com/help/zh/model-studio/"
        "interactive-process-of-qwen-tts-realtime-synthesis"
    ),
    "client_events": (
        "https://www.alibabacloud.com/help/zh/model-studio/qwen-tts-realtime-client-events"
    ),
    "server_events": (
        "https://www.alibabacloud.com/help/zh/model-studio/qwen-tts-realtime-server-events"
    ),
}


class VoiceProviderBlindTestError(base.VoiceParalinguisticError):
    """Raised when a blind-test operation cannot be proven safe."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceProviderBlindTestError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceProviderBlindTestError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceProviderBlindTestError(f"{label} must be an array")
    return value


def _string(value: Any, *, label: str) -> str:
    try:
        return base._require_string(value, label=label)
    except base.VoiceParalinguisticError as error:
        raise VoiceProviderBlindTestError(str(error)) from error


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _validate_iso_timestamp(value: Any, *, label: str) -> str:
    text = _string(value, label=label)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise VoiceProviderBlindTestError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VoiceProviderBlindTestError(f"{label} must include a UTC offset")
    return text


def _create_safe_directory(root: Path, path: Path, *, label: str) -> Path:
    if path.exists():
        return base._require_safe_existing_path(root, path, label=label, directory=True)
    try:
        path.mkdir()
    except FileExistsError:
        pass
    return base._require_safe_existing_path(root, path, label=label, directory=True)


def _write_new_file(path: Path, payload: bytes, *, label: str) -> Path:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise VoiceProviderBlindTestError(f"{label} already exists") from error
    except OSError as error:
        raise VoiceProviderBlindTestError(f"{label} could not be committed") from error
    return path


def _write_atomic_new(path: Path, payload: bytes, *, label: str) -> Path:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError as error:
        raise VoiceProviderBlindTestError(f"{label} already exists") from error
    except OSError as error:
        raise VoiceProviderBlindTestError(f"{label} could not be committed atomically") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def _relative_to_root(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _load_pinned_plan(
    root: Path,
    *,
    preflight_id: str,
    expected_preflight_manifest_byte_sha256: str,
    expected_blind_plan_byte_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = preflight.validate_preflight(
        root,
        preflight_id,
        expected_manifest_byte_sha256=expected_preflight_manifest_byte_sha256,
    )
    _expect(
        validation.get("blind_test_plan_byte_sha256"),
        base._require_sha256(
            expected_blind_plan_byte_sha256,
            label="expected blind plan byte SHA-256",
        ),
        label="blind plan byte SHA-256",
    )
    path = root / preflight.OUTPUT_DIRECTORY / preflight_id / "blind_test_plan.json"
    plan, payload = base._read_json(root, path, label="blind test plan")
    _expect(plan.get("schema_version"), preflight.PLAN_SCHEMA, label="blind plan schema")
    _expect(plan.get("status"), "planned_not_rendered", label="blind plan status")
    _expect(
        base._sha256_bytes(payload),
        expected_blind_plan_byte_sha256,
        label="blind plan byte SHA-256",
    )
    return plan, {
        "preflight_id": preflight_id,
        "manifest_sha256": validation["manifest_sha256"],
        "manifest_byte_sha256": validation["manifest_byte_sha256"],
        "blind_plan_relative_path": _relative_to_root(root, path),
        "blind_plan_semantic_sha256": base._semantic_sha256(plan),
        "blind_plan_byte_sha256": base._sha256_bytes(payload),
    }


def _load_enrollment_receipts(
    root: Path,
    *,
    enrollment_run_id: str,
    expected_enrollment_manifest_byte_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    enrollment.validate_readiness(
        root,
        enrollment_run_id,
        expected_manifest_byte_sha256=expected_enrollment_manifest_byte_sha256,
    )
    run_manifest_path = (
        root / enrollment.OUTPUT_DIRECTORY / enrollment_run_id / "manifest.json"
    )
    run_manifest, _ = base._read_json(root, run_manifest_path, label="enrollment run manifest")
    provider = _object(run_manifest.get("provider_contract"), label="enrollment provider contract")
    _expect(provider.get("region"), REGION, label="enrollment region")
    _expect(provider.get("target_model"), MODEL, label="enrollment target model")

    audit_directory = root / enrollment.AUDIT_DIRECTORY / enrollment_run_id
    base._require_safe_existing_path(
        root,
        audit_directory,
        label="enrollment audit directory",
        directory=True,
    )
    state = enrollment._audit_state(audit_directory)
    _expect(len(state["pending"]), 0, label="uncertain enrollment attempts")
    successes = state["successful_creates"]
    _expect(len(successes), len(CANDIDATE_ORDER), label="successful enrollment receipts")

    anchors: list[dict[str, Any]] = []
    voices: dict[str, str] = {}
    for result in successes:
        key = _string(result.get("candidate_key"), label="enrollment result candidate key")
        if key not in CANDIDATE_ORDER or key in voices:
            raise VoiceProviderBlindTestError("enrollment receipts do not map one-to-one to candidates")
        _expect(result.get("run_id"), enrollment_run_id, label=f"{key} enrollment run")
        _expect(result.get("target_model"), MODEL, label=f"{key} target model")
        _expect(result.get("fallback_mode"), False, label=f"{key} fallback mode")
        voice = _string(result.get("provider_voice_id"), label=f"{key} provider voice ID")
        if not enrollment.VOICE_PATTERN.fullmatch(voice):
            raise VoiceProviderBlindTestError(f"{key} has an invalid provider voice ID")
        attempt_id = base._safe_identifier(
            result.get("attempt_id"),
            enrollment.ATTEMPT_ID_PATTERN,
            label=f"{key} enrollment attempt ID",
        )
        result_path = audit_directory / f"{attempt_id}-result.json"
        document, payload = base._read_json(root, result_path, label=f"{key} enrollment result")
        _expect(document, result, label=f"{key} enrollment result reconstruction")
        base._verify_semantic_hash(document, field="record_sha256", label=f"{key} result")
        anchors.append(
            {
                "candidate_key": key,
                "relative_path": _relative_to_root(root, result_path),
                "record_sha256": document["record_sha256"],
                "byte_sha256": base._sha256_bytes(payload),
                "target_model": MODEL,
                "provider_voice_id_sha256": base._sha256_bytes(voice.encode("utf-8")),
            }
        )
        voices[key] = voice
    _expect(sorted(voices, key=CANDIDATE_ORDER.index), list(CANDIDATE_ORDER), label="voice receipt order")
    anchors.sort(key=lambda item: CANDIDATE_ORDER.index(item["candidate_key"]))
    return run_manifest, anchors, voices


def _random_label_id(existing: set[str]) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        value = "sample-" + "".join(secrets.choice(alphabet) for _ in range(4))
        if value not in existing:
            existing.add(value)
            return value


def _prompt_groups(plan: dict[str, Any]) -> list[dict[str, Any]]:
    groups = _array(plan.get("lexical_test_prompts"), label="lexical prompt groups")
    _expect(
        [item.get("character_slug") if isinstance(item, dict) else None for item in groups],
        list(CHARACTER_ORDER),
        label="lexical character order",
    )
    for raw in groups:
        group = _object(raw, label="lexical prompt group")
        cases = _array(group.get("cases"), label="lexical prompt cases")
        _expect(len(cases), 6, label=f"{group.get('character_slug')} prompt count")
    return groups


def _build_randomized_outputs(
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    operator_map = _array(plan.get("operator_only_candidate_map"), label="operator candidate map")
    by_character: dict[str, list[str]] = {slug: [] for slug in CHARACTER_ORDER}
    for raw in operator_map:
        item = _object(raw, label="operator candidate")
        key = _string(item.get("candidate_key"), label="operator candidate key")
        slug = _string(item.get("character_slug"), label="operator character slug")
        if key not in CANDIDATE_ORDER or slug not in by_character:
            raise VoiceProviderBlindTestError("blind plan contains an unexpected candidate")
        by_character[slug].append(key)
    _expect(
        [key for slug in CHARACTER_ORDER for key in by_character[slug]],
        list(CANDIDATE_ORDER),
        label="blind plan candidate order",
    )

    used_labels: set[str] = set()
    mappings: list[dict[str, Any]] = []
    label_by_candidate: dict[str, dict[str, str]] = {}
    for slug in CHARACTER_ORDER:
        candidates = list(by_character[slug])
        secrets.SystemRandom().shuffle(candidates)
        labels = [_random_label_id(used_labels), _random_label_id(used_labels)]
        entries = []
        for candidate, label_id in zip(candidates, labels, strict=True):
            display = f"样本 {label_id.removeprefix('sample-').upper()}"
            entry = {
                "candidate_key": candidate,
                "opaque_label_id": label_id,
                "display_label": display,
            }
            entries.append(entry)
            label_by_candidate[candidate] = entry
        mappings.append({"character_slug": slug, "labels": entries})

    outputs: list[dict[str, Any]] = []
    for group in _prompt_groups(plan):
        slug = group["character_slug"]
        character_name = _string(group.get("runtime_character_name"), label="runtime character name")
        character_candidates = by_character[slug]
        for case_index, raw_case in enumerate(group["cases"], start=1):
            case = _object(raw_case, label="lexical test case")
            case_id = _string(case.get("case_id"), label="case ID")
            category = _string(case.get("category"), label="case category")
            text = _string(case.get("text"), label="case text")
            display_order = list(character_candidates)
            secrets.SystemRandom().shuffle(display_order)
            order_by_candidate = {key: index for index, key in enumerate(display_order, start=1)}
            for candidate in character_candidates:
                label = label_by_candidate[candidate]
                filename = f"{case_index:02d}-{label['opaque_label_id']}.wav"
                outputs.append(
                    {
                        "output_id": f"blind-output-{secrets.token_hex(8)}",
                        "character_slug": slug,
                        "runtime_character_name": character_name,
                        "case_id": case_id,
                        "case_index": case_index,
                        "category": category,
                        "text": text,
                        "text_sha256": base._sha256_bytes(text.encode("utf-8")),
                        "input_character_count": len(text),
                        "billing_character_count": _billing_character_count(text),
                        "candidate_key": candidate,
                        "opaque_label_id": label["opaque_label_id"],
                        "display_label": label["display_label"],
                        "display_order": order_by_candidate[candidate],
                        "review_audio_relative_path": f"review/audio/{slug}/{filename}",
                    }
                )
    if len({item["output_id"] for item in outputs}) != len(outputs):
        raise VoiceProviderBlindTestError("cryptographic output IDs unexpectedly collided")
    _expect(len(outputs), EXPECTED_OUTPUT_COUNT, label="planned output count")
    _expect(
        sum(item["input_character_count"] for item in outputs),
        EXPECTED_INPUT_CODEPOINTS,
        label="planned input Unicode codepoints",
    )
    _expect(
        sum(item["billing_character_count"] for item in outputs),
        EXPECTED_BILLABLE_CHARACTERS,
        label="planned billable characters",
    )
    return mappings, outputs


def _estimated_cost(characters: int, unit_price: Decimal) -> Decimal:
    return Decimal(characters) * unit_price / Decimal(10_000)


def _billing_character_count(text: str) -> int:
    count = 0
    for character in text:
        name = unicodedata.name(character, "")
        count += 2 if "CJK" in name and "IDEOGRAPH" in name else 1
    return count


def build_run(
    voice_root: Path,
    *,
    preflight_id: str,
    expected_preflight_manifest_byte_sha256: str,
    expected_blind_plan_byte_sha256: str,
    enrollment_run_id: str,
    expected_enrollment_manifest_byte_sha256: str,
    prepared_at: str,
    prior_stage_usage_characters: int = 0,
    supersedes_run_ids: tuple[str, ...] = (),
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    prepared = _validate_iso_timestamp(prepared_at, label="prepared_at")
    if prior_stage_usage_characters < 0:
        raise VoiceProviderBlindTestError("prior stage usage characters must not be negative")
    superseded = [
        base._safe_identifier(value, RUN_ID_PATTERN, label="superseded blind-test run ID")
        for value in supersedes_run_ids
    ]
    if len(set(superseded)) != len(superseded):
        raise VoiceProviderBlindTestError("superseded blind-test run IDs must be unique")
    plan, source_preflight = _load_pinned_plan(
        root,
        preflight_id=preflight_id,
        expected_preflight_manifest_byte_sha256=expected_preflight_manifest_byte_sha256,
        expected_blind_plan_byte_sha256=expected_blind_plan_byte_sha256,
    )
    _, receipts, _ = _load_enrollment_receipts(
        root,
        enrollment_run_id=enrollment_run_id,
        expected_enrollment_manifest_byte_sha256=expected_enrollment_manifest_byte_sha256,
    )
    mappings, outputs = _build_randomized_outputs(plan)
    run_id = f"voice-provider-blind-test-run-{secrets.token_hex(10)}"
    estimated_usd = _estimated_cost(EXPECTED_BILLABLE_CHARACTERS, PRICE_USD_PER_10K_CHARACTERS)
    estimated_cny = _estimated_cost(EXPECTED_BILLABLE_CHARACTERS, PRICE_CNY_PER_10K_CHARACTERS)
    stage_characters = prior_stage_usage_characters + EXPECTED_BILLABLE_CHARACTERS
    estimated_stage_usd = _estimated_cost(stage_characters, PRICE_USD_PER_10K_CHARACTERS)
    if estimated_stage_usd > COST_CEILING_USD:
        raise VoiceProviderBlindTestError("prior usage plus planned run would exceed the stage cost ceiling")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "policy_version": POLICY_VERSION,
        "prepared_at": prepared,
        "status": "prepared_for_authorized_synthesis",
        "supersedes_blind_test_runs": superseded,
        "source_preflight": source_preflight,
        "source_enrollment": {
            "run_id": enrollment_run_id,
            "manifest_byte_sha256": expected_enrollment_manifest_byte_sha256,
            "result_receipts": receipts,
        },
        "provider_contract": {
            "provider_family": preflight.PROVIDER_FAMILY,
            "region": REGION,
            "region_name": "China (Beijing)",
            "target_model": MODEL,
            "websocket_endpoint": WEBSOCKET_ENDPOINT,
            "workspace_binding": "X-DashScope-WorkSpace request header",
            "synthesis_parameters": {
                "mode": MODE,
                "language_type": LANGUAGE_TYPE,
                "response_format": RESPONSE_FORMAT,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "channels": CHANNELS,
                "sample_width_bytes": SAMPLE_WIDTH_BYTES,
                "post_processing": "none",
                "loudness_policy": "same_non_destructive_policy_no_normalization",
            },
        },
        "pricing_contract": {
            "billing_counting_rule": "cjk_ideographs_count_as_two_characters_other_codepoints_as_one",
            "planned_output_count": EXPECTED_OUTPUT_COUNT,
            "planned_input_unicode_codepoints": EXPECTED_INPUT_CODEPOINTS,
            "planned_billable_input_characters": EXPECTED_BILLABLE_CHARACTERS,
            "prior_stage_provider_usage_characters": prior_stage_usage_characters,
            "planned_whole_stage_billable_characters": stage_characters,
            "official_price_usd_per_10000_input_characters": str(PRICE_USD_PER_10K_CHARACTERS),
            "official_price_cny_per_10000_input_characters": str(PRICE_CNY_PER_10K_CHARACTERS),
            "estimated_run_cost_usd": str(estimated_usd),
            "estimated_run_cost_cny": str(estimated_cny),
            "estimated_whole_stage_cost_usd": str(estimated_stage_usd),
            "whole_run_synthesis_cost_ceiling_usd": str(COST_CEILING_USD),
            "output_audio_is_free": True,
            "free_allowance_assumed": False,
            "actual_charge_status": "unknown_until_provider_billing_reconciliation",
        },
        "authorization_contract": {
            "synthesis_and_local_blind_test_authorized": True,
            "authorization_recorded_in_current_codex_task": True,
            "paralinguistic_ordinals_2_and_3_authorized": False,
            "provider_voice_deletion_authorized": False,
            "training_or_fine_tuning_authorized": False,
            "publication_authorized": False,
            "rollout_authorized": False,
        },
        "operator_only_candidate_mapping": mappings,
        "planned_outputs": outputs,
        "rating_rubric": plan.get("rating_rubric"),
        "decision_rule": plan.get("decision_rule"),
        "paralinguistic_event_lane": plan.get("paralinguistic_event_lane"),
        "official_sources": OFFICIAL_SOURCES,
    }
    manifest["manifest_sha256"] = base._semantic_sha256(manifest)
    destination = root / OUTPUT_DIRECTORY / run_id
    return manifest, destination


def _private_readme(run_id: str) -> str:
    return f"""# Project Snow Provider blind test `{run_id}`

This directory is private operator material. `manifest.json` contains the
candidate-to-opaque-label map. Never copy it into `review/`.

Each `render-next` attempt is committed under `audits/` before a WebSocket is
opened. If any attempt lacks a matching result, the whole run is blocked and
must not be retried automatically. The review package is generated only after
all 24 outputs validate. No Provider voice is deleted by this workflow.
"""


def write_run(root_path: Path, manifest: dict[str, Any], destination: Path) -> Path:
    root = base._absolute_lexical(root_path)
    parent = _create_safe_directory(root, root / OUTPUT_DIRECTORY, label="blind-test root")
    _expect(destination.parent, parent, label="blind-test destination parent")
    try:
        destination.mkdir()
    except FileExistsError as error:
        raise VoiceProviderBlindTestError("blind-test run directory already exists") from error
    base._require_safe_existing_path(root, destination, label="blind-test run", directory=True)
    _create_safe_directory(root, destination / "audits", label="synthesis audit directory")
    review = _create_safe_directory(root, destination / "review", label="review directory")
    audio = _create_safe_directory(root, review / "audio", label="review audio directory")
    for slug in CHARACTER_ORDER:
        _create_safe_directory(root, audio / slug, label=f"{slug} review audio directory")
    _write_new_file(destination / "manifest.json", _pretty_json_bytes(manifest), label="run manifest")
    _write_new_file(
        destination / "README.md",
        _private_readme(manifest["run_id"]).encode("utf-8"),
        label="operator README",
    )
    return destination


def _validate_manifest_shape(manifest: dict[str, Any]) -> str:
    _expect(manifest.get("schema_version"), SCHEMA, label="blind-test run schema")
    run_id = base._safe_identifier(manifest.get("run_id"), RUN_ID_PATTERN, label="blind-test run ID")
    base._verify_semantic_hash(manifest, field="manifest_sha256", label="blind-test run manifest")
    _validate_iso_timestamp(manifest.get("prepared_at"), label="prepared_at")
    _expect(
        manifest.get("status"),
        "prepared_for_authorized_synthesis",
        label="blind-test run status",
    )
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    _expect(provider.get("region"), REGION, label="provider region")
    _expect(provider.get("target_model"), MODEL, label="provider model")
    _expect(provider.get("websocket_endpoint"), WEBSOCKET_ENDPOINT, label="WebSocket endpoint")
    parameters = _object(provider.get("synthesis_parameters"), label="synthesis parameters")
    expected_parameters = {
        "mode": MODE,
        "language_type": LANGUAGE_TYPE,
        "response_format": RESPONSE_FORMAT,
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        "post_processing": "none",
        "loudness_policy": "same_non_destructive_policy_no_normalization",
    }
    _expect(parameters, expected_parameters, label="synthesis parameters")
    pricing = _object(manifest.get("pricing_contract"), label="pricing contract")
    _expect(pricing.get("planned_output_count"), EXPECTED_OUTPUT_COUNT, label="planned output count")
    billing_rule = pricing.get("billing_counting_rule")
    legacy_billing_contract = billing_rule is None
    expected_billable = EXPECTED_INPUT_CODEPOINTS if legacy_billing_contract else EXPECTED_BILLABLE_CHARACTERS
    if not legacy_billing_contract:
        _expect(
            billing_rule,
            "cjk_ideographs_count_as_two_characters_other_codepoints_as_one",
            label="billing counting rule",
        )
        _expect(
            pricing.get("planned_input_unicode_codepoints"),
            EXPECTED_INPUT_CODEPOINTS,
            label="planned input Unicode codepoints",
        )
        prior_usage = pricing.get("prior_stage_provider_usage_characters")
        if not isinstance(prior_usage, int) or isinstance(prior_usage, bool) or prior_usage < 0:
            raise VoiceProviderBlindTestError("prior stage Provider usage must be a non-negative integer")
        _expect(
            pricing.get("planned_whole_stage_billable_characters"),
            prior_usage + EXPECTED_BILLABLE_CHARACTERS,
            label="planned whole-stage billable characters",
        )
        if _estimated_cost(
            prior_usage + EXPECTED_BILLABLE_CHARACTERS,
            PRICE_USD_PER_10K_CHARACTERS,
        ) > COST_CEILING_USD:
            raise VoiceProviderBlindTestError("planned whole-stage cost exceeds the ceiling")
    _expect(
        pricing.get("planned_billable_input_characters"),
        expected_billable,
        label="planned billable characters",
    )
    _expect(
        pricing.get("whole_run_synthesis_cost_ceiling_usd"),
        str(COST_CEILING_USD),
        label="synthesis cost ceiling",
    )
    authorization = _object(manifest.get("authorization_contract"), label="authorization contract")
    _expect(
        authorization.get("synthesis_and_local_blind_test_authorized"),
        True,
        label="synthesis authorization",
    )
    for key in (
        "paralinguistic_ordinals_2_and_3_authorized",
        "provider_voice_deletion_authorized",
        "training_or_fine_tuning_authorized",
        "publication_authorized",
        "rollout_authorized",
    ):
        _expect(authorization.get(key), False, label=f"authorization.{key}")
    mappings = _array(
        manifest.get("operator_only_candidate_mapping"),
        label="operator candidate mapping",
    )
    _expect(
        [item.get("character_slug") if isinstance(item, dict) else None for item in mappings],
        list(CHARACTER_ORDER),
        label="candidate mapping character order",
    )
    mapped: dict[str, tuple[str, str, str]] = {}
    label_ids: set[str] = set()
    for raw in mappings:
        mapping = _object(raw, label="character mapping")
        slug = _string(mapping.get("character_slug"), label="mapping character")
        labels = _array(mapping.get("labels"), label="mapping labels")
        _expect(len(labels), 2, label=f"{slug} label count")
        for raw_label in labels:
            label = _object(raw_label, label="opaque label")
            key = _string(label.get("candidate_key"), label="mapped candidate key")
            label_id = base._safe_identifier(
                label.get("opaque_label_id"), OPAQUE_ID_PATTERN, label="opaque label ID"
            )
            display = _string(label.get("display_label"), label="opaque display label")
            if key in mapped or label_id in label_ids:
                raise VoiceProviderBlindTestError("candidate mapping is not one-to-one")
            mapped[key] = (slug, label_id, display)
            label_ids.add(label_id)
    _expect(sorted(mapped, key=CANDIDATE_ORDER.index), list(CANDIDATE_ORDER), label="mapped candidates")

    outputs = _array(manifest.get("planned_outputs"), label="planned outputs")
    _expect(len(outputs), EXPECTED_OUTPUT_COUNT, label="planned output count")
    output_ids: set[str] = set()
    paths: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    total_characters = 0
    total_billable = 0
    for raw in outputs:
        output = _object(raw, label="planned output")
        output_id = base._safe_identifier(
            output.get("output_id"), OUTPUT_ID_PATTERN, label="output ID"
        )
        if output_id in output_ids:
            raise VoiceProviderBlindTestError("duplicate output ID")
        output_ids.add(output_id)
        key = _string(output.get("candidate_key"), label="output candidate key")
        if key not in mapped:
            raise VoiceProviderBlindTestError("output references an unmapped candidate")
        slug, label_id, display = mapped[key]
        _expect(output.get("character_slug"), slug, label=f"{output_id} character")
        _expect(output.get("opaque_label_id"), label_id, label=f"{output_id} opaque label")
        _expect(output.get("display_label"), display, label=f"{output_id} display label")
        case_id = _string(output.get("case_id"), label="output case ID")
        pair = (case_id, key)
        if pair in seen_pairs:
            raise VoiceProviderBlindTestError("duplicate case/candidate output")
        seen_pairs.add(pair)
        text = _string(output.get("text"), label="output text")
        _expect(
            output.get("text_sha256"),
            base._sha256_bytes(text.encode("utf-8")),
            label=f"{output_id} text SHA-256",
        )
        _expect(output.get("input_character_count"), len(text), label=f"{output_id} characters")
        total_characters += len(text)
        calculated_billable = _billing_character_count(text)
        if legacy_billing_contract:
            _expect(
                output.get("billing_character_count"),
                None,
                label=f"{output_id} legacy billing characters",
            )
        else:
            _expect(
                output.get("billing_character_count"),
                calculated_billable,
                label=f"{output_id} billing characters",
            )
        total_billable += calculated_billable
        display_order = output.get("display_order")
        if display_order not in (1, 2):
            raise VoiceProviderBlindTestError("display order must be 1 or 2")
        relative = base._safe_relative_path(
            output.get("review_audio_relative_path"),
            label=f"{output_id} review audio path",
        )
        if relative.parts[:3] != ("review", "audio", slug) or relative.suffix != ".wav":
            raise VoiceProviderBlindTestError("review audio path violates the blind package layout")
        path_text = relative.as_posix()
        if path_text in paths:
            raise VoiceProviderBlindTestError("duplicate review audio path")
        paths.add(path_text)
    _expect(total_characters, EXPECTED_INPUT_CODEPOINTS, label="total input Unicode codepoints")
    if not legacy_billing_contract:
        _expect(total_billable, EXPECTED_BILLABLE_CHARACTERS, label="total billable characters")
    superseded = manifest.get("supersedes_blind_test_runs", [])
    for value in _array(superseded, label="superseded blind-test runs"):
        base._safe_identifier(value, RUN_ID_PATTERN, label="superseded blind-test run ID")
    return run_id


def _source_receipt_map(root: Path, manifest: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    source = _object(manifest.get("source_enrollment"), label="source enrollment")
    run_id = _string(source.get("run_id"), label="source enrollment run ID")
    _, rebuilt_receipts, voices = _load_enrollment_receipts(
        root,
        enrollment_run_id=run_id,
        expected_enrollment_manifest_byte_sha256=_string(
            source.get("manifest_byte_sha256"),
            label="source enrollment manifest byte SHA-256",
        ),
    )
    _expect(source.get("result_receipts"), rebuilt_receipts, label="source enrollment receipts")
    return voices, set(voices.values())


def load_run(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
    revalidate_sources: bool = True,
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    safe_id = base._safe_identifier(run_id, RUN_ID_PATTERN, label="blind-test run ID")
    directory = root / OUTPUT_DIRECTORY / safe_id
    base._require_safe_existing_path(root, directory, label="blind-test run directory", directory=True)
    manifest, payload = base._read_json(root, directory / "manifest.json", label="blind-test run manifest")
    _expect(_validate_manifest_shape(manifest), safe_id, label="blind-test directory ID")
    if expected_manifest_byte_sha256 is not None:
        _expect(
            base._sha256_bytes(payload),
            base._require_sha256(
                expected_manifest_byte_sha256,
                label="expected blind-test manifest byte SHA-256",
            ),
            label="blind-test manifest byte SHA-256",
        )
    source_preflight = _object(manifest.get("source_preflight"), label="source preflight")
    if revalidate_sources:
        _, rebuilt_preflight = _load_pinned_plan(
            root,
            preflight_id=_string(source_preflight.get("preflight_id"), label="source preflight ID"),
            expected_preflight_manifest_byte_sha256=_string(
                source_preflight.get("manifest_byte_sha256"),
                label="source preflight manifest byte SHA-256",
            ),
            expected_blind_plan_byte_sha256=_string(
                source_preflight.get("blind_plan_byte_sha256"),
                label="source blind plan byte SHA-256",
            ),
        )
        _expect(source_preflight, rebuilt_preflight, label="source preflight reconstruction")
        voices, _ = _source_receipt_map(root, manifest)
    else:
        voices = {}
    for relative, label in (
        ("audits", "synthesis audit directory"),
        ("review", "review directory"),
        ("review/audio", "review audio directory"),
    ):
        base._require_safe_existing_path(
            root,
            directory.joinpath(*relative.split("/")),
            label=label,
            directory=True,
        )
    return directory, manifest, voices


def _write_audit(directory: Path, record: dict[str, Any], filename: str) -> Path:
    if not re.fullmatch(r"synthesis-attempt-[0-9a-f]{32}-(?:attempt|result)\.json", filename):
        raise VoiceProviderBlindTestError("invalid synthesis audit filename")
    document = dict(record)
    document["record_sha256"] = base._semantic_sha256(document)
    return _write_new_file(directory / filename, _pretty_json_bytes(document), label="synthesis audit")


def _audit_state(root: Path, directory: Path, run_id: str) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        record, _ = base._read_json(root, path, label="synthesis audit record")
        _expect(record.get("schema_version"), AUDIT_SCHEMA, label="synthesis audit schema")
        base._verify_semantic_hash(record, field="record_sha256", label="synthesis audit record")
        _expect(record.get("run_id"), run_id, label="synthesis audit run ID")
        attempt_id = base._safe_identifier(
            record.get("attempt_id"), ATTEMPT_ID_PATTERN, label="synthesis attempt ID"
        )
        stage = record.get("stage")
        target = attempts if stage == "attempt_started" else results if stage == "result_committed" else None
        if target is None or attempt_id in target:
            raise VoiceProviderBlindTestError("synthesis audit history is ambiguous")
        target[attempt_id] = record
    if set(results) - set(attempts):
        raise VoiceProviderBlindTestError("synthesis result lacks its attempt record")
    pending = [record for key, record in attempts.items() if key not in results]
    successful = [
        record for record in results.values() if record.get("outcome") == "audio_rendered"
    ]
    by_output: dict[str, dict[str, Any]] = {}
    for result in successful:
        output_id = base._safe_identifier(
            result.get("output_id"), OUTPUT_ID_PATTERN, label="result output ID"
        )
        attempt = attempts[result["attempt_id"]]
        _expect(attempt.get("output_id"), output_id, label="attempt/result output ID")
        if output_id in by_output:
            raise VoiceProviderBlindTestError("an output has multiple successful synthesis results")
        by_output[output_id] = result
    return {
        "attempts": attempts,
        "results": results,
        "pending": pending,
        "successful": successful,
        "by_output": by_output,
    }


def validate_run(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
    revalidate_sources: bool = True,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, _ = load_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=revalidate_sources,
    )
    state = _audit_state(root, directory / "audits", run_id)
    planned = {item["output_id"] for item in manifest["planned_outputs"]}
    if not set(state["by_output"]).issubset(planned):
        raise VoiceProviderBlindTestError("synthesis audit references an unplanned output")
    for output_id, result in state["by_output"].items():
        output = next(item for item in manifest["planned_outputs"] if item["output_id"] == output_id)
        _validate_result_audio(root, directory, output, result)
    usage = sum(int(item.get("provider_usage_characters") or 0) for item in state["successful"])
    pricing = _object(manifest.get("pricing_contract"), label="pricing contract")
    prior_usage = int(pricing.get("prior_stage_provider_usage_characters") or 0)
    stage_usage = prior_usage + usage
    return {
        "status": "valid",
        "run_id": run_id,
        "path": str(directory),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_byte_sha256": base._sha256_bytes(
            base._read_stable_bytes(root, directory / "manifest.json", label="blind-test manifest")
        ),
        "planned_output_count": EXPECTED_OUTPUT_COUNT,
        "successful_output_count": len(state["successful"]),
        "pending_attempt_count": len(state["pending"]),
        "provider_usage_characters": usage,
        "estimated_usage_cost_usd": str(_estimated_cost(usage, PRICE_USD_PER_10K_CHARACTERS)),
        "prior_stage_provider_usage_characters": prior_usage,
        "whole_stage_provider_usage_characters": stage_usage,
        "estimated_whole_stage_cost_usd": str(
            _estimated_cost(stage_usage, PRICE_USD_PER_10K_CHARACTERS)
        ),
        "review_ready": len(state["successful"]) == EXPECTED_OUTPUT_COUNT and not state["pending"],
    }


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"event_id": f"event_{uuid.uuid4().hex}", "type": event_type, **fields}


def _send_event(connection: Any, event_type: str, **fields: Any) -> None:
    connection.send(json.dumps(_event(event_type, **fields), ensure_ascii=False, separators=(",", ":")))


def _receive_event(connection: Any, *, deadline: float) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VoiceProviderBlindTestError("provider exchange exceeded the whole-call timeout")
    raw = connection.recv(timeout=min(EVENT_TIMEOUT_SECONDS, remaining))
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
        raise VoiceProviderBlindTestError("provider returned an invalid or oversized event")
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as error:
        raise VoiceProviderBlindTestError("provider returned a non-JSON event") from error
    if not isinstance(event, dict):
        raise VoiceProviderBlindTestError("provider event must be a JSON object")
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise VoiceProviderBlindTestError("provider event lacks a type")
    if event_type == "error":
        details = event.get("error") if isinstance(event.get("error"), dict) else {}
        code = str(details.get("code") or "unknown")
        safe_code = re.sub(r"[^A-Za-z0-9_.-]", "_", code)[:80]
        raise VoiceProviderBlindTestError(f"provider returned an error event ({safe_code})")
    return event


def _receive_until(connection: Any, expected: str, *, deadline: float) -> dict[str, Any]:
    for _ in range(MAX_EVENT_COUNT):
        event = _receive_event(connection, deadline=deadline)
        if event["type"] == expected:
            return event
    raise VoiceProviderBlindTestError(f"provider did not return {expected!r} within the event limit")


def _bounded_identifier(value: Any, *, label: str) -> str:
    text = _string(value, label=label)
    if len(text) > 256 or any(ord(character) < 32 for character in text):
        raise VoiceProviderBlindTestError(f"{label} is invalid")
    return text


def provider_synthesize_pcm(
    *,
    api_key: str,
    workspace_id: str,
    voice_id: str,
    text: str,
    websocket_factory: Callable[..., Any] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    normalized_workspace = workspace_id.strip().lower()
    if not enrollment.WORKSPACE_PATTERN.fullmatch(normalized_workspace):
        raise VoiceProviderBlindTestError("invalid Beijing workspace ID")
    if not api_key or any(character.isspace() for character in api_key):
        raise VoiceProviderBlindTestError("invalid Beijing API key")
    if not enrollment.VOICE_PATTERN.fullmatch(voice_id):
        raise VoiceProviderBlindTestError("invalid Provider voice ID")
    if not text or "\x00" in text:
        raise VoiceProviderBlindTestError("synthesis text must be non-empty plain text")
    if websocket_factory is None:
        from websockets.sync.client import connect

        websocket_factory = connect
    endpoint = f"{WEBSOCKET_ENDPOINT}?{urlencode({'model': MODEL})}"
    deadline = time.monotonic() + WHOLE_EXCHANGE_TIMEOUT_SECONDS
    pcm = bytearray()
    event_count = 0
    audio_done = False
    response_done = False
    response_id = ""
    usage_characters: int | None = None
    session_id = ""
    with websocket_factory(
        endpoint,
        additional_headers={
            "Authorization": f"Bearer {api_key}",
            "X-DashScope-WorkSpace": normalized_workspace,
            "User-Agent": "Project-Snow-voice-blind-test/1.0",
        },
        compression=None,
        open_timeout=15,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=MAX_EVENT_BYTES,
    ) as connection:
        created = _receive_until(connection, "session.created", deadline=deadline)
        created_session = _object(created.get("session"), label="created session")
        session_id = _bounded_identifier(created_session.get("id"), label="provider session ID")
        _expect(created_session.get("model"), MODEL, label="created session model")
        _send_event(
            connection,
            "session.update",
            session={
                "voice": voice_id,
                "mode": MODE,
                "language_type": LANGUAGE_TYPE,
                "response_format": RESPONSE_FORMAT,
                "sample_rate": SAMPLE_RATE_HZ,
            },
        )
        updated = _receive_until(connection, "session.updated", deadline=deadline)
        updated_session = _object(updated.get("session"), label="updated session")
        _expect(updated_session.get("id"), session_id, label="updated session ID")
        _expect(updated_session.get("model"), MODEL, label="updated session model")
        _expect(updated_session.get("voice"), voice_id, label="updated session voice")
        _expect(updated_session.get("mode"), MODE, label="updated session mode")
        updated_language = _string(
            updated_session.get("language_type"), label="updated language"
        )
        _expect(
            updated_language.casefold(),
            LANGUAGE_TYPE.casefold(),
            label="updated language",
        )
        _expect(updated_session.get("response_format"), RESPONSE_FORMAT, label="updated audio format")
        _expect(updated_session.get("sample_rate"), SAMPLE_RATE_HZ, label="updated sample rate")
        _send_event(connection, "input_text_buffer.append", text=text)
        _send_event(connection, "input_text_buffer.commit")
        while not (audio_done and response_done):
            event_count += 1
            if event_count > MAX_EVENT_COUNT:
                raise VoiceProviderBlindTestError("provider synthesis exceeded the event limit")
            event = _receive_event(connection, deadline=deadline)
            event_type = event["type"]
            if event_type == "response.audio.delta":
                delta = event.get("delta")
                if not isinstance(delta, str) or len(delta) > (MAX_AUDIO_BYTES * 2):
                    raise VoiceProviderBlindTestError("provider audio delta is invalid or oversized")
                try:
                    chunk = base64.b64decode(delta, validate=True)
                except (binascii.Error, ValueError) as error:
                    raise VoiceProviderBlindTestError("provider audio delta is not valid base64") from error
                if len(pcm) + len(chunk) > MAX_AUDIO_BYTES:
                    raise VoiceProviderBlindTestError("provider audio exceeded the safety limit")
                pcm.extend(chunk)
                if event.get("response_id"):
                    response_id = _bounded_identifier(
                        event.get("response_id"), label="provider response ID"
                    )
            elif event_type == "response.audio.done":
                audio_done = True
                if event.get("response_id"):
                    response_id = _bounded_identifier(
                        event.get("response_id"), label="provider response ID"
                    )
            elif event_type == "response.done":
                response = _object(event.get("response"), label="completed response")
                if response.get("status") not in ("completed", "done"):
                    raise VoiceProviderBlindTestError("provider response did not complete successfully")
                event_response_id = event.get("response_id") or response.get("id")
                response_id = _bounded_identifier(event_response_id, label="provider response ID")
                if response.get("voice") is not None:
                    _expect(response.get("voice"), voice_id, label="completed response voice")
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                raw_usage = usage.get("characters")
                if isinstance(raw_usage, int) and not isinstance(raw_usage, bool) and raw_usage >= 0:
                    usage_characters = raw_usage
                response_done = True
        if not pcm:
            raise VoiceProviderBlindTestError("provider returned no audio")
        _send_event(connection, "session.finish")
        _receive_until(connection, "session.finished", deadline=deadline)
    return bytes(pcm), {
        "session_id": session_id,
        "response_id": response_id,
        "provider_usage_characters": usage_characters,
        "received_event_count": event_count,
    }


def _pcm_metrics(pcm: bytes) -> dict[str, Any]:
    if not pcm or len(pcm) % (CHANNELS * SAMPLE_WIDTH_BYTES):
        raise VoiceProviderBlindTestError("Provider PCM is empty or frame-misaligned")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    frame_count = len(samples) // CHANNELS
    duration = frame_count / SAMPLE_RATE_HZ
    if not 0.2 <= duration <= 120:
        raise VoiceProviderBlindTestError("Provider PCM duration is outside the 0.2-120 second limit")
    peak = max(abs(value) for value in samples)
    sum_squares = sum(value * value for value in samples)
    rms = math.sqrt(sum_squares / len(samples))
    if rms < 1:
        raise VoiceProviderBlindTestError("Provider PCM is effectively silent")
    clipped = sum(abs(value) >= 32_767 for value in samples)
    peak_dbfs = None if peak == 0 else round(20 * math.log10(peak / 32_768), 6)
    rms_dbfs = None if rms == 0 else round(20 * math.log10(rms / 32_768), 6)
    return {
        "encoding": "pcm_s16le",
        "sample_rate_hz": SAMPLE_RATE_HZ,
        "channels": CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH_BYTES,
        "frame_count": frame_count,
        "duration_seconds": round(duration, 6),
        "pcm_byte_count": len(pcm),
        "pcm_sha256": base._sha256_bytes(pcm),
        "peak_absolute_sample": peak,
        "peak_dbfs": peak_dbfs,
        "rms_dbfs": rms_dbfs,
        "full_scale_sample_count": clipped,
        "full_scale_sample_fraction": round(clipped / len(samples), 9),
    }


def _pcm_to_wav(pcm: bytes) -> tuple[bytes, dict[str, Any]]:
    metrics = _pcm_metrics(pcm)
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE_HZ)
        writer.writeframes(pcm)
    wav = output.getvalue()
    metrics["wav_byte_count"] = len(wav)
    metrics["wav_sha256"] = base._sha256_bytes(wav)
    return wav, metrics


def _validate_wav_bytes(payload: bytes, expected: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        with wave.open(io.BytesIO(payload), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frame_count = reader.getnframes()
            compression = reader.getcomptype()
            pcm = reader.readframes(frame_count)
            trailing = reader.readframes(1)
    except (EOFError, wave.Error) as error:
        raise VoiceProviderBlindTestError("rendered output is not a valid WAV") from error
    _expect(channels, CHANNELS, label="rendered WAV channels")
    _expect(sample_width, SAMPLE_WIDTH_BYTES, label="rendered WAV sample width")
    _expect(sample_rate, SAMPLE_RATE_HZ, label="rendered WAV sample rate")
    _expect(compression, "NONE", label="rendered WAV compression")
    _expect(trailing, b"", label="rendered WAV trailing frames")
    actual = _pcm_metrics(pcm)
    _expect(actual["frame_count"], frame_count, label="rendered WAV frame count")
    actual["wav_byte_count"] = len(payload)
    actual["wav_sha256"] = base._sha256_bytes(payload)
    if expected is not None:
        for key in (
            "sample_rate_hz",
            "channels",
            "sample_width_bytes",
            "frame_count",
            "pcm_byte_count",
            "pcm_sha256",
            "wav_byte_count",
            "wav_sha256",
        ):
            _expect(actual.get(key), expected.get(key), label=f"rendered WAV {key}")
    return actual


def _validate_result_audio(
    root: Path,
    run_directory: Path,
    output: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    _expect(result.get("output_id"), output.get("output_id"), label="result output ID")
    _expect(result.get("case_id"), output.get("case_id"), label="result case ID")
    _expect(
        result.get("opaque_label_id"),
        output.get("opaque_label_id"),
        label="result opaque label",
    )
    relative = base._safe_relative_path(
        result.get("review_audio_relative_path"), label="result audio relative path"
    )
    _expect(
        relative.as_posix(),
        output.get("review_audio_relative_path"),
        label="result/planned audio path",
    )
    audio_path = run_directory.joinpath(*relative.parts)
    payload = base._read_stable_bytes(root, audio_path, label="rendered review audio")
    metrics = _object(result.get("audio_metrics"), label="result audio metrics")
    _validate_wav_bytes(payload, metrics)
    return metrics


def _confirmation_decimal(value: str) -> Decimal:
    try:
        confirmed = Decimal(value)
    except InvalidOperation as error:
        raise VoiceProviderBlindTestError("confirmed synthesis cost ceiling must be decimal USD") from error
    _expect(confirmed, COST_CEILING_USD, label="confirmed synthesis cost ceiling")
    return confirmed


def _planned_output(manifest: dict[str, Any], output_id: str) -> dict[str, Any]:
    safe_id = base._safe_identifier(output_id, OUTPUT_ID_PATTERN, label="output ID")
    matches = [item for item in manifest["planned_outputs"] if item.get("output_id") == safe_id]
    if len(matches) != 1:
        raise VoiceProviderBlindTestError("output ID is not uniquely planned")
    return matches[0]


def _next_output(manifest: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    completed = set(state["by_output"])
    return next((item for item in manifest["planned_outputs"] if item["output_id"] not in completed), None)


def _validate_render_confirmations(
    *,
    run_id: str,
    output_id: str,
    confirm_run_id: str,
    confirm_output_id: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_synthesis_and_local_blind_test_authorized: bool,
    confirm_paralinguistic_ordinals_excluded: bool,
) -> None:
    _expect(confirm_run_id, run_id, label="confirmed run ID")
    _expect(confirm_output_id, output_id, label="confirmed output ID")
    _expect(confirm_model, MODEL, label="confirmed model")
    _expect(confirm_region, REGION, label="confirmed region")
    _confirmation_decimal(confirm_cost_ceiling_usd)
    if not confirm_synthesis_and_local_blind_test_authorized:
        raise VoiceProviderBlindTestError("live synthesis requires explicit local blind-test authorization")
    if not confirm_paralinguistic_ordinals_excluded:
        raise VoiceProviderBlindTestError("live synthesis requires confirmation that ordinals 2/3 stay excluded")


def render_one(
    voice_root: Path,
    run_id: str,
    output_id: str,
    *,
    workspace_id: str,
    api_key_file: Path | None,
    dotenv_file: Path | None,
    expected_manifest_byte_sha256: str | None,
    confirm_run_id: str,
    confirm_output_id: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_synthesis_and_local_blind_test_authorized: bool,
    confirm_paralinguistic_ordinals_excluded: bool,
    websocket_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, voices = load_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    output = _planned_output(manifest, output_id)
    pricing = _object(manifest.get("pricing_contract"), label="pricing contract")
    if pricing.get("billing_counting_rule") is None:
        raise VoiceProviderBlindTestError(
            "legacy run undercounts CJK billing characters and must not submit more synthesis"
        )
    _validate_render_confirmations(
        run_id=run_id,
        output_id=output_id,
        confirm_run_id=confirm_run_id,
        confirm_output_id=confirm_output_id,
        confirm_model=confirm_model,
        confirm_region=confirm_region,
        confirm_cost_ceiling_usd=confirm_cost_ceiling_usd,
        confirm_synthesis_and_local_blind_test_authorized=(
            confirm_synthesis_and_local_blind_test_authorized
        ),
        confirm_paralinguistic_ordinals_excluded=confirm_paralinguistic_ordinals_excluded,
    )
    audit_directory = directory / "audits"
    state = _audit_state(root, audit_directory, run_id)
    if state["pending"]:
        raise VoiceProviderBlindTestError(
            "the run has an uncertain synthesis attempt; do not retry or continue automatically"
        )
    if output_id in state["by_output"]:
        raise VoiceProviderBlindTestError("output already has a successful synthesis result")
    if len(state["successful"]) >= EXPECTED_OUTPUT_COUNT:
        raise VoiceProviderBlindTestError("the planned output ceiling is already exhausted")
    prior_stage_usage = int(pricing["prior_stage_provider_usage_characters"])
    completed_run_usage = sum(
        int(item.get("provider_usage_characters") or 0) for item in state["successful"]
    )
    planned_stage_after = prior_stage_usage + completed_run_usage + output["billing_character_count"]
    if _estimated_cost(planned_stage_after, PRICE_USD_PER_10K_CHARACTERS) > COST_CEILING_USD:
        raise VoiceProviderBlindTestError("the synthesis cost ceiling would be exceeded")

    voice_id = voices[output["candidate_key"]]
    api_key = enrollment._read_secret(
        api_key_file,
        voice_root=root,
        region=REGION,
        dotenv_file=dotenv_file,
    )
    normalized_workspace = workspace_id.strip().lower()
    if not enrollment.WORKSPACE_PATTERN.fullmatch(normalized_workspace):
        raise VoiceProviderBlindTestError("invalid Beijing workspace ID")
    attempt_id = f"synthesis-attempt-{uuid.uuid4().hex}"
    attempt = {
        "schema_version": AUDIT_SCHEMA,
        "attempt_id": attempt_id,
        "stage": "attempt_started",
        "action": "synthesize",
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
        "run_id": run_id,
        "output_id": output_id,
        "case_id": output["case_id"],
        "character_slug": output["character_slug"],
        "candidate_key": output["candidate_key"],
        "opaque_label_id": output["opaque_label_id"],
        "text_sha256": output["text_sha256"],
        "input_character_count": output["input_character_count"],
        "billing_character_count": output["billing_character_count"],
        "provider_voice_id_sha256": base._sha256_bytes(voice_id.encode("utf-8")),
        "workspace_id_sha256": base._sha256_bytes(normalized_workspace.encode("utf-8")),
        "endpoint": WEBSOCKET_ENDPOINT,
        "target_model": MODEL,
        "synthesis_parameters": manifest["provider_contract"]["synthesis_parameters"],
        "whole_run_synthesis_cost_ceiling_usd": str(COST_CEILING_USD),
        "review_audio_relative_path": output["review_audio_relative_path"],
        "request_contains_credentials": False,
        "paralinguistic_ordinals_2_and_3_included": False,
    }
    attempt_path = _write_audit(
        audit_directory,
        attempt,
        f"{attempt_id}-attempt.json",
    )
    try:
        pcm, provider_metadata = provider_synthesize_pcm(
            api_key=api_key,
            workspace_id=normalized_workspace,
            voice_id=voice_id,
            text=output["text"],
            websocket_factory=websocket_factory,
        )
        wav, metrics = _pcm_to_wav(pcm)
        relative = base._safe_relative_path(
            output["review_audio_relative_path"], label="review audio relative path"
        )
        audio_path = directory.joinpath(*relative.parts)
        _write_atomic_new(audio_path, wav, label="rendered review audio")
        verified_metrics = _validate_wav_bytes(
            base._read_stable_bytes(root, audio_path, label="committed review audio"),
            metrics,
        )
    except Exception as error:
        if isinstance(error, VoiceProviderBlindTestError):
            raise
        raise VoiceProviderBlindTestError(
            "provider synthesis failed after the attempt was committed; do not retry automatically"
        ) from error

    usage = provider_metadata.get("provider_usage_characters")
    if usage is None:
        usage = output["billing_character_count"]
        usage_basis = "official_billing_rule_fallback_provider_usage_missing"
    else:
        usage_basis = "provider_response_done_usage_characters"
    completed_usage = prior_stage_usage + sum(
        int(item.get("provider_usage_characters") or 0) for item in state["successful"]
    ) + int(usage)
    maximum_characters_under_ceiling = int(
        (COST_CEILING_USD * Decimal(10_000) / PRICE_USD_PER_10K_CHARACTERS).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if completed_usage > maximum_characters_under_ceiling:
        raise VoiceProviderBlindTestError(
            "provider usage exceeded the fixed cost ceiling after audio commit; stop the run"
        )
    result = {
        "schema_version": AUDIT_SCHEMA,
        "attempt_id": attempt_id,
        "stage": "result_committed",
        "action": "synthesize",
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(),
        "run_id": run_id,
        "output_id": output_id,
        "case_id": output["case_id"],
        "character_slug": output["character_slug"],
        "opaque_label_id": output["opaque_label_id"],
        "outcome": "audio_rendered",
        "target_model": MODEL,
        "provider_session_id": provider_metadata["session_id"],
        "provider_response_id": provider_metadata["response_id"],
        "provider_usage_characters": int(usage),
        "usage_basis": usage_basis,
        "estimated_provider_cost_usd": str(
            _estimated_cost(int(usage), PRICE_USD_PER_10K_CHARACTERS)
        ),
        "actual_provider_charge_usd": None,
        "charge_status": "unknown_until_provider_billing_reconciliation",
        "review_audio_relative_path": output["review_audio_relative_path"],
        "audio_metrics": verified_metrics,
        "attempt_record_relative_path": attempt_path.name,
    }
    result_path = _write_audit(audit_directory, result, f"{attempt_id}-result.json")
    return {
        "status": "audio_rendered",
        "run_id": run_id,
        "output_id": output_id,
        "case_id": output["case_id"],
        "character_slug": output["character_slug"],
        "opaque_label": output["display_label"],
        "duration_seconds": verified_metrics["duration_seconds"],
        "provider_usage_characters": int(usage),
        "successful_output_count": len(state["successful"]) + 1,
        "planned_output_count": EXPECTED_OUTPUT_COUNT,
        "audio_path": str(audio_path),
        "result_audit_path": str(result_path),
    }


def inspect_next(voice_root: Path, run_id: str) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, _ = load_run(root, run_id, revalidate_sources=True)
    state = _audit_state(root, directory / "audits", run_id)
    if state["pending"]:
        return {
            "status": "blocked_uncertain_attempt",
            "run_id": run_id,
            "pending_attempt_count": len(state["pending"]),
            "automatic_retry_permitted": False,
        }
    output = _next_output(manifest, state)
    if output is None:
        return {
            "status": "all_outputs_rendered",
            "run_id": run_id,
            "successful_output_count": len(state["successful"]),
        }
    return {
        "status": "next_output_ready",
        "run_id": run_id,
        "output_id": output["output_id"],
        "case_id": output["case_id"],
        "character_slug": output["character_slug"],
        "opaque_label": output["display_label"],
        "input_character_count": output["input_character_count"],
        "billing_character_count": output.get("billing_character_count"),
        "successful_output_count": len(state["successful"]),
        "remaining_output_count": EXPECTED_OUTPUT_COUNT - len(state["successful"]),
        "credentials_read": False,
        "network_call_performed": False,
    }


def _public_manifest(
    private_manifest: dict[str, Any],
    state: dict[str, Any],
    run_directory: Path,
    root: Path,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    outputs = private_manifest["planned_outputs"]
    for slug in CHARACTER_ORDER:
        character_outputs = [item for item in outputs if item["character_slug"] == slug]
        case_ids = list(dict.fromkeys(item["case_id"] for item in character_outputs))
        for case_id in case_ids:
            pair = [item for item in character_outputs if item["case_id"] == case_id]
            _expect(len(pair), 2, label=f"{case_id} synthesized pair")
            pair.sort(key=lambda item: item["display_order"])
            samples = []
            for output in pair:
                result = state["by_output"][output["output_id"]]
                metrics = _validate_result_audio(root, run_directory, output, result)
                source_relative = base._safe_relative_path(
                    output["review_audio_relative_path"], label="public audio path"
                )
                review_relative = Path(*source_relative.parts[1:]).as_posix()
                samples.append(
                    {
                        "opaque_label_id": output["opaque_label_id"],
                        "display_label": output["display_label"],
                        "audio_relative_path": review_relative,
                        "duration_seconds": metrics["duration_seconds"],
                        "wav_sha256": metrics["wav_sha256"],
                        "full_scale_sample_count": metrics["full_scale_sample_count"],
                    }
                )
            first = pair[0]
            cases.append(
                {
                    "character_slug": slug,
                    "runtime_character_name": first["runtime_character_name"],
                    "case_id": case_id,
                    "case_index": first["case_index"],
                    "category": first["category"],
                    "text": first["text"],
                    "samples": samples,
                }
            )
    usage = sum(int(item.get("provider_usage_characters") or 0) for item in state["successful"])
    pricing = _object(private_manifest.get("pricing_contract"), label="pricing contract")
    prior_usage = int(pricing.get("prior_stage_provider_usage_characters") or 0)
    stage_usage = prior_usage + usage
    document: dict[str, Any] = {
        "schema_version": PUBLIC_SCHEMA,
        "rating_submission_schema_version": RATING_SCHEMA,
        "blind_test_run_id": private_manifest["run_id"],
        "status": "ready_for_local_human_blind_review",
        "privacy_contract": {
            "candidate_mapping_included": False,
            "provider_voice_ids_included": False,
            "candidate_a_b_labels_included": False,
            "local_review_only": True,
            "publication_authorized": False,
        },
        "synthesis_contract": {
            "target_model": MODEL,
            "region": REGION,
            "same_parameters_within_every_pair": True,
            "parameters": private_manifest["provider_contract"]["synthesis_parameters"],
            "post_processing": "none",
        },
        "cost_summary": {
            "rendered_output_count": len(state["successful"]),
            "current_run_provider_reported_or_fallback_input_characters": usage,
            "prior_stage_provider_usage_characters": prior_usage,
            "whole_stage_provider_usage_characters": stage_usage,
            "estimated_current_run_cost_usd": str(
                _estimated_cost(usage, PRICE_USD_PER_10K_CHARACTERS)
            ),
            "estimated_whole_stage_cost_usd": str(
                _estimated_cost(stage_usage, PRICE_USD_PER_10K_CHARACTERS)
            ),
            "whole_stage_cost_ceiling_usd": str(COST_CEILING_USD),
            "actual_charge_status": "unknown_until_provider_billing_reconciliation",
        },
        "review_instructions": {
            "listen_with_matched_playback_volume": True,
            "do_not_infer_label_mapping": True,
            "score_each_dimension_as_integer_0_to_5": True,
            "mark_every_audible_critical_failure": True,
            "pair_preference_values": ["first_sample", "second_sample", "tie_or_unsure"],
        },
        "rating_rubric": private_manifest["rating_rubric"],
        "decision_rule": private_manifest["decision_rule"],
        "paralinguistic_event_lane": {
            "ordinals": [2, 3],
            "included_in_this_review": False,
            "base_tts_training": "excluded",
            "event_bank_eligibility": "pending_human_event_qa",
        },
        "cases": cases,
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    return document


def _review_html(public: dict[str, Any]) -> str:
    embedded = json.dumps(public, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    dimension_labels = {
        "speaker_identity_similarity": "说话人身份相似度",
        "intelligibility": "可懂度",
        "naturalness": "自然度",
        "character_fit": "角色贴合度",
        "prosody_and_breath_stability": "韵律与气息稳定",
        "artifact_absence": "无伪影/接缝",
    }
    failure_labels = {
        "wrong_or_unstable_voice_identity": "身份错误或漂移",
        "truncation_or_missing_content": "截断或漏字",
        "hallucinated_or_repeated_words": "幻觉或重复词",
        "clipping_discontinuity_or_audible_seam": "削波、断裂或可闻接缝",
    }
    labels_json = json.dumps(
        {"dimensions": dimension_labels, "failures": failure_labels},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    title = html.escape(f"Project Snow 音色盲测 · {public['blind_test_run_id']}")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--bg:#f3f6fb;--card:#fff;--line:#dce3ee;--ink:#172033;--muted:#5d687b;--accent:#3157d5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}}
main{{max-width:1180px;margin:28px auto;padding:0 18px 56px}}h1{{font-size:25px}}h2{{margin-top:0}}
.notice{{background:#fff5d9;border-left:4px solid #ce8b00;padding:13px 15px;border-radius:8px;margin:14px 0}}
.toolbar{{position:sticky;top:0;z-index:5;background:rgba(243,246,251,.94);padding:10px 0;display:flex;gap:10px;flex-wrap:wrap}}
button{{border:0;border-radius:8px;background:var(--accent);color:white;padding:9px 14px;cursor:pointer}}
button.secondary{{background:#65718a}}section.case{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:18px 0}}
.prompt{{font-size:17px;background:#f7f9fd;padding:12px;border-radius:8px}}.pair{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px}}
article.sample{{border:1px solid var(--line);border-radius:11px;padding:14px}}audio{{width:100%}}.meta{{color:var(--muted);font-size:13px}}
.scores{{display:grid;grid-template-columns:minmax(160px,1fr) 90px;gap:6px 10px;align-items:center;margin-top:12px}}
select{{width:100%;padding:6px;border:1px solid #bbc5d5;border-radius:6px;background:#fff}}fieldset{{border:0;padding:8px 0;margin:5px 0}}
.failures label{{display:block;margin:5px 0}}.preference{{margin-top:13px;border-top:1px solid var(--line);padding-top:12px}}
.status{{color:var(--muted)}}@media(max-width:520px){{.pair{{grid-template-columns:1fr}}.scores{{grid-template-columns:1fr 78px}}}}
</style></head><body><main>
<h1>{title}</h1>
<div class="notice">本页仅供本地盲听。页面不含 A/B 对应关系或 Provider 音色 ID；不要根据标签猜测来源。2、3 段特殊气声/呓语未纳入本次基础音色测试。</div>
<p>同一题的两条音频使用完全相同文本与合成参数，且未做响度归一化或其他后处理。请在相同播放音量下逐项评分。</p>
<div class="toolbar"><button id="save">保存到本机</button><button id="export">导出评分 JSON</button><button class="secondary" id="clear">清空评分</button><span class="status" id="status"></span></div>
<div id="cases"></div>
<script type="application/json" id="review-data">{embedded}</script>
<script type="application/json" id="label-data">{labels_json}</script>
<script>
const review=JSON.parse(document.getElementById('review-data').textContent);
const labels=JSON.parse(document.getElementById('label-data').textContent);
const key='project-snow-blind-ratings:'+review.blind_test_run_id;
const root=document.getElementById('cases');
const esc=s=>String(s).replace(/[&<>\"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
const scoreOptions='<option value="">未评分</option>'+[0,1,2,3,4,5].map(v=>`<option value="${{v}}">${{v}}</option>`).join('');
for(const c of review.cases){{
 const section=document.createElement('section');section.className='case';section.dataset.caseId=c.case_id;
 section.innerHTML=`<h2>${{esc(c.runtime_character_name)}} · 第 ${{c.case_index}} 题</h2><p class="meta">${{esc(c.category)}} · ${{esc(c.case_id)}}</p><p class="prompt">${{esc(c.text)}}</p><div class="pair"></div><div class="preference"><label>本题偏好 <select data-pref><option value="">未选择</option><option value="first_sample">第一个样本</option><option value="second_sample">第二个样本</option><option value="tie_or_unsure">平局 / 不确定</option></select></label></div>`;
 const pair=section.querySelector('.pair');
 c.samples.forEach((s,index)=>{{
  const article=document.createElement('article');article.className='sample';article.dataset.label=s.opaque_label_id;
  const scoreRows=review.rating_rubric.dimensions.map(d=>`<label>${{esc(labels.dimensions[d]||d)}}</label><select data-score="${{esc(d)}}">${{scoreOptions}}</select>`).join('');
  const failures=review.rating_rubric.critical_failures.map(f=>`<label><input type="checkbox" data-failure="${{esc(f)}}"> ${{esc(labels.failures[f]||f)}}</label>`).join('');
  article.innerHTML=`<h3>${{index+1}} · ${{esc(s.display_label)}}</h3><audio controls preload="metadata" src="${{esc(s.audio_relative_path)}}"></audio><p class="meta">${{s.duration_seconds.toFixed(3)}} 秒 · WAV ${{esc(s.wav_sha256.slice(0,12))}}…</p><div class="scores">${{scoreRows}}</div><fieldset class="failures"><legend>关键失败</legend>${{failures}}</fieldset>`;
  pair.appendChild(article);
 }});root.appendChild(section);
}}
function collect(){{const ratings={{}};document.querySelectorAll('section.case').forEach(section=>{{const item={{samples:{{}},pair_preference:section.querySelector('[data-pref]').value}};section.querySelectorAll('article.sample').forEach(article=>{{const scores={{}};article.querySelectorAll('[data-score]').forEach(x=>scores[x.dataset.score]=x.value===''?null:Number(x.value));const failures=[...article.querySelectorAll('[data-failure]:checked')].map(x=>x.dataset.failure);item.samples[article.dataset.label]={{scores,critical_failures:failures}}}});ratings[section.dataset.caseId]=item}});return {{schema_version:review.rating_submission_schema_version,blind_test_run_id:review.blind_test_run_id,source_review_manifest_sha256:review.manifest_sha256,saved_at:new Date().toISOString(),ratings}}}}
function apply(data){{if(!data||data.blind_test_run_id!==review.blind_test_run_id)return;for(const [caseId,item] of Object.entries(data.ratings||{{}})){{const section=document.querySelector(`section[data-case-id="${{CSS.escape(caseId)}}"]`);if(!section)continue;section.querySelector('[data-pref]').value=item.pair_preference||'';for(const [label,sample] of Object.entries(item.samples||{{}})){{const article=section.querySelector(`article[data-label="${{CSS.escape(label)}}"]`);if(!article)continue;article.querySelectorAll('[data-score]').forEach(x=>x.value=sample.scores?.[x.dataset.score]??'');article.querySelectorAll('[data-failure]').forEach(x=>x.checked=(sample.critical_failures||[]).includes(x.dataset.failure))}}}}}}
function setStatus(s){{document.getElementById('status').textContent=s}}
document.getElementById('save').onclick=()=>{{localStorage.setItem(key,JSON.stringify(collect()));setStatus('已保存到本机')}};
document.getElementById('export').onclick=()=>{{const data=collect();localStorage.setItem(key,JSON.stringify(data));const blob=new Blob([JSON.stringify(data,null,2)+'\\n'],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=review.blind_test_run_id+'-ratings.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);setStatus('评分 JSON 已导出')}};
document.getElementById('clear').onclick=()=>{{if(confirm('确认清空本页评分？')){{localStorage.removeItem(key);location.reload()}}}};
try{{apply(JSON.parse(localStorage.getItem(key)||'null'));setStatus(localStorage.getItem(key)?'已载入本机评分':'尚未保存评分')}}catch(e){{setStatus('本机评分无法读取')}}
</script></main></body></html>
"""


def _assert_public_privacy(payloads: list[bytes], private_manifest: dict[str, Any], voices: set[str]) -> None:
    combined = b"\n".join(payloads).decode("utf-8")
    exact_json_values = {*(f'"{key}"' for key in CANDIDATE_ORDER), *voices}
    forbidden_keys = {
        '"provider_voice_id"',
        '"provider_voice_id_sha256"',
        '"operator_only_candidate_mapping"',
        '"operator_only_candidate_map"',
        '"source_slot"',
    }
    for value in exact_json_values | forbidden_keys:
        if value and value in combined:
            raise VoiceProviderBlindTestError("public review package contains private operator data")
    mapped_labels = {
        label["opaque_label_id"]
        for mapping in private_manifest["operator_only_candidate_mapping"]
        for label in mapping["labels"]
    }
    if not mapped_labels.issubset(set(combined.split('"'))):
        raise VoiceProviderBlindTestError("public review package lost an opaque label")


def finalize_review(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, voices_by_candidate = load_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    state = _audit_state(root, directory / "audits", run_id)
    if state["pending"]:
        raise VoiceProviderBlindTestError("cannot finalize while a synthesis attempt is uncertain")
    _expect(len(state["successful"]), EXPECTED_OUTPUT_COUNT, label="successful rendered outputs")
    public = _public_manifest(manifest, state, directory, root)
    manifest_payload = _pretty_json_bytes(public)
    html_payload = _review_html(public).encode("utf-8")
    _assert_public_privacy([manifest_payload, html_payload], manifest, set(voices_by_candidate.values()))
    review_directory = directory / "review"
    manifest_path = review_directory / "manifest.json"
    html_path = review_directory / "review.html"
    if manifest_path.exists() or html_path.exists():
        if not (manifest_path.is_file() and html_path.is_file()):
            raise VoiceProviderBlindTestError("review finalization is partially present")
        _expect(
            base._read_stable_bytes(root, manifest_path, label="existing public manifest"),
            manifest_payload,
            label="existing public manifest",
        )
        _expect(
            base._read_stable_bytes(root, html_path, label="existing review HTML"),
            html_payload,
            label="existing review HTML",
        )
        write_status = "existing_identical"
    else:
        _write_atomic_new(manifest_path, manifest_payload, label="public review manifest")
        _write_atomic_new(html_path, html_payload, label="review HTML")
        write_status = "written"
    return {
        "status": "ready_for_local_human_blind_review",
        "write_status": write_status,
        "run_id": run_id,
        "review_html_path": str(html_path),
        "review_manifest_path": str(manifest_path),
        "review_manifest_sha256": public["manifest_sha256"],
        "rendered_output_count": EXPECTED_OUTPUT_COUNT,
        "provider_voice_ids_in_review_files": False,
        "candidate_mapping_in_review_files": False,
        "paralinguistic_ordinals_2_and_3_included": False,
    }


def render_all_remaining(
    voice_root: Path,
    run_id: str,
    *,
    workspace_id: str,
    api_key_file: Path | None,
    dotenv_file: Path | None,
    expected_manifest_byte_sha256: str | None,
    confirm_run_id: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_synthesis_and_local_blind_test_authorized: bool,
    confirm_paralinguistic_ordinals_excluded: bool,
    maximum_outputs: int = EXPECTED_OUTPUT_COUNT,
    websocket_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if not 1 <= maximum_outputs <= EXPECTED_OUTPUT_COUNT:
        raise VoiceProviderBlindTestError("maximum_outputs must be between 1 and 24")
    _expect(confirm_run_id, run_id, label="confirmed run ID")
    _expect(confirm_model, MODEL, label="confirmed model")
    _expect(confirm_region, REGION, label="confirmed region")
    _confirmation_decimal(confirm_cost_ceiling_usd)
    if not confirm_synthesis_and_local_blind_test_authorized:
        raise VoiceProviderBlindTestError("live synthesis requires explicit local blind-test authorization")
    if not confirm_paralinguistic_ordinals_excluded:
        raise VoiceProviderBlindTestError("ordinals 2/3 must remain excluded")
    rendered: list[dict[str, Any]] = []
    for _ in range(maximum_outputs):
        root = base._absolute_lexical(voice_root)
        directory, manifest, _ = load_run(
            root,
            run_id,
            expected_manifest_byte_sha256=expected_manifest_byte_sha256,
            revalidate_sources=True,
        )
        state = _audit_state(root, directory / "audits", run_id)
        if state["pending"]:
            raise VoiceProviderBlindTestError(
                "the run has an uncertain synthesis attempt; remaining outputs were not submitted"
            )
        output = _next_output(manifest, state)
        if output is None:
            break
        rendered.append(
            render_one(
                voice_root,
                run_id,
                output["output_id"],
                workspace_id=workspace_id,
                api_key_file=api_key_file,
                dotenv_file=dotenv_file,
                expected_manifest_byte_sha256=expected_manifest_byte_sha256,
                confirm_run_id=run_id,
                confirm_output_id=output["output_id"],
                confirm_model=MODEL,
                confirm_region=REGION,
                confirm_cost_ceiling_usd=str(COST_CEILING_USD),
                confirm_synthesis_and_local_blind_test_authorized=True,
                confirm_paralinguistic_ordinals_excluded=True,
                websocket_factory=websocket_factory,
            )
        )
    validation = validate_run(
        voice_root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    return {
        "status": "render_batch_complete",
        "run_id": run_id,
        "rendered_in_this_command": len(rendered),
        "successful_output_count": validation["successful_output_count"],
        "planned_output_count": EXPECTED_OUTPUT_COUNT,
        "pending_attempt_count": validation["pending_attempt_count"],
        "review_ready": validation["review_ready"],
        "outputs": [
            {
                key: item[key]
                for key in (
                    "output_id",
                    "case_id",
                    "character_slug",
                    "opaque_label",
                    "duration_seconds",
                    "provider_usage_characters",
                )
            }
            for item in rendered
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", type=Path, required=True)
    parser.add_argument("--workspace-id", default=os.getenv("DASHSCOPE_WORKSPACE_ID", ""))
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--dotenv-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="prepare an immutable private blind-test run")
    prepare.add_argument("--preflight-id", default=DEFAULT_PREFLIGHT_ID)
    prepare.add_argument(
        "--expect-preflight-manifest-byte-sha256",
        default=DEFAULT_PREFLIGHT_MANIFEST_BYTE_SHA256,
    )
    prepare.add_argument(
        "--expect-blind-plan-byte-sha256",
        default=DEFAULT_BLIND_PLAN_BYTE_SHA256,
    )
    prepare.add_argument("--enrollment-run-id", default=DEFAULT_ENROLLMENT_RUN_ID)
    prepare.add_argument(
        "--expect-enrollment-manifest-byte-sha256",
        default=DEFAULT_ENROLLMENT_MANIFEST_BYTE_SHA256,
    )
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--prior-stage-usage-characters", type=int, default=0)
    prepare.add_argument("--supersedes-run-id", action="append", default=[])
    prepare.add_argument("--confirm-synthesis-only", action="store_true")
    prepare.add_argument("--execute", action="store_true")

    validate = commands.add_parser("validate", help="validate a run, audits, and rendered WAVs")
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--expect-run-manifest-byte-sha256")
    validate.add_argument("--skip-source-revalidation", action="store_true")

    inspect = commands.add_parser("inspect-next", help="show the next opaque output without credentials")
    inspect.add_argument("--run-id", required=True)

    render = commands.add_parser("render-next", help="render exactly one next output")
    render.add_argument("--run-id", required=True)
    render.add_argument("--expect-run-manifest-byte-sha256")
    render.add_argument("--confirm-run-id", default="")
    render.add_argument("--confirm-output-id", default="")
    render.add_argument("--confirm-model", default="")
    render.add_argument("--confirm-region", default="")
    render.add_argument("--confirm-cost-ceiling-usd", default="")
    render.add_argument(
        "--confirm-synthesis-and-local-blind-test-authorized",
        action="store_true",
    )
    render.add_argument("--confirm-paralinguistic-ordinals-excluded", action="store_true")
    render.add_argument("--execute", action="store_true")

    render_all = commands.add_parser("render-all", help="render remaining outputs sequentially")
    render_all.add_argument("--run-id", required=True)
    render_all.add_argument("--expect-run-manifest-byte-sha256")
    render_all.add_argument("--maximum-outputs", type=int, default=EXPECTED_OUTPUT_COUNT)
    render_all.add_argument("--confirm-run-id", default="")
    render_all.add_argument("--confirm-model", default="")
    render_all.add_argument("--confirm-region", default="")
    render_all.add_argument("--confirm-cost-ceiling-usd", default="")
    render_all.add_argument(
        "--confirm-synthesis-and-local-blind-test-authorized",
        action="store_true",
    )
    render_all.add_argument("--confirm-paralinguistic-ordinals-excluded", action="store_true")
    render_all.add_argument("--execute", action="store_true")

    finalize = commands.add_parser("finalize", help="build the public local review package")
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--expect-run-manifest-byte-sha256")
    finalize.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            if arguments.execute and not arguments.confirm_synthesis_only:
                raise VoiceProviderBlindTestError("--execute requires --confirm-synthesis-only")
            manifest, destination = build_run(
                arguments.voice_root,
                preflight_id=arguments.preflight_id,
                expected_preflight_manifest_byte_sha256=(
                    arguments.expect_preflight_manifest_byte_sha256
                ),
                expected_blind_plan_byte_sha256=arguments.expect_blind_plan_byte_sha256,
                enrollment_run_id=arguments.enrollment_run_id,
                expected_enrollment_manifest_byte_sha256=(
                    arguments.expect_enrollment_manifest_byte_sha256
                ),
                prepared_at=arguments.prepared_at,
                prior_stage_usage_characters=arguments.prior_stage_usage_characters,
                supersedes_run_ids=tuple(arguments.supersedes_run_id),
            )
            if arguments.execute:
                write_run(arguments.voice_root, manifest, destination)
            result = {
                "status": "prepared" if arguments.execute else "dry_run",
                "run_id": manifest["run_id"],
                "path": str(destination),
                "manifest_sha256": manifest["manifest_sha256"],
                "manifest_byte_sha256": base._sha256_bytes(_pretty_json_bytes(manifest)),
                "planned_output_count": EXPECTED_OUTPUT_COUNT,
                "planned_input_unicode_codepoints": EXPECTED_INPUT_CODEPOINTS,
                "planned_billable_input_characters": EXPECTED_BILLABLE_CHARACTERS,
                "prior_stage_provider_usage_characters": manifest["pricing_contract"][
                    "prior_stage_provider_usage_characters"
                ],
                "planned_whole_stage_billable_characters": manifest["pricing_contract"][
                    "planned_whole_stage_billable_characters"
                ],
                "estimated_run_cost_usd": manifest["pricing_contract"][
                    "estimated_run_cost_usd"
                ],
                "estimated_whole_stage_cost_usd": manifest["pricing_contract"][
                    "estimated_whole_stage_cost_usd"
                ],
                "whole_stage_synthesis_cost_ceiling_usd": str(COST_CEILING_USD),
                "credentials_read": False,
                "network_calls_performed": False,
            }
        elif arguments.command == "validate":
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
                revalidate_sources=not arguments.skip_source_revalidation,
            )
        elif arguments.command == "inspect-next":
            result = inspect_next(arguments.voice_root, arguments.run_id)
        elif arguments.command == "render-next" and not arguments.execute:
            result = inspect_next(arguments.voice_root, arguments.run_id)
            result["note"] = "render-next remains offline unless --execute is present"
        elif arguments.command == "render-next":
            inspection = inspect_next(arguments.voice_root, arguments.run_id)
            if inspection.get("status") != "next_output_ready":
                result = inspection
            else:
                result = render_one(
                    arguments.voice_root,
                    arguments.run_id,
                    inspection["output_id"],
                    workspace_id=arguments.workspace_id,
                    api_key_file=arguments.api_key_file,
                    dotenv_file=arguments.dotenv_file,
                    expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
                    confirm_run_id=arguments.confirm_run_id,
                    confirm_output_id=arguments.confirm_output_id,
                    confirm_model=arguments.confirm_model,
                    confirm_region=arguments.confirm_region,
                    confirm_cost_ceiling_usd=arguments.confirm_cost_ceiling_usd,
                    confirm_synthesis_and_local_blind_test_authorized=(
                        arguments.confirm_synthesis_and_local_blind_test_authorized
                    ),
                    confirm_paralinguistic_ordinals_excluded=(
                        arguments.confirm_paralinguistic_ordinals_excluded
                    ),
                )
        elif arguments.command == "render-all" and not arguments.execute:
            result = inspect_next(arguments.voice_root, arguments.run_id)
            result["note"] = "render-all remains offline unless --execute is present"
        elif arguments.command == "render-all":
            result = render_all_remaining(
                arguments.voice_root,
                arguments.run_id,
                workspace_id=arguments.workspace_id,
                api_key_file=arguments.api_key_file,
                dotenv_file=arguments.dotenv_file,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
                confirm_run_id=arguments.confirm_run_id,
                confirm_model=arguments.confirm_model,
                confirm_region=arguments.confirm_region,
                confirm_cost_ceiling_usd=arguments.confirm_cost_ceiling_usd,
                confirm_synthesis_and_local_blind_test_authorized=(
                    arguments.confirm_synthesis_and_local_blind_test_authorized
                ),
                confirm_paralinguistic_ordinals_excluded=(
                    arguments.confirm_paralinguistic_ordinals_excluded
                ),
                maximum_outputs=arguments.maximum_outputs,
            )
        elif not arguments.execute:
            validation = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
            result = {
                **validation,
                "note": "finalize remains a dry run unless --execute is present",
            }
        else:
            result = finalize_review(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
    except (OSError, TimeoutError, base.VoiceParalinguisticError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
