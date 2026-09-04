"""Record the exact Vidya and Chenxing compacted A/B listening approval."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from . import voice_paralinguistic_ops as base
else:
    import voice_paralinguistic_ops as base


SCHEMA = "project-snow-private-local-voice-target-ab-review-1"
OUTPUT_DIRECTORY = "tts_target_ab_reviews"
BATCH_DIRECTORY = "recording_dialogue_comparison_batches"
PACKAGE_DIRECTORY = "recording_dialogue_comparisons"
BATCH_SCHEMA = "project-snow-private-local-voice-dialogue-comparison-batch-1"
PACKAGE_SCHEMA = "project-snow-private-local-voice-dialogue-comparison-1"
REVIEW_ID_PATTERN = re.compile(r"voice-target-ab-review-[0-9a-f]{20}\Z")
BATCH_ID_PATTERN = re.compile(
    r"voice-recording-dialogue-comparison-batch-[0-9a-f]{20}\Z"
)
PACKAGE_ID_PATTERN = re.compile(
    r"voice-recording-dialogue-comparison-[0-9a-f]{20}\Z"
)
DEFAULT_BATCH_ID = "voice-recording-dialogue-comparison-batch-2b1a6205488b251038a3"
EXPECTED_USER_STATEMENT = (
    "四个槽位按推荐方案通过：薇蒂雅 A 压缩版、B 通过；辰星 A/B 压缩版。"
)
TARGETS = (
    {
        "character_id": "5157b8972632",
        "character_name": "薇蒂雅",
        "package_id": "voice-recording-dialogue-comparison-a62af942ac96e7bc059e",
    },
    {
        "character_id": "98322bd505f4",
        "character_name": "辰星",
        "package_id": "voice-recording-dialogue-comparison-9eaa4601e56ff9917069",
    },
)
SCOPE_LIMIT_KEYS = (
    "training_use_approved",
    "voice_cloning_approved",
    "rights_accepted",
    "publication_approved",
    "provider_enrollment_allowed",
    "public_rollout_allowed",
)


class VoiceTargetABReviewError(base.VoiceParalinguisticError):
    """Raised when target A/B review evidence is incomplete or inconsistent."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceTargetABReviewError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceTargetABReviewError(f"{label} must be an object")
    return value


def _require_array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceTargetABReviewError(f"{label} must be an array")
    return value


def _require_integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise VoiceTargetABReviewError(
            f"{label} must be an integer at least {minimum}"
        )
    return value


