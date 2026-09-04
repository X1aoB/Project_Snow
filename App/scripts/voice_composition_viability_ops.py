"""Audit post-routing A/B duration viability without composing or approving audio."""

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
    from . import voice_corpus_routing_ops as routing
    from . import voice_paralinguistic_ops as base
else:
    import voice_corpus_routing_ops as routing
    import voice_paralinguistic_ops as base


SCHEMA = "project-snow-private-voice-composition-viability-1"
PROFILE = "project-snow-post-routing-ab-duration-viability-1"
OUTPUT_DIRECTORY = "tts_composition_viability"
VIABILITY_ID_PATTERN = re.compile(r"voice-composition-viability-[0-9a-f]{20}\Z")
POLICY_RELATIVE_PATH = Path(
    "tts_corpus_status/2026-08-31-public-four-fragment-feasibility.md"
)

MINIMUM_DURATION_SECONDS = 10.0
MAXIMUM_DURATION_SECONDS = 20.0
MINIMUM_COMPONENT_COUNT = 1
MAXIMUM_COMPONENT_COUNT = 4
EXPECTED_JOIN_SILENCE_MS = 150
REQUIRED_POLICY_LINES = (
    "- 每份样本 10–20 秒。",
    "- 每份样本 1–4 个互不重复的父片段。",
    "- A/B 不得共享父 candidate、音频哈希或文本哈希。",
    "- 静音占比不高于 20%。",
    "- SNR 不低于 25 dB。",
    "- true peak 不高于 -1 dBTP，削波样本数为 0。",
)


