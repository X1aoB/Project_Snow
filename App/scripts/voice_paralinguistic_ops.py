"""Record fail-closed paralinguistic classifications for reviewed voice spans.

The command intentionally appends an immutable receipt. It never edits source
review submissions, manifests, queues, or audio clips.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

RECEIPT_SCHEMA = "project-snow-private-voice-paralinguistic-review-1"
CLASSIFICATION_PROFILE = "project-snow-paralinguistic-retention-profile-1"
SUBMISSION_SCHEMA = "project-snow-private-voice-span-asr-review-submission-1"
RUN_SCHEMA = "project-snow-private-voice-span-audio-asr-run-1"
PACKAGE_SCHEMA = "project-snow-private-voice-span-review-package-3"
QUEUE_SCHEMA = "project-snow-private-voice-span-review-queue-1"

TARGET_SURFACES = {2: "啊啊啊", 3: "啊"}
TARGET_ORDINALS = tuple(TARGET_SURFACES)
ALLOWED_EVENT_KINDS = {"paralinguistic_event"}
ALLOWED_EVENT_TYPES = {"nonlexical_murmur"}
ALLOWED_BASE_TTS_STATES = {"excluded"}
ALLOWED_PENDING_STATES = {"pending_human_event_qa"}

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SUBMISSION_ID_PATTERN = re.compile(r"voice-span-asr-review-submission-[0-9a-f]{20}\Z")
RUN_ID_PATTERN = re.compile(r"voice-span-asr-run-[0-9a-f]{20}\Z")
PACKAGE_ID_PATTERN = re.compile(r"voice-span-review-package-[0-9a-f]{20}\Z")
REVIEW_ID_PATTERN = re.compile(r"voice-paralinguistic-review-[0-9a-f]{20}\Z")
REVIEWER_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")

OUTPUT_DIRECTORY = "paralinguistic_event_reviews"
CHUNK_SIZE = 1024 * 1024


class VoiceParalinguisticError(RuntimeError):
    """Raised when a source or receipt violates the fail-closed contract."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _semantic_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VoiceParalinguisticError(f"JSON contains duplicate key: {key!r}")
        result[key] = value
    return result


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _require_safe_existing_path(root: Path, path: Path, *, label: str, directory: bool) -> Path:
    root_lexical = _absolute_lexical(root)
    path_lexical = _absolute_lexical(path)
    try:
        root_metadata = root_lexical.lstat()
    except FileNotFoundError as error:
        raise VoiceParalinguisticError(f"voice root does not exist: {root_lexical}") from error
    if stat.S_ISLNK(root_metadata.st_mode) or _is_reparse_point(root_metadata):
        raise VoiceParalinguisticError("voice root must not be a link or reparse point")
    try:
        relative = path_lexical.relative_to(root_lexical)
    except ValueError as error:
        raise VoiceParalinguisticError(f"{label} escapes the voice root") from error

    current = root_lexical
    for part in relative.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise VoiceParalinguisticError(f"{label} does not exist: {current}") from error
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise VoiceParalinguisticError(f"{label} traverses a link or reparse point: {current}")

    try:
        root_resolved = root_lexical.resolve(strict=True)
        path_resolved = path_lexical.resolve(strict=True)
        path_resolved.relative_to(root_resolved)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise VoiceParalinguisticError(f"{label} does not resolve inside the voice root") from error

    metadata = path_lexical.lstat()
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected:
        kind = "directory" if directory else "regular file"
        raise VoiceParalinguisticError(f"{label} is not a {kind}")
    return path_lexical


def _read_stable_bytes(root: Path, path: Path, *, label: str) -> bytes:
    safe_path = _require_safe_existing_path(root, path, label=label, directory=False)
    before = safe_path.lstat()
    with safe_path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        payload = bytearray()
        while True:
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            payload.extend(block)
    after = safe_path.lstat()
    identities = {
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns),
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
    }
    if len(identities) != 1 or len(payload) != before.st_size:
        raise VoiceParalinguisticError(f"{label} changed while it was being read")
    return bytes(payload)