def _require_number(value: Any, *, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise VoiceTargetABReviewError(f"{label} must be numeric")
    return float(value)


def _scope_limits() -> dict[str, bool]:
    return {key: False for key in SCOPE_LIMIT_KEYS}


def _verify_scope_limits(
    value: Any, *, label: str, exact_keys: bool = False
) -> dict[str, bool]:
    limits = base._require_all_false(value, label=label)
    missing = set(SCOPE_LIMIT_KEYS) - set(limits)
    if missing:
        raise VoiceTargetABReviewError(
            f"{label} is missing required closed gates: {sorted(missing)!r}"
        )
    if exact_keys:
        _expect(tuple(limits), SCOPE_LIMIT_KEYS, label=f"{label} keys")
    return limits


def _safe_asset_path(
    root: Path,
    package_directory: Path,
    relative_value: Any,
    *,
    label: str,
) -> tuple[Path, str]:
    relative = base._safe_relative_path(relative_value, label=f"{label}.relative_path")
    path = package_directory.joinpath(*relative.parts)
    base._require_safe_existing_path(root, path, label=label, directory=False)
    return path, relative.as_posix()


def _verify_asset(
    root: Path,
    package_directory: Path,
    entry_value: Any,
    *,
    label: str,
    hash_field: str,
) -> dict[str, Any]:
    entry = _require_object(entry_value, label=label)
    path, relative_path = _safe_asset_path(
        root,
        package_directory,
        entry.get("relative_path"),
        label=label,
    )
    payload = base._read_stable_bytes(root, path, label=label)
    expected_hash = base._require_sha256(
        entry.get(hash_field), label=f"{label}.{hash_field}"
    )
    actual_hash = base._sha256_bytes(payload)
    _expect(actual_hash, expected_hash, label=f"{label} SHA-256")
    byte_count = _require_integer(
        entry.get("byte_count"), label=f"{label}.byte_count"
    )
    _expect(len(payload), byte_count, label=f"{label} byte count")
    return {
        "relative_path": relative_path,
        "sha256": actual_hash,
        "byte_count": byte_count,
    }


def _verify_audio_entry(
    root: Path,
    package_directory: Path,
    entry_value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    entry = _require_object(entry_value, label=label)
    verified = _verify_asset(
        root,
        package_directory,
        entry,
        label=label,
        hash_field="wav_sha256",
    )
    audio_format = _require_object(
        entry.get("audio_format"), label=f"{label}.audio_format"
    )
    _expect(
        audio_format.get("encoding"),
        "pcm_s16le",
        label=f"{label} encoding",
    )
    _expect(
        audio_format.get("sample_rate_hz"),
        24000,
        label=f"{label} sample rate",
    )
    _expect(audio_format.get("channels"), 1, label=f"{label} channel count")
    _expect(
        audio_format.get("sample_width_bytes"),
        2,
        label=f"{label} sample width",
    )
    duration = _require_number(
        audio_format.get("duration_seconds"),
        label=f"{label}.duration_seconds",
    )
    if not 10.0 <= duration <= 20.0:
        raise VoiceTargetABReviewError(
            f"{label} duration must remain within 10 to 20 seconds"
        )
    verified.update(
        {
            "pcm_sha256": base._require_sha256(
                entry.get("pcm_sha256"), label=f"{label}.pcm_sha256"
            ),
            "audio_format": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 24000,
                "channels": 1,
                "sample_width_bytes": 2,
                "frame_count": _require_integer(
                    audio_format.get("frame_count"),
                    label=f"{label}.frame_count",
                    minimum=1,
                ),
                "duration_seconds": duration,
            },
        }
    )
    return verified


def _range(value: Any, *, label: str) -> tuple[int, int]:
    source_range = _require_object(value, label=label)
    start = _require_integer(
        source_range.get("start_frame"), label=f"{label}.start_frame"
    )
    end = _require_integer(
        source_range.get("end_frame_exclusive"),
        label=f"{label}.end_frame_exclusive",
        minimum=1,
    )
    if end <= start:
        raise VoiceTargetABReviewError(f"{label} must be non-empty")
    return start, end


def _load_batch(
    root: Path,
    batch_id: str,
    *,
    expected_byte_sha256: str | None,
) -> dict[str, Any]:
    safe_batch_id = base._safe_identifier(
        batch_id, BATCH_ID_PATTERN, label="batch_id"
    )
    path = root / BATCH_DIRECTORY / safe_batch_id / "manifest.json"
    batch, payload = base._read_json(root, path, label="comparison batch manifest")
    _expect(batch.get("schema_version"), BATCH_SCHEMA, label="batch schema")
    _expect(batch.get("batch_id"), safe_batch_id, label="batch ID")
    _expect(batch.get("executed"), True, label="batch executed state")
    _verify_scope_limits(batch.get("scope_limits"), label="batch scope_limits")
    semantic_hash = base._verify_semantic_hash(
        batch, field="manifest_sha256", label="comparison batch"
    )
    byte_hash = base._verify_expected_byte_hash(
        payload,
        expected_byte_sha256,
        label="comparison batch byte SHA-256",
    )
    return {
        "document": batch,
        "id": safe_batch_id,
        "manifest_sha256": semantic_hash,
        "byte_sha256": byte_hash,
        "relative_path": (
            f"{BATCH_DIRECTORY}/{safe_batch_id}/manifest.json"
        ),
    }


def _batch_package_entry(
    batch: dict[str, Any], package_id: str
) -> dict[str, Any]:
    matches = [
        item
        for item in _require_array(batch.get("packages"), label="batch packages")
        if isinstance(item, dict) and item.get("package_id") == package_id
    ]
    if len(matches) != 1:
        raise VoiceTargetABReviewError(
            f"batch must contain package {package_id!r} exactly once"
        )
    return matches[0]


def _verify_sample(
    root: Path,
    package_directory: Path,
    sample_value: Any,
    *,
    character_id: str,
    character_name: str,
) -> dict[str, Any]:
    sample = _require_object(sample_value, label="comparison sample")
    slot = sample.get("slot")
    if slot not in {"A", "B"}:
        raise VoiceTargetABReviewError("comparison sample slot must be A or B")
    _expect(
        sample.get("runtime_character_id"),
        character_id,
        label=f"{character_name} {slot} character ID",
    )
    _expect(
        sample.get("runtime_character_name"),
        character_name,
        label=f"{character_name} {slot} character name",
    )
    _expect(
        sample.get("parent_candidate_count"),
        1,
        label=f"{character_name} {slot} parent count",
    )
    _expect(
        sample.get("all_quality_gates_passed"),
        True,
        label=f"{character_name} {slot} aggregate quality gate",
    )
    quality_gates = _require_object(
        sample.get("quality_gates"),
        label=f"{character_name} {slot} quality_gates",
    )
    if not quality_gates or any(value is not True for value in quality_gates.values()):
        raise VoiceTargetABReviewError(
            f"{character_name} {slot} must pass every quality gate"
        )

    displayed_text = _verify_asset(
        root,
        package_directory,
        sample.get("displayed_text"),
        label=f"{character_name} {slot} displayed text",
        hash_field="utf8_sha256",
    )
    transcript_hash = base._require_sha256(
        sample.get("authoritative_transcript_utf8_sha256"),
        label=f"{character_name} {slot} transcript SHA-256",
    )
    _expect(
        displayed_text["sha256"],
        transcript_hash,
        label=f"{character_name} {slot} displayed text authority",
    )
    natural = _verify_audio_entry(
        root,
        package_directory,
        sample.get("natural_audio"),
        label=f"{character_name} {slot} natural audio",
    )
    compacted = _verify_audio_entry(
        root,
        package_directory,
        sample.get("compacted_audio"),
        label=f"{character_name} {slot} compacted audio",
    )
    input_audio = _require_object(
        sample.get("input_candidate_audio"),
        label=f"{character_name} {slot} input candidate audio",
    )
    source_start, source_end = _range(
        input_audio.get("source_frame_range"),
        label=f"{character_name} {slot} source frame range",
    )
    cut_count = _require_integer(
        sample.get("internal_silence_compaction_cut_count"),
        label=f"{character_name} {slot} cut count",
    )
    cuts = _require_array(
        sample.get("internal_silence_compaction_cuts"),
        label=f"{character_name} {slot} compaction cuts",
    )
    _expect(len(cuts), cut_count, label=f"{character_name} {slot} cut list")
    return {
        "slot": slot,
        "candidate_ordinal": _require_integer(
            sample.get("candidate_ordinal"),
            label=f"{character_name} {slot} candidate ordinal",
            minimum=1,
        ),
        "original_candidate_id": base._require_string(
            sample.get("original_candidate_id"),
            label=f"{character_name} {slot} original candidate ID",
        ),
        "source_id": base._require_string(
            sample.get("source_id"), label=f"{character_name} {slot} source ID"
        ),
        "source_frame_range": {
            "start_frame": source_start,
            "end_frame_exclusive": source_end,
        },
        "authoritative_transcript_utf8_sha256": transcript_hash,
        "input_candidate_wav_sha256": base._require_sha256(
            input_audio.get("wav_sha256"),
            label=f"{character_name} {slot} input WAV SHA-256",
        ),
        "displayed_text": displayed_text,
        "natural_audio": natural,
        "selected_audio": compacted,
        "selected_variant": "compacted",
        "natural_and_compacted_identical": (
            natural["sha256"] == compacted["sha256"]
            and natural["byte_count"] == compacted["byte_count"]
        ),
        "internal_silence_compaction_cut_count": cut_count,
        "decision": "compacted_accepted_no_issue",
        "human_assessment": "no_material_audible_difference_reported",
        "issue_codes": [],
    }


def _verify_ab_isolation(
    decisions: list[dict[str, Any]], *, character_name: str
) -> dict[str, bool]:
    ordered = sorted(decisions, key=lambda item: item["slot"])
    _expect([item["slot"] for item in ordered], ["A", "B"], label="A/B slots")
    unique_fields = {
        "candidate_ordinals_unique": "candidate_ordinal",
        "original_candidate_ids_unique": "original_candidate_id",
        "transcript_hashes_unique": "authoritative_transcript_utf8_sha256",
        "input_audio_hashes_unique": "input_candidate_wav_sha256",
        "selected_audio_hashes_unique": None,
    }
    assertions: dict[str, bool] = {}
    for assertion, field in unique_fields.items():
        values = (
            [item["selected_audio"]["sha256"] for item in ordered]
            if field is None
            else [item[field] for item in ordered]
        )
        assertions[assertion] = len(set(values)) == 2
        if not assertions[assertion]:
            raise VoiceTargetABReviewError(
                f"{character_name} violates {assertion}"
            )

    first, second = ordered
    if first["source_id"] == second["source_id"]:
        first_range = first["source_frame_range"]
        second_range = second["source_frame_range"]
        overlap = not (
            first_range["end_frame_exclusive"] <= second_range["start_frame"]
            or second_range["end_frame_exclusive"] <= first_range["start_frame"]
        )
    else:
        overlap = False
    assertions["source_frame_ranges_non_overlapping"] = not overlap
    if overlap:
        raise VoiceTargetABReviewError(
            f"{character_name} A/B source frame ranges overlap"
        )
    return assertions


def _load_package(
    root: Path,
    batch: dict[str, Any],
    target: dict[str, str],
    *,
    expected_byte_sha256: str | None,
) -> dict[str, Any]:
    package_id = base._safe_identifier(
        target["package_id"], PACKAGE_ID_PATTERN, label="package_id"
    )
    entry = _batch_package_entry(batch, package_id)
    package_directory = root / PACKAGE_DIRECTORY / package_id
    path = package_directory / "manifest.json"
    package, payload = base._read_json(root, path, label="comparison package manifest")
    _expect(package.get("schema_version"), PACKAGE_SCHEMA, label="package schema")
    _expect(package.get("package_id"), package_id, label="package ID")
    _expect(package.get("executed"), True, label="package executed state")
    _expect(
        package.get("review_status"),
        "awaiting_human_natural_vs_compacted_comparison",
        label="package prior review status",
    )
    _verify_scope_limits(package.get("scope_limits"), label="package scope_limits")
    manifest_hash = base._verify_semantic_hash(
        package, field="manifest_sha256", label="comparison package"
    )
    byte_hash = base._verify_expected_byte_hash(
        payload,
        expected_byte_sha256,
        label=f"{target['character_name']} package byte SHA-256",
    )
    _expect(
        entry.get("manifest_sha256"),
        manifest_hash,
        label="batch package manifest SHA-256",
    )
    _expect(
        entry.get("runtime_character_id"),
        target["character_id"],
        label="batch character ID",
    )
    _expect(
        entry.get("runtime_character_name"),
        target["character_name"],
        label="batch character name",
    )
    source_policy = _require_object(
        batch.get("source_policy_review"), label="batch source_policy_review"
    )
    policy = _require_object(package.get("quality_policy"), label="quality_policy")
    _expect(
        base._semantic_sha256(policy),
        source_policy.get("approved_policy_sha256"),
        label="approved quality policy SHA-256",
    )
    _expect(
        package.get("source_boundary_adjudication"),
        batch.get("source_boundary_adjudication"),
        label="source boundary adjudication",
    )
    samples = _require_array(package.get("samples"), label="package samples")
    _expect(package.get("sample_count"), 2, label="package sample_count")
    _expect(len(samples), 2, label="package sample length")
    decisions = [
        _verify_sample(
            root,
            package_directory,
            sample,
            character_id=target["character_id"],
            character_name=target["character_name"],
        )
        for sample in samples
    ]
    decisions.sort(key=lambda item: item["slot"])
    review_page = _verify_asset(
        root,
        package_directory,
        package.get("review_page"),
        label=f"{target['character_name']} review page",
        hash_field="sha256",
    )
    isolation = _verify_ab_isolation(
        decisions, character_name=target["character_name"]
    )
    return {
        "package_id": package_id,
        "profile_id": base._require_string(
            entry.get("profile_id"), label="batch profile_id"
        ),
        "runtime_character_id": target["character_id"],
        "runtime_character_name": target["character_name"],
        "manifest_sha256": manifest_hash,
        "byte_sha256": byte_hash,
        "relative_path": f"{PACKAGE_DIRECTORY}/{package_id}/manifest.json",
        "prior_review_status": package["review_status"],
        "review_page": review_page,
        "decisions": decisions,
        "ab_isolation": isolation,
        "verified_asset_count": 7,
    }


def _expected_package_hashes(
    values: dict[str, str] | None,
) -> dict[str, str | None]:
    expected = {target["package_id"]: None for target in TARGETS}
    if values is None:
        return expected
    unknown = set(values) - set(expected)
    if unknown:
        raise VoiceTargetABReviewError(
            f"unexpected package hash pins: {sorted(unknown)!r}"
        )
    for package_id, digest in values.items():
        expected[package_id] = base._require_sha256(
            digest, label=f"expected {package_id} byte SHA-256"
        )
    return expected


def build_review_receipt(
    voice_root: Path,
    *,
    batch_id: str = DEFAULT_BATCH_ID,
    reviewer_id: str = "xiaob",
    reviewed_at: str | None = None,
    user_statement: str = EXPECTED_USER_STATEMENT,
    expected_batch_sha256: str | None = None,
    expected_package_sha256: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    reviewer = base._require_string(reviewer_id, label="reviewer_id")
    recorded = base._parse_recorded_at(reviewed_at)
    statement = base._require_string(user_statement, label="user_statement")
    _expect(statement, EXPECTED_USER_STATEMENT, label="exact user approval statement")
    batch_anchor = _load_batch(
        root, batch_id, expected_byte_sha256=expected_batch_sha256
    )
    expected_hashes = _expected_package_hashes(expected_package_sha256)
    packages = [
        _load_package(
            root,
            batch_anchor["document"],
            target,
            expected_byte_sha256=expected_hashes[target["package_id"]],
        )
        for target in TARGETS
    ]
    package_anchors = [
        {
            "package_id": item["package_id"],
            "runtime_character_id": item["runtime_character_id"],
            "runtime_character_name": item["runtime_character_name"],
            "manifest_sha256": item["manifest_sha256"],
            "byte_sha256": item["byte_sha256"],
        }
        for item in packages
    ]
    decisions = [
        {
            "package_id": package["package_id"],
            "runtime_character_id": package["runtime_character_id"],
            "slot": decision["slot"],
            "candidate_ordinal": decision["candidate_ordinal"],
            "selected_wav_sha256": decision["selected_audio"]["sha256"],
            "decision": decision["decision"],
        }
        for package in packages
        for decision in package["decisions"]
    ]
    statement_hash = base._sha256_bytes(statement.encode("utf-8"))
    stable_basis = {
        "schema_version": SCHEMA,
        "reviewed_at": recorded,
        "reviewer_id": reviewer,
        "user_statement_utf8_sha256": statement_hash,
        "batch_manifest_sha256": batch_anchor["manifest_sha256"],
        "package_anchors": package_anchors,
        "decisions": decisions,
    }
    stable_identity = base._semantic_sha256(stable_basis)
    review_id = f"voice-target-ab-review-{stable_identity[:20]}"
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA,
        "review_id": review_id,
        "reviewed_at": recorded,
        "artifact_purpose": (
            "record_exact_human_natural_vs_compacted_selection_for_local_ab_only"
        ),
        "reviewer_id": reviewer,
        "authorization_evidence": {
            "source": "current_codex_task_user_message",
            "statement": statement,
            "statement_utf8_sha256": statement_hash,
        },
        "source_batch": {
            key: value
            for key, value in batch_anchor.items()
            if key != "document"
        },
        "package_set_sha256": base._semantic_sha256(package_anchors),
        "decision_set_sha256": base._semantic_sha256(decisions),
        "packages": packages,
        "summary": {
            "package_count": 2,
            "character_count": 2,
            "slot_count": 4,
            "compacted_accepted_no_issue_count": 4,
            "natural_and_compacted_identical_count": sum(
                decision["natural_and_compacted_identical"]
                for package in packages
                for decision in package["decisions"]
            ),
            "verified_asset_count": sum(
                package["verified_asset_count"] for package in packages
            ),
            "all_signal_quality_gates_passed": True,
            "all_ab_isolation_checks_passed": True,
        },
        "approval_scope": {
            "human_listening_completed": True,
            "compacted_variant_selected_for_local_ab_candidates": True,
            "no_material_audible_difference_reported": True,
            "natural_masters_retained": True,
        },
        "scope_limits": _scope_limits(),
        "executed": True,
        "stable_identity": stable_identity,
    }
    receipt["receipt_sha256"] = base._semantic_sha256(receipt)
    destination = root / OUTPUT_DIRECTORY / f"{review_id}.json"
    return receipt, destination


def _validate_shape(receipt: dict[str, Any]) -> None:
    _expect(receipt.get("schema_version"), SCHEMA, label="receipt schema")
    review_id = base._safe_identifier(
        receipt.get("review_id"), REVIEW_ID_PATTERN, label="review_id"
    )
    stable_identity = base._require_sha256(
        receipt.get("stable_identity"), label="stable_identity"
    )
    _expect(
        review_id,
        f"voice-target-ab-review-{stable_identity[:20]}",
        label="review ID derivation",
    )
    base._verify_semantic_hash(
        receipt, field="receipt_sha256", label="target A/B review receipt"
    )
    _verify_scope_limits(
        receipt.get("scope_limits"),
        label="receipt scope_limits",
        exact_keys=True,
    )
    summary = _require_object(receipt.get("summary"), label="receipt summary")
    _expect(summary.get("package_count"), 2, label="summary package_count")
    _expect(summary.get("slot_count"), 4, label="summary slot_count")
    _expect(
        summary.get("verified_asset_count"), 14, label="summary asset count"
    )


def validate_review_receipt(voice_root: Path, review_id: str) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    safe_review_id = base._safe_identifier(
        review_id, REVIEW_ID_PATTERN, label="review_id"
    )
    path = root / OUTPUT_DIRECTORY / f"{safe_review_id}.json"
    receipt, payload = base._read_json(root, path, label="target A/B review receipt")
    _validate_shape(receipt)
    source_batch = _require_object(
        receipt.get("source_batch"), label="receipt source_batch"
    )
    package_hashes = {
        package["package_id"]: package["byte_sha256"]
        for package in _require_array(receipt.get("packages"), label="receipt packages")
        if isinstance(package, dict)
    }
    authorization = _require_object(
        receipt.get("authorization_evidence"),
        label="receipt authorization_evidence",
    )
    rebuilt, rebuilt_path = build_review_receipt(
        root,
        batch_id=source_batch.get("id"),
        reviewer_id=receipt.get("reviewer_id"),
        reviewed_at=receipt.get("reviewed_at"),
        user_statement=authorization.get("statement"),
        expected_batch_sha256=source_batch.get("byte_sha256"),
        expected_package_sha256=package_hashes,
    )
    _expect(rebuilt_path, path, label="rebuilt receipt path")
    _expect(rebuilt, receipt, label="receipt/source reconstruction")
    return {
        "status": "valid",
        "review_id": safe_review_id,
        "path": str(path),
        "receipt_sha256": receipt["receipt_sha256"],
        "byte_sha256": base._sha256_bytes(payload),
        "slot_count": 4,
        "verified_asset_count": 14,
        "all_scope_gates_closed": True,
    }


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _existing_receipt(
    root: Path, receipt: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    validate_review_receipt(root, receipt["review_id"])
    path = root / OUTPUT_DIRECTORY / f"{receipt['review_id']}.json"
    existing, _ = base._read_json(root, path, label="existing target A/B receipt")
    if existing.get("stable_identity") != receipt.get("stable_identity"):
        return "existing_conflict", existing
    return "existing_valid", existing


def write_review_receipt(
    voice_root: Path,
    receipt: dict[str, Any],
    destination: Path,
) -> tuple[str, dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    _validate_shape(receipt)
    output_directory = root / OUTPUT_DIRECTORY
    if output_directory.exists():
        base._require_safe_existing_path(
            root,
            output_directory,
            label="target A/B review output directory",
            directory=True,
        )
    else:
        try:
            output_directory.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(
            root,
            output_directory,
            label="target A/B review output directory",
            directory=True,
        )
    expected_destination = output_directory / f"{receipt['review_id']}.json"
    _expect(
        base._absolute_lexical(destination),
        expected_destination,
        label="receipt destination",
    )
    if expected_destination.exists():
        return _existing_receipt(root, receipt)
    payload = _pretty_json_bytes(receipt)
    temporary = output_directory / (
        f".{receipt['review_id']}.{uuid.uuid4().hex}.partial"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        base._require_safe_existing_path(
            root, temporary, label="temporary target A/B receipt", directory=False
        )
        try:
            os.link(temporary, expected_destination)
        except FileExistsError:
            return _existing_receipt(root, receipt)
        validation = validate_review_receipt(root, receipt["review_id"])
        _expect(
            validation["receipt_sha256"],
            receipt["receipt_sha256"],
            label="written receipt SHA-256",
        )
        return "created", receipt
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parse_package_hashes(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        package_id, separator, digest = value.partition("=")
        if not separator or package_id in result:
            raise VoiceTargetABReviewError(
                "--expect-package-sha256 requires unique PACKAGE_ID=SHA256 values"
            )
        result[package_id] = digest
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser(
        "record", help="dry-run or record the exact four-slot human approval"
    )
    record.add_argument("--voice-root", type=Path, required=True)
    record.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    record.add_argument("--reviewer-id", default="xiaob")
    record.add_argument("--reviewed-at")
    record.add_argument("--expect-batch-sha256")
    record.add_argument(
        "--expect-package-sha256", action="append", default=[]
    )
    record.add_argument("--confirm-four-compacted-selections", action="store_true")
    record.add_argument("--execute", action="store_true")
    validate = subparsers.add_parser(
        "validate", help="validate the receipt and all pinned source assets"
    )
    validate.add_argument("--voice-root", type=Path, required=True)
    validate.add_argument("--review-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            result = validate_review_receipt(
                arguments.voice_root, arguments.review_id
            )
        else:
            if arguments.execute and not arguments.confirm_four_compacted_selections:
                raise VoiceTargetABReviewError(
                    "--execute requires --confirm-four-compacted-selections"
                )
            package_hashes = _parse_package_hashes(
                arguments.expect_package_sha256
            )
            receipt, destination = build_review_receipt(
                arguments.voice_root,
                batch_id=arguments.batch_id,
                reviewer_id=arguments.reviewer_id,
                reviewed_at=arguments.reviewed_at,
                expected_batch_sha256=arguments.expect_batch_sha256,
                expected_package_sha256=package_hashes or None,
            )
            write_status = None
            if arguments.execute:
                write_status, receipt = write_review_receipt(
                    arguments.voice_root, receipt, destination
                )
                if write_status == "existing_conflict":
                    raise VoiceTargetABReviewError(
                        "an immutable receipt already exists with conflicting content"
                    )
            result = {
                "status": "ok",
                "mode": "execute" if arguments.execute else "dry_run",
                "write_status": write_status,
                "review_id": receipt["review_id"],
                "path": str(destination),
                "receipt_sha256": receipt["receipt_sha256"],
                "decision_set_sha256": receipt["decision_set_sha256"],
                "slot_count": receipt["summary"]["slot_count"],
                "verified_asset_count": receipt["summary"][
                    "verified_asset_count"
                ],
                "all_scope_gates_closed": all(
                    state is False for state in receipt["scope_limits"].values()
                ),
            }
    except (OSError, base.VoiceParalinguisticError) as error:
        print(
            json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
