"""Build an immutable candidate-only routing snapshot for the 15 reviewed spans."""

from __future__ import annotations

import argparse
import hashlib
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


ROUTING_SCHEMA = "project-snow-private-voice-corpus-routing-1"
ROUTING_PROFILE = "project-snow-reviewed-span-routing-profile-1"
OUTPUT_DIRECTORY = "tts_corpus_routing"
ROUTING_ID_PATTERN = re.compile(r"voice-corpus-routing-[0-9a-f]{20}\Z")

EXPECTED_ORDINALS = tuple(range(1, 16))
LEXICAL_ORDINALS = (1, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15)
PARALINGUISTIC_ORDINALS = (2, 3)
DUPLICATE_ORDINAL = 12
DUPLICATE_OF_ORDINAL = 11

TRACK_LEXICAL = "lexical_base_candidate"
TRACK_PARALINGUISTIC = "paralinguistic_event_candidate"
TRACK_DUPLICATE = "excluded_duplicate"


class VoiceCorpusRoutingError(base.VoiceParalinguisticError):
    """Raised when the exact 12/2/1 routing contract is not satisfied."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceCorpusRoutingError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_paralinguistic_receipt(
    root: Path,
    review_id: str,
    *,
    expected_byte_sha256: str | None,
) -> tuple[dict[str, Any], str, str]:
    review_id = base._safe_identifier(
        review_id, base.REVIEW_ID_PATTERN, label="paralinguistic_review_id"
    )
    base.validate_receipt(root, review_id)
    path = root / base.OUTPUT_DIRECTORY / f"{review_id}.json"
    receipt, payload = base._read_json(root, path, label="paralinguistic receipt")
    byte_sha = base._verify_expected_byte_hash(
        payload,
        expected_byte_sha256,
        label="paralinguistic receipt byte SHA-256",
    )
    semantic_sha = base._verify_semantic_hash(
        receipt,
        field="receipt_sha256",
        label="paralinguistic receipt",
    )
    return receipt, byte_sha, semantic_sha


def _selected_source(
    queue: dict[str, Any], character_id: str, character_name: str, *, ordinal: int
) -> dict[str, Any]:
    characters = queue.get("characters")
    if not isinstance(characters, list):
        raise VoiceCorpusRoutingError("queue characters must be an array")
    matches = [
        item.get("selected_source")
        for item in characters
        if isinstance(item, dict)
        and item.get("character_id") == character_id
        and item.get("character_name") == character_name
    ]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise VoiceCorpusRoutingError(
            f"clip {ordinal} character source is not unique in the queue"
        )
    return matches[0]


def _validate_audio(
    sources: dict[str, Any],
    clip: dict[str, Any],
    run_clip: dict[str, Any],
    decision: dict[str, Any],
    *,
    ordinal: int,
) -> dict[str, Any]:
    span_id = base._require_string(
        clip.get("span_id"), label=f"clip {ordinal} span_id"
    )
    _expect(run_clip.get("span_id"), span_id, label=f"clip {ordinal} ASR span_id")
    _expect(
        decision.get("span_id"),
        span_id,
        label=f"clip {ordinal} submission span_id",
    )
    audio_sha = base._require_sha256(
        clip.get("output_wav_sha256"), label=f"clip {ordinal} audio SHA-256"
    )
    _expect(
        run_clip.get("audio_sha256"),
        audio_sha,
        label=f"clip {ordinal} ASR audio SHA-256",
    )
    _expect(
        decision.get("audio_sha256"),
        audio_sha,
        label=f"clip {ordinal} submission audio SHA-256",
    )

    relative = base._safe_relative_path(
        clip.get("clip_path"), label=f"clip {ordinal} path"
    )
    path = (
        sources["root"]
        / "span_review_packages"
        / sources["package_id"]
        / Path(*relative.parts)
    )
    payload = base._read_stable_bytes(
        sources["root"], path, label=f"clip {ordinal} audio"
    )
    _expect(
        hashlib.sha256(payload).hexdigest(),
        audio_sha,
        label=f"clip {ordinal} audio byte SHA-256",
    )
    _expect(
        run_clip.get("audio_byte_count"),
        len(payload),
        label=f"clip {ordinal} audio byte count",
    )
    start_frame = clip.get("start_frame")
    end_frame = clip.get("end_frame_exclusive")
    frame_count = clip.get("frame_count")
    if (
        not isinstance(start_frame, int)
        or isinstance(start_frame, bool)
        or not isinstance(end_frame, int)
        or isinstance(end_frame, bool)
        or not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or end_frame - start_frame != frame_count
    ):
        raise VoiceCorpusRoutingError(
            f"clip {ordinal} source range does not match its frame count"
        )
    audio_format = clip.get("audio_format")
    if not isinstance(audio_format, dict):
        raise VoiceCorpusRoutingError(f"clip {ordinal} audio_format must be an object")
    _expect(
        run_clip.get("source_range"),
        {
            "start_frame": start_frame,
            "end_frame_exclusive": end_frame,
            "sample_rate_hz": audio_format.get("sample_rate_hz"),
        },
        label=f"clip {ordinal} ASR source range",
    )
    base._validate_wav(payload, clip, run_clip, ordinal=ordinal)

    character_id = base._require_string(
        clip.get("character_id"), label=f"clip {ordinal} character_id"
    )
    character_name = base._require_string(
        clip.get("character_name"), label=f"clip {ordinal} character_name"
    )
    return {
        "ordinal": ordinal,
        "span_id": span_id,
        "character": {
            "character_id": character_id,
            "character_name": character_name,
            "selected_source": _selected_source(
                sources["queue"], character_id, character_name, ordinal=ordinal
            ),
        },
        "audio": {
            "package_relative_path": relative.as_posix(),
            "sha256": audio_sha,
            "byte_count": len(payload),
            "encoding": audio_format.get("encoding"),
            "sample_rate_hz": audio_format.get("sample_rate_hz"),
            "channels": audio_format.get("channels"),
            "sample_width_bytes": audio_format.get("sample_width_bytes"),
            "frame_count": frame_count,
            "duration_seconds": clip.get("duration_seconds"),
            "source_range": {
                "start_frame": start_frame,
                "end_frame_exclusive": end_frame,
            },
        },
    }


def _reviewed_transcript(
    decision: dict[str, Any], run_clip: dict[str, Any], *, ordinal: int
) -> dict[str, Any]:
    review = decision.get("transcript_review")
    if not isinstance(review, dict):
        raise VoiceCorpusRoutingError(
            f"clip {ordinal} transcript_review must be an object"
        )
    review_decision = review.get("decision")
    if review_decision not in {"accepted_exact", "corrected_from_audio"}:
        raise VoiceCorpusRoutingError(
            f"clip {ordinal} does not have a resolved lexical transcript"
        )
    text = base._require_string(
        review.get("reviewed_text"), label=f"clip {ordinal} reviewed text"
    )
    digest = _text_sha(text)
    _expect(
        review.get("reviewed_text_utf8_sha256"),
        digest,
        label=f"clip {ordinal} reviewed text SHA-256",
    )
    if review_decision == "accepted_exact":
        _expect(
            run_clip.get("text"), text, label=f"clip {ordinal} accepted ASR text"
        )
        _expect(
            run_clip.get("text_utf8_sha256"),
            digest,
            label=f"clip {ordinal} accepted ASR text SHA-256",
        )
    return {
        "decision": review_decision,
        "text": text,
        "text_utf8_sha256": digest,
        "source": "human_audio_review",
    }


def _validate_asr_surface(run_clip: dict[str, Any], *, ordinal: int) -> None:
    text = base._require_string(
        run_clip.get("text"), label=f"clip {ordinal} ASR surface"
    )
    _expect(
        run_clip.get("text_utf8_sha256"),
        _text_sha(text),
        label=f"clip {ordinal} ASR surface SHA-256",
    )


def _build_routes(
    sources: dict[str, Any], paralinguistic_receipt: dict[str, Any]
) -> list[dict[str, Any]]:
    package_clips = base._index_by_ordinal(
        sources["package"].get("clips"), label="package clips"
    )
    run_clips = base._index_by_ordinal(
        sources["run"].get("clips"), label="ASR clips"
    )
    decisions = base._index_by_ordinal(
        sources["submission"].get("decisions"), label="submission decisions"
    )
    _expect(tuple(sorted(package_clips)), EXPECTED_ORDINALS, label="package ordinals")
    _expect(tuple(sorted(run_clips)), EXPECTED_ORDINALS, label="ASR ordinals")
    _expect(tuple(sorted(decisions)), EXPECTED_ORDINALS, label="submission ordinals")
    event_reviews = base._index_by_ordinal(
        paralinguistic_receipt.get("events"), label="paralinguistic events"
    )
    _expect(
        tuple(sorted(event_reviews)),
        PARALINGUISTIC_ORDINALS,
        label="paralinguistic ordinals",
    )

    routes: list[dict[str, Any]] = []
    lexical_text_hashes: dict[str, int] = {}
    duplicate_text: tuple[int, str] | None = None
    for ordinal in EXPECTED_ORDINALS:
        clip = package_clips[ordinal]
        run_clip = run_clips[ordinal]
        decision = decisions[ordinal]
        _validate_asr_surface(run_clip, ordinal=ordinal)
        route = _validate_audio(
            sources, clip, run_clip, decision, ordinal=ordinal
        )
        training_use = decision.get("training_use")
        if not isinstance(training_use, dict):
            raise VoiceCorpusRoutingError(
                f"clip {ordinal} training_use must be an object"
            )

        if ordinal in LEXICAL_ORDINALS:
            _expect(
                training_use.get("disposition"),
                "not_assessed",
                label=f"clip {ordinal} training disposition",
            )
            transcript = _reviewed_transcript(
                decision, run_clip, ordinal=ordinal
            )
            prior = lexical_text_hashes.get(transcript["text_utf8_sha256"])
            if prior is not None:
                raise VoiceCorpusRoutingError(
                    f"lexical candidates {prior} and {ordinal} duplicate reviewed text"
                )
            lexical_text_hashes[transcript["text_utf8_sha256"]] = ordinal
            route["transcript"] = transcript
            route["routing"] = {
                "track": TRACK_LEXICAL,
                "status": "candidate_only",
                "base_tts_training": "pending_dataset_qc_and_rights",
            }
        elif ordinal in PARALINGUISTIC_ORDINALS:
            event = event_reviews[ordinal]
            _expect(
                event.get("span_id"),
                route["span_id"],
                label=f"clip {ordinal} event span_id",
            )
            event_audio = event.get("audio")
            observation = event.get("observation")
            classification = event.get("classification")
            if not all(
                isinstance(item, dict)
                for item in (event_audio, observation, classification)
            ):
                raise VoiceCorpusRoutingError(
                    f"clip {ordinal} paralinguistic event is incomplete"
                )
            _expect(
                event_audio.get("sha256"),
                route["audio"]["sha256"],
                label=f"clip {ordinal} event audio SHA-256",
            )
            _expect(
                observation.get("phonetic_surface"),
                run_clip.get("text"),
                label=f"clip {ordinal} event surface",
            )
            route["paralinguistic_event"] = {
                "review_id": paralinguistic_receipt["review_id"],
                "event_type": classification.get("event_type"),
                "phonetic_surface": observation.get("phonetic_surface"),
                "phonetic_surface_utf8_sha256": observation.get(
                    "phonetic_surface_utf8_sha256"
                ),
            }
            route["routing"] = {
                "track": TRACK_PARALINGUISTIC,
                "status": "retained_pending_human_event_qa",
                "base_tts_training": "excluded",
            }
        elif ordinal == DUPLICATE_ORDINAL:
            _expect(
                training_use.get("disposition"),
                "excluded_from_current_training_set",
                label="duplicate training disposition",
            )
            _expect(
                training_use.get("reason_code"),
                "duplicate_utterance_text",
                label="duplicate reason",
            )
            _expect(
                training_use.get("duplicate_of_ordinal"),
                DUPLICATE_OF_ORDINAL,
                label="duplicate target ordinal",
            )
            transcript = _reviewed_transcript(
                decision, run_clip, ordinal=ordinal
            )
            duplicate_text = (
                DUPLICATE_OF_ORDINAL,
                transcript["text_utf8_sha256"],
            )
            route["transcript"] = transcript
            route["routing"] = {
                "track": TRACK_DUPLICATE,
                "status": "excluded",
                "reason_code": "duplicate_utterance_text",
                "duplicate_of_ordinal": DUPLICATE_OF_ORDINAL,
                "base_tts_training": "excluded",
            }
        else:
            raise VoiceCorpusRoutingError(f"clip {ordinal} has no routing rule")
        routes.append(route)

    if duplicate_text is None:
        raise VoiceCorpusRoutingError("the duplicate route is missing")
    target_ordinal, duplicate_hash = duplicate_text
    target_route = routes[target_ordinal - 1]
    _expect(
        target_route["transcript"]["text_utf8_sha256"],
        duplicate_hash,
        label="duplicate/target reviewed text SHA-256",
    )
    return routes


def _routing_summary(routes: list[dict[str, Any]]) -> dict[str, Any]:
    track_counts = {
        TRACK_LEXICAL: 0,
        TRACK_PARALINGUISTIC: 0,
        TRACK_DUPLICATE: 0,
    }
    track_durations = {key: 0.0 for key in track_counts}
    characters: dict[str, dict[str, Any]] = {}
    for route in routes:
        track = route["routing"]["track"]
        track_counts[track] += 1
        track_durations[track] += float(route["audio"]["duration_seconds"])
        character = route["character"]
        character_id = character["character_id"]
        summary = characters.setdefault(
            character_id,
            {
                "character_id": character_id,
                "character_name": character["character_name"],
                "lexical_candidate_count": 0,
                "paralinguistic_candidate_count": 0,
                "duplicate_excluded_count": 0,
            },
        )
        if track == TRACK_LEXICAL:
            summary["lexical_candidate_count"] += 1
        elif track == TRACK_PARALINGUISTIC:
            summary["paralinguistic_candidate_count"] += 1
        else:
            summary["duplicate_excluded_count"] += 1
    return {
        "source_span_count": len(routes),
        "transcript_resolved_count": len(LEXICAL_ORDINALS) + 1,
        "lexical_candidate_count": track_counts[TRACK_LEXICAL],
        "paralinguistic_candidate_count": track_counts[TRACK_PARALINGUISTIC],
        "duplicate_excluded_count": track_counts[TRACK_DUPLICATE],
        "duration_seconds_by_track": {
            key: round(value, 6) for key, value in track_durations.items()
        },
        "characters": list(characters.values()),
    }


def build_routing_receipt(
    voice_root: Path,
    submission_id: str,
    paralinguistic_review_id: str,
    *,
    reviewer_id: str,
    recorded_at: str | None = None,
    expected_submission_sha256: str | None = None,
    expected_run_sha256: str | None = None,
    expected_package_sha256: str | None = None,
    expected_queue_sha256: str | None = None,
    expected_paralinguistic_sha256: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if not base.REVIEWER_ID_PATTERN.fullmatch(reviewer_id):
        raise VoiceCorpusRoutingError("reviewer_id has an invalid format")
    sources = base._load_sources(
        voice_root,
        submission_id,
        expected_submission_sha256=expected_submission_sha256,
        expected_run_sha256=expected_run_sha256,
        expected_package_sha256=expected_package_sha256,
        expected_queue_sha256=expected_queue_sha256,
    )
    _expect(
        sources["submission"].get("reviewer_id"),
        reviewer_id,
        label="reviewer identity",
    )
    para_receipt, para_byte_sha, para_receipt_sha = _load_paralinguistic_receipt(
        sources["root"],
        paralinguistic_review_id,
        expected_byte_sha256=expected_paralinguistic_sha256,
    )
    para_identity = para_receipt.get("stable_identity")
    if not isinstance(para_identity, dict):
        raise VoiceCorpusRoutingError(
            "paralinguistic stable_identity must be an object"
        )
    _expect(
        para_identity.get("source_submission_id"),
        sources["submission_id"],
        label="paralinguistic source submission",
    )
    _expect(
        para_identity.get("reviewer_id"),
        reviewer_id,
        label="paralinguistic reviewer",
    )

    routes = _build_routes(sources, para_receipt)
    route_set_sha = base._semantic_sha256(routes)
    stable_identity = {
        "routing_profile": ROUTING_PROFILE,
        "source_submission_id": sources["submission_id"],
        "source_submission_receipt_sha256": sources["submission_receipt_sha"],
        "source_submission_byte_sha256": sources["submission_byte_sha"],
        "source_package_id": sources["package_id"],
        "source_package_manifest_sha256": sources["package_manifest_sha"],
        "paralinguistic_review_id": para_receipt["review_id"],
        "paralinguistic_receipt_sha256": para_receipt_sha,
        "reviewer_id": reviewer_id,
        "route_set_sha256": route_set_sha,
    }
    identity_sha = base._semantic_sha256(stable_identity)
    routing_id = f"voice-corpus-routing-{identity_sha[:20]}"
    receipt: dict[str, Any] = {
        "schema_version": ROUTING_SCHEMA,
        "routing_id": routing_id,
        "recorded_at": base._parse_recorded_at(recorded_at),
        "artifact_purpose": "immutable_candidate_only_voice_corpus_routing",
        "routing_status": "complete_as_candidate_only",
        "stable_identity": stable_identity,
        "stable_identity_sha256": identity_sha,
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
            "paralinguistic_review": {
                "review_id": para_receipt["review_id"],
                "receipt_sha256": para_receipt_sha,
                "byte_sha256": para_byte_sha,
            },
        },
        "routing_rules": {
            "lexical_ordinals": list(LEXICAL_ORDINALS),
            "paralinguistic_ordinals": list(PARALINGUISTIC_ORDINALS),
            "duplicate_ordinal": DUPLICATE_ORDINAL,
            "duplicate_of_ordinal": DUPLICATE_OF_ORDINAL,
            "candidate_does_not_mean_training_approved": True,
        },
        "route_set_sha256": route_set_sha,
        "routes": routes,
        "summary": _routing_summary(routes),
        "scope_limits": {
            "source_artifacts_modified": False,
            "dataset_qc_completed": False,
            "training_use_approved": False,
            "voice_cloning_approved": False,
            "expressive_training_approved": False,
            "event_bank_approved": False,
            "concatenation_approved": False,
            "ab_winner_selected": False,
            "rights_accepted": False,
            "publication_approved": False,
            "provider_enrollment_allowed": False,
            "public_rollout_allowed": False,
        },
    }
    receipt["receipt_sha256"] = base._semantic_sha256(receipt)
    destination = sources["root"] / OUTPUT_DIRECTORY / f"{routing_id}.json"
    return receipt, destination


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _validate_shape(receipt: dict[str, Any]) -> None:
    _expect(receipt.get("schema_version"), ROUTING_SCHEMA, label="routing schema")
    routing_id = base._safe_identifier(
        receipt.get("routing_id"), ROUTING_ID_PATTERN, label="routing_id"
    )
    base._verify_semantic_hash(
        receipt, field="receipt_sha256", label="corpus routing receipt"
    )
    identity = receipt.get("stable_identity")
    if not isinstance(identity, dict):
        raise VoiceCorpusRoutingError("stable_identity must be an object")
    identity_sha = base._semantic_sha256(identity)
    _expect(
        receipt.get("stable_identity_sha256"),
        identity_sha,
        label="stable identity SHA-256",
    )
    _expect(
        routing_id,
        f"voice-corpus-routing-{identity_sha[:20]}",
        label="derived routing ID",
    )
    routes = receipt.get("routes")
    if not isinstance(routes, list):
        raise VoiceCorpusRoutingError("routes must be an array")
    _expect(
        receipt.get("route_set_sha256"),
        base._semantic_sha256(routes),
        label="route set SHA-256",
    )
    _expect(
        identity.get("route_set_sha256"),
        receipt.get("route_set_sha256"),
        label="identity route set SHA-256",
    )
    indexed = base._index_by_ordinal(routes, label="routing routes")
    _expect(tuple(indexed), EXPECTED_ORDINALS, label="route ordinals")
    tracks = [route.get("routing", {}).get("track") for route in routes]
    _expect(tracks.count(TRACK_LEXICAL), 12, label="lexical route count")
    _expect(
        tracks.count(TRACK_PARALINGUISTIC),
        2,
        label="paralinguistic route count",
    )
    _expect(tracks.count(TRACK_DUPLICATE), 1, label="duplicate route count")
    base._require_all_false(
        receipt.get("scope_limits"), label="routing scope_limits"
    )
    _expect(
        receipt.get("summary"),
        _routing_summary(routes),
        label="routing summary",
    )


def validate_routing_receipt(voice_root: Path, routing_id: str) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(
        root, root, label="voice root", directory=True
    )
    routing_id = base._safe_identifier(
        routing_id, ROUTING_ID_PATTERN, label="routing_id"
    )
    path = root / OUTPUT_DIRECTORY / f"{routing_id}.json"
    receipt, payload = base._read_json(root, path, label="corpus routing receipt")
    _validate_shape(receipt)
    _expect(
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(_pretty_bytes(receipt)).hexdigest(),
        label="routing receipt bytes",
    )
    identity = receipt["stable_identity"]
    artifacts = receipt.get("source_artifacts")
    if not isinstance(artifacts, dict):
        raise VoiceCorpusRoutingError("source_artifacts must be an object")
    required = (
        "submission",
        "asr_run",
        "review_package",
        "span_review_queue",
        "paralinguistic_review",
    )
    if not all(isinstance(artifacts.get(key), dict) for key in required):
        raise VoiceCorpusRoutingError("source artifact anchors are incomplete")
    rebuilt, rebuilt_path = build_routing_receipt(
        root,
        identity.get("source_submission_id"),
        identity.get("paralinguistic_review_id"),
        reviewer_id=identity.get("reviewer_id"),
        recorded_at=receipt.get("recorded_at"),
        expected_submission_sha256=artifacts["submission"].get("byte_sha256"),
        expected_run_sha256=artifacts["asr_run"].get("byte_sha256"),
        expected_package_sha256=artifacts["review_package"].get("byte_sha256"),
        expected_queue_sha256=artifacts["span_review_queue"].get("byte_sha256"),
        expected_paralinguistic_sha256=artifacts["paralinguistic_review"].get(
            "byte_sha256"
        ),
    )
    _expect(rebuilt_path, path, label="rebuilt routing path")
    _expect(receipt, rebuilt, label="routing/source reconstruction")
    return {
        "status": "valid",
        "routing_id": routing_id,
        "path": str(path),
        "receipt_sha256": receipt["receipt_sha256"],
        "byte_sha256": hashlib.sha256(payload).hexdigest(),
        "summary": receipt["summary"],
        "all_scope_gates_closed": True,
    }


def _existing_receipt(
    root: Path, desired: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    validate_routing_receipt(root, desired["routing_id"])
    path = root / OUTPUT_DIRECTORY / f"{desired['routing_id']}.json"
    existing, _ = base._read_json(root, path, label="existing routing receipt")
    if existing.get("stable_identity") != desired.get("stable_identity"):
        return "existing_conflict", existing
    return "existing_valid", existing


def write_routing_receipt(
    voice_root: Path, receipt: dict[str, Any], destination: Path
) -> tuple[str, dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    _validate_shape(receipt)
    output = root / OUTPUT_DIRECTORY
    if output.exists():
        base._require_safe_existing_path(
            root, output, label="routing output directory", directory=True
        )
    else:
        try:
            output.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(
            root, output, label="routing output directory", directory=True
        )
    expected = output / f"{receipt['routing_id']}.json"
    _expect(base._absolute_lexical(destination), expected, label="routing destination")
    if expected.exists():
        return _existing_receipt(root, receipt)

    temporary = output / f".{receipt['routing_id']}.{uuid.uuid4().hex}.partial"
    try:
        with temporary.open("xb") as stream:
            stream.write(_pretty_bytes(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        base._require_safe_existing_path(
            root, temporary, label="temporary routing receipt", directory=False
        )
        try:
            os.link(temporary, expected)
        except FileExistsError:
            return _existing_receipt(root, receipt)
        validation = validate_routing_receipt(root, receipt["routing_id"])
        _expect(
            validation["receipt_sha256"],
            receipt["receipt_sha256"],
            label="written routing receipt SHA-256",
        )
        return "created", receipt
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record", help="dry-run or append 12/2/1 routing")
    record.add_argument("--voice-root", type=Path, required=True)
    record.add_argument("--submission-id", required=True)
    record.add_argument("--paralinguistic-review-id", required=True)
    record.add_argument("--reviewer-id", default="xiaob")
    record.add_argument("--recorded-at")
    record.add_argument("--expect-submission-sha256")
    record.add_argument("--expect-run-sha256")
    record.add_argument("--expect-package-sha256")
    record.add_argument("--expect-queue-sha256")
    record.add_argument("--expect-paralinguistic-sha256")
    record.add_argument("--confirm-candidate-routing-only", action="store_true")
    record.add_argument("--execute", action="store_true")
    validate = subparsers.add_parser("validate", help="validate routing and sources")
    validate.add_argument("--voice-root", type=Path, required=True)
    validate.add_argument("--routing-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            result = validate_routing_receipt(
                arguments.voice_root, arguments.routing_id
            )
        else:
            if arguments.execute and not arguments.confirm_candidate_routing_only:
                raise VoiceCorpusRoutingError(
                    "--execute requires --confirm-candidate-routing-only"
                )
            receipt, destination = build_routing_receipt(
                arguments.voice_root,
                arguments.submission_id,
                arguments.paralinguistic_review_id,
                reviewer_id=arguments.reviewer_id,
                recorded_at=arguments.recorded_at,
                expected_submission_sha256=arguments.expect_submission_sha256,
                expected_run_sha256=arguments.expect_run_sha256,
                expected_package_sha256=arguments.expect_package_sha256,
                expected_queue_sha256=arguments.expect_queue_sha256,
                expected_paralinguistic_sha256=(
                    arguments.expect_paralinguistic_sha256
                ),
            )
            write_status = None
            if arguments.execute:
                write_status, receipt = write_routing_receipt(
                    arguments.voice_root, receipt, destination
                )
                if write_status == "existing_conflict":
                    raise VoiceCorpusRoutingError(
                        "an immutable routing receipt already conflicts"
                    )
            result = {
                "status": "ok",
                "mode": "execute" if arguments.execute else "dry_run",
                "write_status": write_status,
                "routing_id": receipt["routing_id"],
                "path": str(destination),
                "receipt_sha256": receipt["receipt_sha256"],
                "route_set_sha256": receipt["route_set_sha256"],
                "summary": receipt["summary"],
                "all_scope_gates_closed": all(
                    value is False for value in receipt["scope_limits"].values()
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