def _read_json(root: Path, path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    payload = _read_stable_bytes(root, path, label=label)
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except UnicodeDecodeError as error:
        raise VoiceParalinguisticError(f"{label} is not UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise VoiceParalinguisticError(f"{label} is invalid JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise VoiceParalinguisticError(f"{label} must contain one JSON object")
    return value, payload


def _require_string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VoiceParalinguisticError(f"{label} must be a non-empty string")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    digest = _require_string(value, label=label)
    if digest != digest.lower() or not SHA256_PATTERN.fullmatch(digest):
        raise VoiceParalinguisticError(f"{label} must be a lowercase SHA-256 digest")
    return digest


def _expect_equal(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceParalinguisticError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _verify_semantic_hash(document: dict[str, Any], *, field: str, label: str) -> str:
    embedded = _require_sha256(document.get(field), label=f"{label}.{field}")
    basis = dict(document)
    basis.pop(field, None)
    calculated = _semantic_sha256(basis)
    _expect_equal(calculated, embedded, label=f"{label} semantic SHA-256")
    return embedded


def _verify_expected_byte_hash(payload: bytes, expected: str | None, *, label: str) -> str:
    calculated = _sha256_bytes(payload)
    if expected is not None:
        _expect_equal(calculated, _require_sha256(expected, label=f"expected {label}"), label=label)
    return calculated


def _require_all_false(value: Any, *, label: str) -> dict[str, bool]:
    if not isinstance(value, dict) or not value:
        raise VoiceParalinguisticError(f"{label} must be a non-empty object")
    for key, state in value.items():
        if state is not False:
            raise VoiceParalinguisticError(f"{label}.{key} must remain false")
    return value


def _require_list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceParalinguisticError(f"{label} must be an array")
    return value


def _index_by_ordinal(items: Any, *, label: str) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for item in _require_list(items, label=label):
        if not isinstance(item, dict):
            raise VoiceParalinguisticError(f"{label} entries must be objects")
        ordinal = item.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise VoiceParalinguisticError(f"{label} contains an invalid ordinal")
        if ordinal in indexed:
            raise VoiceParalinguisticError(f"{label} contains duplicate ordinal {ordinal}")
        indexed[ordinal] = item
    return indexed


def _safe_identifier(value: Any, pattern: re.Pattern[str], *, label: str) -> str:
    identifier = _require_string(value, label=label)
    if not pattern.fullmatch(identifier):
        raise VoiceParalinguisticError(f"{label} has an invalid format")
    return identifier


def _safe_relative_path(value: Any, *, label: str) -> PurePosixPath:
    raw = _require_string(value, label=label)
    if "\\" in raw:
        raise VoiceParalinguisticError(f"{label} must use POSIX separators")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise VoiceParalinguisticError(f"{label} is not a safe relative path")
    if relative.as_posix() != raw:
        raise VoiceParalinguisticError(f"{label} is not canonical")
    return relative


def _parse_recorded_at(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VoiceParalinguisticError("recorded_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VoiceParalinguisticError("recorded_at must include a UTC offset")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _same_instant(left: Any, right: Any, *, label: str) -> None:
    left_value = _require_string(left, label=f"{label} left timestamp")
    right_value = _require_string(right, label=f"{label} right timestamp")
    try:
        left_time = datetime.fromisoformat(left_value.replace("Z", "+00:00"))
        right_time = datetime.fromisoformat(right_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VoiceParalinguisticError(f"{label} contains an invalid timestamp") from error
    if left_time.tzinfo is None or right_time.tzinfo is None or left_time != right_time:
        raise VoiceParalinguisticError(f"{label} timestamps do not identify the same instant")


def _validate_wav(payload: bytes, clip: dict[str, Any], run_clip: dict[str, Any], *, ordinal: int) -> None:
    try:
        with wave.open(io.BytesIO(payload), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frame_count = stream.getnframes()
            compression = stream.getcomptype()
    except (EOFError, wave.Error) as error:
        raise VoiceParalinguisticError(f"clip {ordinal} is not a readable PCM WAV") from error

    package_format = clip.get("audio_format")
    run_format = run_clip.get("audio_format")
    if not isinstance(package_format, dict) or not isinstance(run_format, dict):
        raise VoiceParalinguisticError(f"clip {ordinal} is missing audio format metadata")
    expected = {
        "encoding": "pcm_s16le",
        "sample_rate_hz": 24000,
        "channels": 1,
        "sample_width_bytes": 2,
    }
    observed = {
        "encoding": "pcm_s16le" if compression == "NONE" else compression,
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
    }
    _expect_equal(observed, expected, label=f"clip {ordinal} WAV format")
    for key, expected_value in expected.items():
        _expect_equal(package_format.get(key), expected_value, label=f"clip {ordinal} package {key}")
        _expect_equal(run_format.get(key), expected_value, label=f"clip {ordinal} ASR {key}")
    _expect_equal(clip.get("frame_count"), frame_count, label=f"clip {ordinal} package frame count")
    _expect_equal(run_format.get("frame_count"), frame_count, label=f"clip {ordinal} ASR frame count")
    duration = frame_count / sample_rate
    for source_label, value in (
        ("package duration", clip.get("duration_seconds")),
        ("ASR duration", run_clip.get("duration_seconds")),
        ("ASR format duration", run_format.get("duration_seconds")),
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or abs(float(value) - duration) > 1e-9
        ):
            raise VoiceParalinguisticError(f"clip {ordinal} {source_label} does not match WAV frames")


def _load_sources(
    voice_root: Path,
    submission_id: str,
    *,
    expected_submission_sha256: str | None,
    expected_run_sha256: str | None,
    expected_package_sha256: str | None,
    expected_queue_sha256: str | None,
) -> dict[str, Any]:
    root = _absolute_lexical(voice_root)
    _require_safe_existing_path(root, root, label="voice root", directory=True)
    submission_id = _safe_identifier(submission_id, SUBMISSION_ID_PATTERN, label="submission_id")

    submission_path = root / "span_asr_review_submissions" / f"{submission_id}.json"
    submission, submission_bytes = _read_json(root, submission_path, label="source submission")
    submission_byte_sha = _verify_expected_byte_hash(
        submission_bytes, expected_submission_sha256, label="source submission byte SHA-256"
    )
    _expect_equal(submission.get("schema_version"), SUBMISSION_SCHEMA, label="submission schema")
    _expect_equal(submission.get("submission_id"), submission_id, label="submission ID")
    submission_receipt_sha = _verify_semantic_hash(
        submission, field="receipt_sha256", label="source submission"
    )
    _expect_equal(submission.get("review_status"), "needs_clarification", label="submission review status")
    _expect_equal(submission.get("unresolved_ordinals"), list(TARGET_ORDINALS), label="unresolved ordinals")
    _require_all_false(submission.get("scope_limits"), label="submission scope_limits")
    reviewer_assertions = submission.get("reviewer_assertions")
    if not isinstance(reviewer_assertions, dict) or reviewer_assertions.get("audio_only_review") is not True:
        raise VoiceParalinguisticError("submission must attest to an audio-only review")

    source_run = submission.get("source_run")
    if not isinstance(source_run, dict):
        raise VoiceParalinguisticError("submission source_run must be an object")
    run_id = _safe_identifier(source_run.get("run_id"), RUN_ID_PATTERN, label="run_id")
    run_path = root / "span_asr_runs" / f"{run_id}.json"
    run, run_bytes = _read_json(root, run_path, label="source ASR run")
    run_byte_sha = _verify_expected_byte_hash(run_bytes, expected_run_sha256, label="ASR run byte SHA-256")
    _expect_equal(run.get("schema_version"), RUN_SCHEMA, label="ASR run schema")
    _expect_equal(run.get("run_id"), run_id, label="ASR run ID")
    run_manifest_sha = _verify_semantic_hash(run, field="manifest_sha256", label="source ASR run")
    _expect_equal(source_run.get("manifest_sha256"), run_manifest_sha, label="submission ASR run SHA-256")
    _same_instant(source_run.get("created_at"), run.get("created_at"), label="ASR run creation")
    _require_all_false(run.get("scope_limits"), label="ASR run scope_limits")
    input_policy = run.get("input_policy")
    if not isinstance(input_policy, dict):
        raise VoiceParalinguisticError("ASR run input_policy must be an object")
    _expect_equal(input_policy.get("input_kind"), "wav_bytes_only", label="ASR input kind")
    for key in (
        "prompt_used",
        "reference_transcript_used",
        "hotwords_used",
        "character_metadata_used",
        "cross_clip_context_used",
        "external_audio_disclosure",
    ):
        _expect_equal(input_policy.get(key), False, label=f"ASR input policy {key}")

    package_anchor = run.get("source_package")
    if not isinstance(package_anchor, dict):
        raise VoiceParalinguisticError("ASR run source_package must be an object")
    package_id = _safe_identifier(package_anchor.get("package_id"), PACKAGE_ID_PATTERN, label="package_id")
    package_path = root / "span_review_packages" / package_id / "manifest.json"
    package, package_bytes = _read_json(root, package_path, label="source package manifest")
    package_byte_sha = _verify_expected_byte_hash(
        package_bytes, expected_package_sha256, label="package manifest byte SHA-256"
    )
    _expect_equal(package.get("schema_version"), PACKAGE_SCHEMA, label="package schema")
    _expect_equal(package.get("package_id"), package_id, label="package ID")
    package_manifest_sha = _verify_semantic_hash(package, field="manifest_sha256", label="source package")
    _expect_equal(package.get("review_status"), "pending_human_listening", label="package review status")
    _expect_equal(package_anchor.get("manifest_sha256"), package_manifest_sha, label="ASR package SHA-256")
    _expect_equal(
        source_run.get("source_package_manifest_sha256"),
        package_manifest_sha,
        label="submission package SHA-256",
    )
    _require_all_false(package.get("scope_limits"), label="package scope_limits")
    authorization = package.get("materialization_authorization")
    if not isinstance(authorization, dict):
        raise VoiceParalinguisticError("package materialization_authorization must be an object")
    _expect_equal(
        authorization.get("authorization_scope"),
        "local_human_span_listening_only",
        label="package authorization scope",
    )
    _expect_equal(
        authorization.get("review_clip_materialization_allowed"),
        True,
        label="review clip materialization authorization",
    )
    for key in (
        "source_files_may_be_modified",
        "span_review_may_be_marked_complete",
        "concatenation_allowed",
        "rights_accepted",
        "publication_approved",
        "provider_enrollment_allowed",
    ):
        _expect_equal(authorization.get(key), False, label=f"package authorization {key}")

    queue_path = root / "operator_span_review_queue.json"
    queue, queue_bytes = _read_json(root, queue_path, label="span review queue")
    queue_byte_sha = _verify_expected_byte_hash(
        queue_bytes, expected_queue_sha256, label="queue byte SHA-256"
    )
    _expect_equal(queue.get("schema_version"), QUEUE_SCHEMA, label="queue schema")
    _expect_equal(package.get("span_review_queue_sha256"), queue_byte_sha, label="package queue SHA-256")
    _expect_equal(package_anchor.get("span_review_queue_sha256"), queue_byte_sha, label="ASR queue SHA-256")
    _expect_equal(
        package_anchor.get("inventory_sha256"),
        package.get("inventory_sha256"),
        label="ASR package inventory SHA-256",
    )
    _require_all_false(queue.get("scope_limits"), label="queue scope_limits")

    package_clips = _index_by_ordinal(package.get("clips"), label="package clips")
    run_clips = _index_by_ordinal(run.get("clips"), label="ASR clips")
    decisions = _index_by_ordinal(submission.get("decisions"), label="submission decisions")
    _expect_equal(package.get("clip_count"), len(package_clips), label="package clip count")
    _expect_equal(run.get("clip_count"), len(run_clips), label="ASR clip count")
    _expect_equal(submission.get("clip_count"), len(decisions), label="submission clip count")
    _expect_equal(set(package_clips), set(run_clips), label="package/ASR ordinal set")
    _expect_equal(set(run_clips), set(decisions), label="ASR/submission ordinal set")
    for key in ("input_audio_set_sha256", "transcript_set_sha256", "clip_count"):
        _expect_equal(source_run.get(key), run.get(key), label=f"submission ASR {key}")

    queue_characters = queue.get("characters")
    if not isinstance(queue_characters, list):
        raise VoiceParalinguisticError("queue characters must be an array")

    events: list[dict[str, Any]] = []
    for ordinal, expected_surface in TARGET_SURFACES.items():
        if ordinal not in package_clips or ordinal not in run_clips or ordinal not in decisions:
            raise VoiceParalinguisticError(f"target ordinal {ordinal} is absent from a source artifact")
        clip = package_clips[ordinal]
        run_clip = run_clips[ordinal]
        decision = decisions[ordinal]

        span_id = _require_string(clip.get("span_id"), label=f"clip {ordinal} span_id")
        _expect_equal(run_clip.get("span_id"), span_id, label=f"clip {ordinal} ASR span_id")
        _expect_equal(decision.get("span_id"), span_id, label=f"clip {ordinal} submission span_id")
        audio_sha = _require_sha256(clip.get("output_wav_sha256"), label=f"clip {ordinal} audio SHA-256")
        _expect_equal(run_clip.get("audio_sha256"), audio_sha, label=f"clip {ordinal} ASR audio SHA-256")
        _expect_equal(
            decision.get("audio_sha256"),
            audio_sha,
            label=f"clip {ordinal} submission audio SHA-256",
        )

        transcript_review = decision.get("transcript_review")
        training_use = decision.get("training_use")
        if not isinstance(transcript_review, dict) or not isinstance(training_use, dict):
            raise VoiceParalinguisticError(f"clip {ordinal} review decision is incomplete")
        _expect_equal(
            transcript_review.get("decision"),
            "needs_clarification",
            label=f"clip {ordinal} transcript decision",
        )
        _expect_equal(transcript_review.get("reviewed_text"), None, label=f"clip {ordinal} reviewed text")
        _expect_equal(
            transcript_review.get("reason_code"),
            "needs_exact_transcript_decision",
            label=f"clip {ordinal} transcript reason",
        )
        _expect_equal(
            training_use.get("disposition"),
            "excluded_from_current_training_set",
            label=f"clip {ordinal} training disposition",
        )
        _expect_equal(
            training_use.get("reason_code"),
            "nonlexical_vocalization",
            label=f"clip {ordinal} training reason",
        )

        surface = _require_string(run_clip.get("text"), label=f"clip {ordinal} ASR surface")
        _expect_equal(surface, expected_surface, label=f"clip {ordinal} nonlexical surface")
        surface_sha = _sha256_bytes(surface.encode("utf-8"))
        _expect_equal(
            run_clip.get("text_utf8_sha256"),
            surface_sha,
            label=f"clip {ordinal} ASR surface SHA-256",
        )
        _expect_equal(
            decision.get("asr_hypothesis_text_sha256"),
            surface_sha,
            label=f"clip {ordinal} submission ASR surface SHA-256",
        )
        _expect_equal(
            run_clip.get("hypothesis_status"),
            "pending_human_audio_review",
            label=f"clip {ordinal} ASR hypothesis status",
        )

        relative = _safe_relative_path(clip.get("clip_path"), label=f"clip {ordinal} path")
        clip_path = root / "span_review_packages" / package_id / Path(*relative.parts)
        audio_bytes = _read_stable_bytes(root, clip_path, label=f"clip {ordinal} audio")
        _expect_equal(_sha256_bytes(audio_bytes), audio_sha, label=f"clip {ordinal} audio byte SHA-256")
        _expect_equal(
            run_clip.get("audio_byte_count"),
            len(audio_bytes),
            label=f"clip {ordinal} audio byte count",
        )
        start_frame = clip.get("start_frame")
        end_frame = clip.get("end_frame_exclusive")
        if (
            not isinstance(start_frame, int)
            or not isinstance(end_frame, int)
            or end_frame - start_frame != clip.get("frame_count")
        ):
            raise VoiceParalinguisticError(f"clip {ordinal} source range does not match its frame count")
        _expect_equal(
            run_clip.get("source_range"),
            {
                "start_frame": start_frame,
                "end_frame_exclusive": end_frame,
                "sample_rate_hz": clip["audio_format"].get("sample_rate_hz"),
            },
            label=f"clip {ordinal} ASR source range",
        )
        _validate_wav(audio_bytes, clip, run_clip, ordinal=ordinal)

        character_id = _require_string(clip.get("character_id"), label=f"clip {ordinal} character_id")
        character_name = _require_string(clip.get("character_name"), label=f"clip {ordinal} character_name")
        selected_sources = [
            item.get("selected_source")
            for item in queue_characters
            if isinstance(item, dict)
            and item.get("character_id") == character_id
            and item.get("character_name") == character_name
        ]
        if len(selected_sources) != 1 or not isinstance(selected_sources[0], dict):
            raise VoiceParalinguisticError(f"clip {ordinal} character source is not unique in the queue")

        package_format = clip["audio_format"]
        events.append(
            {
                "ordinal": ordinal,
                "span_id": span_id,
                "character": {
                    "character_id": character_id,
                    "character_name": character_name,
                    "selected_source": selected_sources[0],
                },
                "audio": {
                    "package_relative_path": relative.as_posix(),
                    "sha256": audio_sha,
                    "byte_count": len(audio_bytes),
                    "encoding": package_format["encoding"],
                    "sample_rate_hz": package_format["sample_rate_hz"],
                    "channels": package_format["channels"],
                    "sample_width_bytes": package_format["sample_width_bytes"],
                    "frame_count": clip["frame_count"],
                    "duration_seconds": clip["duration_seconds"],
                    "source_range": {
                        "start_frame": clip.get("start_frame"),
                        "end_frame_exclusive": clip.get("end_frame_exclusive"),
                    },
                },
                "observation": {
                    "phonetic_surface": surface,
                    "phonetic_surface_utf8_sha256": surface_sha,
                    "evidence": "reviewer_listening_and_audio_only_asr_agreement",
                },
                "classification": {
                    "kind": "paralinguistic_event",
                    "event_type": "nonlexical_murmur",
                    "retention_disposition": "retained_for_paralinguistic_event_review",
                    "base_tts_training": "excluded",
                    "expressive_training": "pending_human_event_qa",
                    "event_bank_eligibility": "pending_human_event_qa",
                    "delivery_attributes": {
                        "breathiness": "unrated",
                        "emotion": "unrated",
                        "intensity": "unrated",
                    },
                },
            }
        )

    return {
        "root": root,
        "submission": submission,
        "submission_id": submission_id,
        "submission_byte_sha": submission_byte_sha,
        "submission_receipt_sha": submission_receipt_sha,
        "run": run,
        "run_id": run_id,
        "run_byte_sha": run_byte_sha,
        "run_manifest_sha": run_manifest_sha,
        "package": package,
        "package_id": package_id,
        "package_byte_sha": package_byte_sha,
        "package_manifest_sha": package_manifest_sha,
        "queue": queue,
        "queue_byte_sha": queue_byte_sha,
        "events": events,
    }


def build_receipt(
    voice_root: Path,
    submission_id: str,
    *,
    reviewer_id: str,
    recorded_at: str | None = None,
    expected_submission_sha256: str | None = None,
    expected_run_sha256: str | None = None,
    expected_package_sha256: str | None = None,
    expected_queue_sha256: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if not REVIEWER_ID_PATTERN.fullmatch(reviewer_id):
        raise VoiceParalinguisticError("reviewer_id has an invalid format")
    sources = _load_sources(
        voice_root,
        submission_id,
        expected_submission_sha256=expected_submission_sha256,
        expected_run_sha256=expected_run_sha256,
        expected_package_sha256=expected_package_sha256,
        expected_queue_sha256=expected_queue_sha256,
    )
    _expect_equal(sources["submission"].get("reviewer_id"), reviewer_id, label="reviewer identity")

    event_set_sha = _semantic_sha256(sources["events"])
    stable_identity = {
        "classification_profile": CLASSIFICATION_PROFILE,
        "source_submission_id": sources["submission_id"],
        "source_submission_receipt_sha256": sources["submission_receipt_sha"],
        "source_submission_byte_sha256": sources["submission_byte_sha"],
        "source_package_id": sources["package_id"],
        "source_package_manifest_sha256": sources["package_manifest_sha"],
        "reviewer_id": reviewer_id,
        "target_ordinals": list(TARGET_ORDINALS),
        "event_set_sha256": event_set_sha,
    }
    stable_identity_sha = _semantic_sha256(stable_identity)
    review_id = f"voice-paralinguistic-review-{stable_identity_sha[:20]}"
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "review_id": review_id,
        "recorded_at": _parse_recorded_at(recorded_at),
        "artifact_purpose": "immutable_paralinguistic_retention_classification",
        "stable_identity": stable_identity,
        "stable_identity_sha256": stable_identity_sha,
        "source_artifacts": {
            "submission": {
                "submission_id": sources["submission_id"],
                "receipt_sha256": sources["submission_receipt_sha"],
                "byte_sha256": sources["submission_byte_sha"],
            },
            "asr_run": {
                "run_id": sources["run_id"],
                "manifest_sha256": sources["run_manifest_sha"],
                "byte_sha256": sources["run_byte_sha"],
            },
            "review_package": {
                "package_id": sources["package_id"],
                "manifest_sha256": sources["package_manifest_sha"],
                "byte_sha256": sources["package_byte_sha"],
            },
            "span_review_queue": {"byte_sha256": sources["queue_byte_sha"]},
        },
        "reviewer_assertions": {
            "reviewer_id": reviewer_id,
            "target_clips_listened": True,
            "retain_as_paralinguistic_only": True,
            "lexical_transcript_resolution_claimed": False,
        },
        "source_submission_state": {
            "review_status": "needs_clarification",
            "unresolved_ordinals": list(TARGET_ORDINALS),
            "source_submission_modified": False,
        },
        "allowlist": {
            "kinds": sorted(ALLOWED_EVENT_KINDS),
            "event_types": sorted(ALLOWED_EVENT_TYPES),
            "base_tts_training_states": sorted(ALLOWED_BASE_TTS_STATES),
            "pending_states": sorted(ALLOWED_PENDING_STATES),
        },
        "event_set_sha256": event_set_sha,
        "events": sources["events"],
        "scope_limits": {
            "source_submission_modified": False,
            "lexical_transcript_accepted": False,
            "span_approved": False,
            "transcript_alignment_verified": False,
            "base_tts_training_approved": False,
            "expressive_training_approved": False,
            "event_bank_approved": False,
            "concatenation_approved": False,
            "automated_qc_overridden": False,
            "rights_accepted": False,
            "publication_approved": False,
            "provider_enrollment_allowed": False,
        },
    }
    receipt["receipt_sha256"] = _semantic_sha256(receipt)
    destination = sources["root"] / OUTPUT_DIRECTORY / f"{review_id}.json"
    return receipt, destination


def _validate_receipt_shape(receipt: dict[str, Any]) -> None:
    _expect_equal(receipt.get("schema_version"), RECEIPT_SCHEMA, label="receipt schema")
    review_id = _safe_identifier(receipt.get("review_id"), REVIEW_ID_PATTERN, label="review_id")
    _verify_semantic_hash(receipt, field="receipt_sha256", label="paralinguistic receipt")
    stable_identity = receipt.get("stable_identity")
    if not isinstance(stable_identity, dict):
        raise VoiceParalinguisticError("stable_identity must be an object")
    identity_sha = _semantic_sha256(stable_identity)
    _expect_equal(receipt.get("stable_identity_sha256"), identity_sha, label="stable identity SHA-256")
    _expect_equal(review_id, f"voice-paralinguistic-review-{identity_sha[:20]}", label="derived review ID")
    events = receipt.get("events")
    if not isinstance(events, list):
        raise VoiceParalinguisticError("receipt events must be an array")
    _expect_equal(receipt.get("event_set_sha256"), _semantic_sha256(events), label="event set SHA-256")
    _expect_equal(
        stable_identity.get("event_set_sha256"),
        receipt.get("event_set_sha256"),
        label="identity event set SHA-256",
    )
    _expect_equal(stable_identity.get("target_ordinals"), list(TARGET_ORDINALS), label="identity ordinals")
    _require_all_false(receipt.get("scope_limits"), label="receipt scope_limits")
    allowlist = receipt.get("allowlist")
    expected_allowlist = {
        "kinds": sorted(ALLOWED_EVENT_KINDS),
        "event_types": sorted(ALLOWED_EVENT_TYPES),
        "base_tts_training_states": sorted(ALLOWED_BASE_TTS_STATES),
        "pending_states": sorted(ALLOWED_PENDING_STATES),
    }
    _expect_equal(allowlist, expected_allowlist, label="receipt allowlist")
    indexed = _index_by_ordinal(events, label="receipt events")
    _expect_equal(tuple(indexed), TARGET_ORDINALS, label="receipt event ordinals")
    for ordinal, event in indexed.items():
        classification = event.get("classification")
        if not isinstance(classification, dict):
            raise VoiceParalinguisticError(f"receipt event {ordinal} classification must be an object")
        if classification.get("kind") not in ALLOWED_EVENT_KINDS:
            raise VoiceParalinguisticError(f"receipt event {ordinal} kind is not allowlisted")
        if classification.get("event_type") not in ALLOWED_EVENT_TYPES:
            raise VoiceParalinguisticError(f"receipt event {ordinal} event_type is not allowlisted")
        if classification.get("base_tts_training") not in ALLOWED_BASE_TTS_STATES:
            raise VoiceParalinguisticError(f"receipt event {ordinal} base TTS state is not allowlisted")
        for key in ("expressive_training", "event_bank_eligibility"):
            if classification.get(key) not in ALLOWED_PENDING_STATES:
                raise VoiceParalinguisticError(f"receipt event {ordinal} {key} state is not allowlisted")


def validate_receipt(voice_root: Path, review_id: str) -> dict[str, Any]:
    root = _absolute_lexical(voice_root)
    _require_safe_existing_path(root, root, label="voice root", directory=True)
    review_id = _safe_identifier(review_id, REVIEW_ID_PATTERN, label="review_id")
    path = root / OUTPUT_DIRECTORY / f"{review_id}.json"
    receipt, receipt_bytes = _read_json(root, path, label="paralinguistic receipt")
    _validate_receipt_shape(receipt)
    _expect_equal(
        _sha256_bytes(receipt_bytes),
        _sha256_bytes(_pretty_json_bytes(receipt)),
        label="receipt bytes",
    )

    identity = receipt["stable_identity"]
    artifacts = receipt.get("source_artifacts")
    if not isinstance(artifacts, dict):
        raise VoiceParalinguisticError("source_artifacts must be an object")
    submission_anchor = artifacts.get("submission")
    run_anchor = artifacts.get("asr_run")
    package_anchor = artifacts.get("review_package")
    queue_anchor = artifacts.get("span_review_queue")
    if not all(
        isinstance(item, dict)
        for item in (submission_anchor, run_anchor, package_anchor, queue_anchor)
    ):
        raise VoiceParalinguisticError("receipt source artifact anchors are incomplete")

    rebuilt, rebuilt_path = build_receipt(
        root,
        identity.get("source_submission_id"),
        reviewer_id=identity.get("reviewer_id"),
        recorded_at=receipt.get("recorded_at"),
        expected_submission_sha256=submission_anchor.get("byte_sha256"),
        expected_run_sha256=run_anchor.get("byte_sha256"),
        expected_package_sha256=package_anchor.get("byte_sha256"),
        expected_queue_sha256=queue_anchor.get("byte_sha256"),
    )
    _expect_equal(rebuilt_path, path, label="rebuilt receipt path")
    _expect_equal(receipt, rebuilt, label="receipt/source reconstruction")
    return {
        "status": "valid",
        "review_id": review_id,
        "path": str(path),
        "receipt_sha256": receipt["receipt_sha256"],
        "byte_sha256": _sha256_bytes(receipt_bytes),
        "event_count": len(receipt["events"]),
        "target_ordinals": list(TARGET_ORDINALS),
        "all_scope_gates_closed": True,
    }


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_existing_receipt(root: Path, receipt: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    validate_receipt(root, receipt["review_id"])
    path = root / OUTPUT_DIRECTORY / f"{receipt['review_id']}.json"
    existing, _ = _read_json(root, path, label="existing paralinguistic receipt")
    if existing.get("stable_identity") != receipt.get("stable_identity"):
        return "existing_conflict", existing
    return "existing_valid", existing


def write_receipt(
    voice_root: Path, receipt: dict[str, Any], destination: Path
) -> tuple[str, dict[str, Any]]:
    root = _absolute_lexical(voice_root)
    _validate_receipt_shape(receipt)
    output_directory = root / OUTPUT_DIRECTORY
    if output_directory.exists():
        _require_safe_existing_path(root, output_directory, label="receipt output directory", directory=True)
    else:
        try:
            output_directory.mkdir()
        except FileExistsError:
            pass
        _require_safe_existing_path(root, output_directory, label="receipt output directory", directory=True)

    expected_destination = output_directory / f"{receipt['review_id']}.json"
    _expect_equal(_absolute_lexical(destination), expected_destination, label="receipt destination")
    payload = _pretty_json_bytes(receipt)
    if expected_destination.exists():
        return _read_existing_receipt(root, receipt)

    temporary = output_directory / f".{receipt['review_id']}.{uuid.uuid4().hex}.partial"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _require_safe_existing_path(root, temporary, label="temporary receipt", directory=False)
        try:
            os.link(temporary, expected_destination)
        except FileExistsError:
            return _read_existing_receipt(root, receipt)
        validation = validate_receipt(root, receipt["review_id"])
        _expect_equal(
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


def _record_summary(
    receipt: dict[str, Any],
    destination: Path,
    *,
    mode: str,
    write_status: str | None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": mode,
        "write_status": write_status,
        "review_id": receipt["review_id"],
        "path": str(destination),
        "receipt_sha256": receipt["receipt_sha256"],
        "event_set_sha256": receipt["event_set_sha256"],
        "events": [
            {
                "ordinal": event["ordinal"],
                "span_id": event["span_id"],
                "phonetic_surface": event["observation"]["phonetic_surface"],
                "event_type": event["classification"]["event_type"],
                "base_tts_training": event["classification"]["base_tts_training"],
                "expressive_training": event["classification"]["expressive_training"],
                "event_bank_eligibility": event["classification"]["event_bank_eligibility"],
            }
            for event in receipt["events"]
        ],
        "all_scope_gates_closed": all(state is False for state in receipt["scope_limits"].values()),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="dry-run or append the exact 2/3 classification receipt")
    record.add_argument("--voice-root", type=Path, required=True)
    record.add_argument("--submission-id", required=True)
    record.add_argument("--reviewer-id", default="xiaob")
    record.add_argument("--recorded-at")
    record.add_argument("--expect-submission-sha256")
    record.add_argument("--expect-run-sha256")
    record.add_argument("--expect-package-sha256")
    record.add_argument("--expect-queue-sha256")
    record.add_argument("--confirm-retain-as-paralinguistic-only", action="store_true")
    record.add_argument("--execute", action="store_true")

    validate = subparsers.add_parser("validate", help="validate a receipt and every pinned source artifact")
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
            result = validate_receipt(arguments.voice_root, arguments.review_id)
        else:
            if arguments.execute and not arguments.confirm_retain_as_paralinguistic_only:
                raise VoiceParalinguisticError(
                    "--execute requires --confirm-retain-as-paralinguistic-only"
                )
            receipt, destination = build_receipt(
                arguments.voice_root,
                arguments.submission_id,
                reviewer_id=arguments.reviewer_id,
                recorded_at=arguments.recorded_at,
                expected_submission_sha256=arguments.expect_submission_sha256,
                expected_run_sha256=arguments.expect_run_sha256,
                expected_package_sha256=arguments.expect_package_sha256,
                expected_queue_sha256=arguments.expect_queue_sha256,
            )
            write_status = None
            if arguments.execute:
                write_status, receipt = write_receipt(arguments.voice_root, receipt, destination)
                if write_status == "existing_conflict":
                    raise VoiceParalinguisticError(
                        "an immutable receipt already exists with conflicting content"
                    )
            result = _record_summary(
                receipt,
                destination,
                mode="execute" if arguments.execute else "dry_run",
                write_status=write_status,
            )
    except (OSError, VoiceParalinguisticError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