class VoiceCompositionViabilityError(base.VoiceParalinguisticError):
    """Raised when fixed routing cannot support a trustworthy viability audit."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceCompositionViabilityError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _expect_float(actual: Any, expected: float, *, label: str) -> None:
    if (
        not isinstance(actual, (int, float))
        or isinstance(actual, bool)
        or abs(float(actual) - expected) > 1e-6
    ):
        raise VoiceCompositionViabilityError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _load_inputs(
    voice_root: Path,
    routing_id: str,
    *,
    expected_routing_sha256: str | None,
    expected_package_sha256: str | None,
    expected_queue_sha256: str | None,
    expected_policy_sha256: str | None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(
        root, root, label="voice root", directory=True
    )
    routing_id = base._safe_identifier(
        routing_id, routing.ROUTING_ID_PATTERN, label="routing_id"
    )
    routing.validate_routing_receipt(root, routing_id)
    routing_path = root / routing.OUTPUT_DIRECTORY / f"{routing_id}.json"
    routing_receipt, routing_bytes = base._read_json(
        root, routing_path, label="corpus routing receipt"
    )
    routing_byte_sha = base._verify_expected_byte_hash(
        routing_bytes,
        expected_routing_sha256,
        label="routing receipt byte SHA-256",
    )
    routing_receipt_sha = base._verify_semantic_hash(
        routing_receipt,
        field="receipt_sha256",
        label="corpus routing receipt",
    )
    artifacts = routing_receipt.get("source_artifacts")
    if not isinstance(artifacts, dict):
        raise VoiceCompositionViabilityError(
            "routing source_artifacts must be an object"
        )
    package_anchor = artifacts.get("review_package")
    queue_anchor = artifacts.get("span_review_queue")
    if not isinstance(package_anchor, dict) or not isinstance(queue_anchor, dict):
        raise VoiceCompositionViabilityError(
            "routing package and queue anchors are required"
        )

    package_id = base._safe_identifier(
        package_anchor.get("package_id"),
        base.PACKAGE_ID_PATTERN,
        label="package_id",
    )
    package_path = root / "span_review_packages" / package_id / "manifest.json"
    package, package_bytes = base._read_json(
        root, package_path, label="review package manifest"
    )
    package_byte_sha = base._verify_expected_byte_hash(
        package_bytes,
        expected_package_sha256,
        label="package manifest byte SHA-256",
    )
    package_manifest_sha = base._verify_semantic_hash(
        package, field="manifest_sha256", label="review package"
    )
    _expect(
        package_anchor.get("manifest_sha256"),
        package_manifest_sha,
        label="routing package manifest SHA-256",
    )
    _expect(
        package_anchor.get("byte_sha256"),
        package_byte_sha,
        label="routing package byte SHA-256",
    )

    queue_path = root / "operator_span_review_queue.json"
    queue, queue_bytes = base._read_json(
        root, queue_path, label="span review queue"
    )
    queue_byte_sha = base._verify_expected_byte_hash(
        queue_bytes, expected_queue_sha256, label="queue byte SHA-256"
    )
    _expect(
        queue_anchor.get("byte_sha256"),
        queue_byte_sha,
        label="routing queue byte SHA-256",
    )
    prediction = queue.get("prediction_method")
    if not isinstance(prediction, dict):
        raise VoiceCompositionViabilityError(
            "queue prediction_method must be an object"
        )
    _expect(
        prediction.get("join_silence_ms"),
        EXPECTED_JOIN_SILENCE_MS,
        label="join silence milliseconds",
    )

    policy_path = root / POLICY_RELATIVE_PATH
    policy_bytes = base._read_stable_bytes(
        root, policy_path, label="fixed composition policy report"
    )
    policy_byte_sha = base._verify_expected_byte_hash(
        policy_bytes, expected_policy_sha256, label="policy report byte SHA-256"
    )
    try:
        policy_text = policy_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VoiceCompositionViabilityError(
            "fixed composition policy report is not UTF-8"
        ) from error
    missing = [line for line in REQUIRED_POLICY_LINES if line not in policy_text]
    if missing:
        raise VoiceCompositionViabilityError(
            f"fixed composition policy report is missing rules: {missing!r}"
        )
    return {
        "root": root,
        "routing": routing_receipt,
        "routing_id": routing_id,
        "routing_byte_sha": routing_byte_sha,
        "routing_receipt_sha": routing_receipt_sha,
        "package": package,
        "package_id": package_id,
        "package_byte_sha": package_byte_sha,
        "package_manifest_sha": package_manifest_sha,
        "queue": queue,
        "queue_byte_sha": queue_byte_sha,
        "policy_byte_sha": policy_byte_sha,
    }


def _proposal_index(queue: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    characters = queue.get("characters")
    if not isinstance(characters, list):
        raise VoiceCompositionViabilityError("queue characters must be an array")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for character in characters:
        if not isinstance(character, dict):
            raise VoiceCompositionViabilityError(
                "queue character entries must be objects"
            )
        character_id = base._require_string(
            character.get("character_id"), label="queue character_id"
        )
        proposals = character.get("proposals")
        if not isinstance(proposals, list):
            raise VoiceCompositionViabilityError(
                f"queue character {character_id} proposals must be an array"
            )
        for proposal in proposals:
            if not isinstance(proposal, dict):
                raise VoiceCompositionViabilityError(
                    f"queue character {character_id} proposal must be an object"
                )
            slot = proposal.get("slot")
            if slot not in {"A", "B"}:
                raise VoiceCompositionViabilityError(
                    f"queue character {character_id} has invalid slot"
                )
            key = (character_id, slot)
            if key in result:
                raise VoiceCompositionViabilityError(
                    f"queue contains duplicate proposal {key!r}"
                )
            result[key] = proposal
    return result


def _duration_with_joins(routes: list[dict[str, Any]], join_seconds: float) -> float:
    if not routes:
        return 0.0
    return round(
        sum(float(route["audio"]["duration_seconds"]) for route in routes)
        + join_seconds * (len(routes) - 1),
        6,
    )


def _build_viability(
    routing_receipt: dict[str, Any],
    package: dict[str, Any],
    queue: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    routes = base._index_by_ordinal(
        routing_receipt.get("routes"), label="routing routes"
    )
    clips = base._index_by_ordinal(package.get("clips"), label="package clips")
    _expect(tuple(routes), routing.EXPECTED_ORDINALS, label="routing ordinals")
    _expect(tuple(clips), routing.EXPECTED_ORDINALS, label="package ordinals")
    proposals = _proposal_index(queue)
    join_seconds = EXPECTED_JOIN_SILENCE_MS / 1000

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    character_names: dict[str, str] = {}
    for ordinal in routing.EXPECTED_ORDINALS:
        route = routes[ordinal]
        clip = clips[ordinal]
        _expect(
            route.get("span_id"),
            clip.get("span_id"),
            label=f"clip {ordinal} routing/package span_id",
        )
        _expect_float(
            route.get("audio", {}).get("duration_seconds"),
            float(clip.get("duration_seconds")),
            label=f"clip {ordinal} duration",
        )
        character = route.get("character")
        if not isinstance(character, dict):
            raise VoiceCompositionViabilityError(
                f"clip {ordinal} character must be an object"
            )
        character_id = base._require_string(
            character.get("character_id"), label=f"clip {ordinal} character_id"
        )
        character_name = base._require_string(
            character.get("character_name"),
            label=f"clip {ordinal} character_name",
        )
        _expect(
            clip.get("character_id"),
            character_id,
            label=f"clip {ordinal} package character_id",
        )
        slot = clip.get("slot")
        if slot not in {"A", "B"}:
            raise VoiceCompositionViabilityError(
                f"clip {ordinal} package slot must be A or B"
            )
        character_names[character_id] = character_name
        grouped.setdefault((character_id, slot), []).append(route)

    _expect(set(grouped), set(proposals), label="routed/package proposal set")
    slot_results: list[dict[str, Any]] = []
    for character_id, slot in sorted(
        grouped, key=lambda item: (character_names[item[0]], item[1])
    ):
        all_routes = grouped[(character_id, slot)]
        proposal = proposals[(character_id, slot)]
        lexical = [
            route
            for route in all_routes
            if route["routing"]["track"] == routing.TRACK_LEXICAL
        ]
        paralinguistic = [
            route
            for route in all_routes
            if route["routing"]["track"] == routing.TRACK_PARALINGUISTIC
        ]
        excluded = [
            route
            for route in all_routes
            if route["routing"]["track"] == routing.TRACK_DUPLICATE
        ]
        original_duration = _duration_with_joins(all_routes, join_seconds)
        predicted = proposal.get("predicted_composite_qc")
        if not isinstance(predicted, dict):
            raise VoiceCompositionViabilityError(
                f"proposal {character_id}/{slot} predicted QC must be an object"
            )
        _expect_float(
            predicted.get("duration_seconds"),
            original_duration,
            label=f"proposal {character_id}/{slot} original duration",
        )
        retained_duration = _duration_with_joins(lexical, join_seconds)
        component_count = len(lexical)
        duration_eligible = (
            MINIMUM_DURATION_SECONDS
            <= retained_duration
            <= MAXIMUM_DURATION_SECONDS
        )
        component_count_eligible = (
            MINIMUM_COMPONENT_COUNT
            <= component_count
            <= MAXIMUM_COMPONENT_COUNT
        )
        duration_deficit = round(
            max(0.0, MINIMUM_DURATION_SECONDS - retained_duration), 6
        )
        slot_results.append(
            {
                "character_id": character_id,
                "character_name": character_names[character_id],
                "slot": slot,
                "source_ordinals": [route["ordinal"] for route in all_routes],
                "retained_lexical_ordinals": [
                    route["ordinal"] for route in lexical
                ],
                "paralinguistic_ordinals": [
                    route["ordinal"] for route in paralinguistic
                ],
                "excluded_duplicate_ordinals": [
                    route["ordinal"] for route in excluded
                ],
                "original_predicted_duration_seconds": original_duration,
                "retained_lexical_duration_seconds": retained_duration,
                "duration_deficit_to_minimum_seconds": duration_deficit,
                "retained_component_count": component_count,
                "duration_eligible": duration_eligible,
                "component_count_eligible": component_count_eligible,
                "duration_status": (
                    "duration_candidate_pending_full_qc"
                    if duration_eligible and component_count_eligible
                    else "insufficient_lexical_duration"
                ),
                "full_acoustic_qc_status": "not_run_after_routing",
            }
        )

    pair_results: list[dict[str, Any]] = []
    for character_id in sorted(character_names, key=character_names.get):
        slots = {
            item["slot"]: item
            for item in slot_results
            if item["character_id"] == character_id
        }
        _expect(set(slots), {"A", "B"}, label=f"{character_id} A/B slots")
        retained_routes = {
            slot: [routes[ordinal] for ordinal in item["retained_lexical_ordinals"]]
            for slot, item in slots.items()
        }
        audio_sets = {
            slot: {route["audio"]["sha256"] for route in values}
            for slot, values in retained_routes.items()
        }
        text_sets = {
            slot: {
                route["transcript"]["text_utf8_sha256"] for route in values
            }
            for slot, values in retained_routes.items()
        }
        retained_independent = not (
            audio_sets["A"] & audio_sets["B"]
            or text_sets["A"] & text_sets["B"]
        )
        both_duration_eligible = all(
            item["duration_eligible"] and item["component_count_eligible"]
            for item in slots.values()
        )
        pair_results.append(
            {
                "character_id": character_id,
                "character_name": character_names[character_id],
                "retained_ab_content_independent": retained_independent,
                "both_slots_duration_eligible": both_duration_eligible,
                "complete_ab_candidate": (
                    retained_independent and both_duration_eligible
                ),
                "blocking_slots": [
                    slot
                    for slot in ("A", "B")
                    if not (
                        slots[slot]["duration_eligible"]
                        and slots[slot]["component_count_eligible"]
                    )
                ],
                "full_acoustic_qc_status": "not_run_after_routing",
            }
        )

    summary = {
        "character_count": len(pair_results),
        "slot_count": len(slot_results),
        "duration_eligible_slot_count": sum(
            item["duration_eligible"] and item["component_count_eligible"]
            for item in slot_results
        ),
        "insufficient_duration_slot_count": sum(
            not item["duration_eligible"] for item in slot_results
        ),
        "complete_ab_candidate_character_count": sum(
            item["complete_ab_candidate"] for item in pair_results
        ),
        "full_acoustic_qc_completed_slot_count": 0,
    }
    return slot_results, pair_results, summary


def build_viability_receipt(
    voice_root: Path,
    routing_id: str,
    *,
    reviewer_id: str,
    recorded_at: str | None = None,
    expected_routing_sha256: str | None = None,
    expected_package_sha256: str | None = None,
    expected_queue_sha256: str | None = None,
    expected_policy_sha256: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if not base.REVIEWER_ID_PATTERN.fullmatch(reviewer_id):
        raise VoiceCompositionViabilityError("reviewer_id has an invalid format")
    sources = _load_inputs(
        voice_root,
        routing_id,
        expected_routing_sha256=expected_routing_sha256,
        expected_package_sha256=expected_package_sha256,
        expected_queue_sha256=expected_queue_sha256,
        expected_policy_sha256=expected_policy_sha256,
    )
    identity = sources["routing"].get("stable_identity")
    if not isinstance(identity, dict):
        raise VoiceCompositionViabilityError(
            "routing stable_identity must be an object"
        )
    _expect(identity.get("reviewer_id"), reviewer_id, label="reviewer identity")
    slots, pairs, summary = _build_viability(
        sources["routing"], sources["package"], sources["queue"]
    )
    result_set = {"slots": slots, "character_pairs": pairs, "summary": summary}
    result_set_sha = base._semantic_sha256(result_set)
    stable_identity = {
        "viability_profile": PROFILE,
        "routing_id": sources["routing_id"],
        "routing_receipt_sha256": sources["routing_receipt_sha"],
        "routing_byte_sha256": sources["routing_byte_sha"],
        "policy_report_byte_sha256": sources["policy_byte_sha"],
        "reviewer_id": reviewer_id,
        "result_set_sha256": result_set_sha,
    }
    identity_sha = base._semantic_sha256(stable_identity)
    viability_id = f"voice-composition-viability-{identity_sha[:20]}"
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA,
        "viability_id": viability_id,
        "recorded_at": base._parse_recorded_at(recorded_at),
        "artifact_purpose": "immutable_post_routing_ab_duration_viability_audit",
        "stable_identity": stable_identity,
        "stable_identity_sha256": identity_sha,
        "source_artifacts": {
            "corpus_routing": {
                "routing_id": sources["routing_id"],
                "receipt_sha256": sources["routing_receipt_sha"],
                "byte_sha256": sources["routing_byte_sha"],
            },
            "review_package": {
                "package_id": sources["package_id"],
                "manifest_sha256": sources["package_manifest_sha"],
                "byte_sha256": sources["package_byte_sha"],
            },
            "span_review_queue": {"byte_sha256": sources["queue_byte_sha"]},
            "fixed_policy_report": {
                "relative_path": POLICY_RELATIVE_PATH.as_posix(),
                "byte_sha256": sources["policy_byte_sha"],
            },
        },
        "fixed_rules": {
            "minimum_duration_seconds": MINIMUM_DURATION_SECONDS,
            "maximum_duration_seconds": MAXIMUM_DURATION_SECONDS,
            "minimum_component_count": MINIMUM_COMPONENT_COUNT,
            "maximum_component_count": MAXIMUM_COMPONENT_COUNT,
            "join_silence_ms": EXPECTED_JOIN_SILENCE_MS,
            "ab_must_not_share_audio_or_text_hashes": True,
        },
        "analysis_limits": {
            "analysis_kind": "duration_and_retained_content_independence_only",
            "audio_composed": False,
            "full_acoustic_qc_run_after_routing": False,
            "duration_eligibility_is_not_full_qc_approval": True,
        },
        "result_set_sha256": result_set_sha,
        **result_set,
        "scope_limits": {
            "source_artifacts_modified": False,
            "composition_approved": False,
            "full_acoustic_qc_completed": False,
            "training_use_approved": False,
            "voice_cloning_approved": False,
            "rights_accepted": False,
            "publication_approved": False,
            "provider_enrollment_allowed": False,
            "public_rollout_allowed": False,
        },
    }
    receipt["receipt_sha256"] = base._semantic_sha256(receipt)
    destination = sources["root"] / OUTPUT_DIRECTORY / f"{viability_id}.json"
    return receipt, destination


def _pretty_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _validate_shape(receipt: dict[str, Any]) -> None:
    _expect(receipt.get("schema_version"), SCHEMA, label="viability schema")
    viability_id = base._safe_identifier(
        receipt.get("viability_id"),
        VIABILITY_ID_PATTERN,
        label="viability_id",
    )
    base._verify_semantic_hash(
        receipt, field="receipt_sha256", label="composition viability receipt"
    )
    identity = receipt.get("stable_identity")
    if not isinstance(identity, dict):
        raise VoiceCompositionViabilityError("stable_identity must be an object")
    identity_sha = base._semantic_sha256(identity)
    _expect(
        receipt.get("stable_identity_sha256"),
        identity_sha,
        label="stable identity SHA-256",
    )
    _expect(
        viability_id,
        f"voice-composition-viability-{identity_sha[:20]}",
        label="derived viability ID",
    )
    result_set = {
        "slots": receipt.get("slots"),
        "character_pairs": receipt.get("character_pairs"),
        "summary": receipt.get("summary"),
    }
    result_sha = base._semantic_sha256(result_set)
    _expect(
        receipt.get("result_set_sha256"),
        result_sha,
        label="result set SHA-256",
    )
    _expect(
        identity.get("result_set_sha256"),
        result_sha,
        label="identity result set SHA-256",
    )
    base._require_all_false(
        receipt.get("scope_limits"), label="viability scope_limits"
    )


def validate_viability_receipt(
    voice_root: Path, viability_id: str
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(
        root, root, label="voice root", directory=True
    )
    viability_id = base._safe_identifier(
        viability_id, VIABILITY_ID_PATTERN, label="viability_id"
    )
    path = root / OUTPUT_DIRECTORY / f"{viability_id}.json"
    receipt, payload = base._read_json(
        root, path, label="composition viability receipt"
    )
    _validate_shape(receipt)
    _expect(
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(_pretty_bytes(receipt)).hexdigest(),
        label="viability receipt bytes",
    )
    identity = receipt["stable_identity"]
    artifacts = receipt.get("source_artifacts")
    if not isinstance(artifacts, dict):
        raise VoiceCompositionViabilityError("source_artifacts must be an object")
    route_anchor = artifacts.get("corpus_routing")
    package_anchor = artifacts.get("review_package")
    queue_anchor = artifacts.get("span_review_queue")
    policy_anchor = artifacts.get("fixed_policy_report")
    if not all(
        isinstance(item, dict)
        for item in (route_anchor, package_anchor, queue_anchor, policy_anchor)
    ):
        raise VoiceCompositionViabilityError(
            "viability source artifact anchors are incomplete"
        )
    rebuilt, rebuilt_path = build_viability_receipt(
        root,
        identity.get("routing_id"),
        reviewer_id=identity.get("reviewer_id"),
        recorded_at=receipt.get("recorded_at"),
        expected_routing_sha256=route_anchor.get("byte_sha256"),
        expected_package_sha256=package_anchor.get("byte_sha256"),
        expected_queue_sha256=queue_anchor.get("byte_sha256"),
        expected_policy_sha256=policy_anchor.get("byte_sha256"),
    )
    _expect(rebuilt_path, path, label="rebuilt viability path")
    _expect(receipt, rebuilt, label="viability/source reconstruction")
    return {
        "status": "valid",
        "viability_id": viability_id,
        "path": str(path),
        "receipt_sha256": receipt["receipt_sha256"],
        "byte_sha256": hashlib.sha256(payload).hexdigest(),
        "summary": receipt["summary"],
        "slots": receipt["slots"],
        "all_scope_gates_closed": True,
    }


def _existing_receipt(
    root: Path, desired: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    validate_viability_receipt(root, desired["viability_id"])
    path = root / OUTPUT_DIRECTORY / f"{desired['viability_id']}.json"
    existing, _ = base._read_json(root, path, label="existing viability receipt")
    if existing.get("stable_identity") != desired.get("stable_identity"):
        return "existing_conflict", existing
    return "existing_valid", existing


def write_viability_receipt(
    voice_root: Path, receipt: dict[str, Any], destination: Path
) -> tuple[str, dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    _validate_shape(receipt)
    output = root / OUTPUT_DIRECTORY
    if output.exists():
        base._require_safe_existing_path(
            root, output, label="viability output directory", directory=True
        )
    else:
        try:
            output.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(
            root, output, label="viability output directory", directory=True
        )
    expected = output / f"{receipt['viability_id']}.json"
    _expect(
        base._absolute_lexical(destination), expected, label="viability destination"
    )
    if expected.exists():
        return _existing_receipt(root, receipt)
    temporary = output / f".{receipt['viability_id']}.{uuid.uuid4().hex}.partial"
    try:
        with temporary.open("xb") as stream:
            stream.write(_pretty_bytes(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        base._require_safe_existing_path(
            root, temporary, label="temporary viability receipt", directory=False
        )
        try:
            os.link(temporary, expected)
        except FileExistsError:
            return _existing_receipt(root, receipt)
        validation = validate_viability_receipt(root, receipt["viability_id"])
        _expect(
            validation["receipt_sha256"],
            receipt["receipt_sha256"],
            label="written viability receipt SHA-256",
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
    record = subparsers.add_parser("record", help="dry-run or append viability audit")
    record.add_argument("--voice-root", type=Path, required=True)
    record.add_argument("--routing-id", required=True)
    record.add_argument("--reviewer-id", default="xiaob")
    record.add_argument("--recorded-at")
    record.add_argument("--expect-routing-sha256")
    record.add_argument("--expect-package-sha256")
    record.add_argument("--expect-queue-sha256")
    record.add_argument("--expect-policy-sha256")
    record.add_argument("--confirm-analysis-only", action="store_true")
    record.add_argument("--execute", action="store_true")
    validate = subparsers.add_parser("validate", help="validate viability audit")
    validate.add_argument("--voice-root", type=Path, required=True)
    validate.add_argument("--viability-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            result = validate_viability_receipt(
                arguments.voice_root, arguments.viability_id
            )
        else:
            if arguments.execute and not arguments.confirm_analysis_only:
                raise VoiceCompositionViabilityError(
                    "--execute requires --confirm-analysis-only"
                )
            receipt, destination = build_viability_receipt(
                arguments.voice_root,
                arguments.routing_id,
                reviewer_id=arguments.reviewer_id,
                recorded_at=arguments.recorded_at,
                expected_routing_sha256=arguments.expect_routing_sha256,
                expected_package_sha256=arguments.expect_package_sha256,
                expected_queue_sha256=arguments.expect_queue_sha256,
                expected_policy_sha256=arguments.expect_policy_sha256,
            )
            write_status = None
            if arguments.execute:
                write_status, receipt = write_viability_receipt(
                    arguments.voice_root, receipt, destination
                )
                if write_status == "existing_conflict":
                    raise VoiceCompositionViabilityError(
                        "an immutable viability receipt already conflicts"
                    )
            result = {
                "status": "ok",
                "mode": "execute" if arguments.execute else "dry_run",
                "write_status": write_status,
                "viability_id": receipt["viability_id"],
                "path": str(destination),
                "receipt_sha256": receipt["receipt_sha256"],
                "result_set_sha256": receipt["result_set_sha256"],
                "summary": receipt["summary"],
                "slots": receipt["slots"],
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
