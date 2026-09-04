# ruff: noqa: E501
"""Build and validate a private, style-specific local TTS runtime profile.

The profile is derived only from the terminal unseen-text preference conclusion.
It resolves private candidate references to the already-created Beijing Qwen VC
voices, but never prints those references or Provider voice IDs.  This command
does not read credentials, contact the Provider, synthesize audio, or authorize
publication.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_preference_challenger_ops as challenger
    from . import voice_preference_tournament_ops as tournament
    from . import voice_provider_blind_test_ops as blind
else:
    import voice_paralinguistic_ops as base
    import voice_preference_challenger_ops as challenger
    import voice_preference_tournament_ops as tournament
    import voice_provider_blind_test_ops as blind


SCHEMA = "project-snow-private-local-voice-runtime-profile-1"
POLICY_VERSION = "project-snow-style-specific-qwen-vc-runtime-routing-1"
OUTPUT_DIRECTORY = "tts_runtime_profiles"
PROFILE_ID_PATTERN = re.compile(r"voice-runtime-profile-[0-9a-f]{20}\Z")
DEFAULT_TERMINAL_ROUND_ID = "voice-preference-round-9fcc6ed7447cbfa10728"

STYLE_BY_ANCHOR = {
    "neutral": "neutral",
    "neutral_short": "neutral",
    "restrained_breathy_lexical": "breathy",
    "heightened": "heightened",
    "heightened_fixated_lexical": "heightened",
    "heightened_urgent_lexical": "heightened",
}
EXPECTED_STYLE_ORDER = ("neutral", "breathy", "heightened")
CHARACTERS = {
    "vidya": {
        "runtime_character_id": "5157b8972632",
        "runtime_character_name": "薇蒂雅",
    },
    "chenxing": {
        "runtime_character_id": "98322bd505f4",
        "runtime_character_name": "辰星",
    },
}


class VoiceRuntimeProfileError(base.VoiceParalinguisticError):
    """Raised when private runtime routing cannot be proven safe."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceRuntimeProfileError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceRuntimeProfileError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceRuntimeProfileError(f"{label} must be an array")
    return value


def _string(value: Any, *, label: str) -> str:
    try:
        return base._require_string(value, label=label)
    except base.VoiceParalinguisticError as error:
        raise VoiceRuntimeProfileError(str(error)) from error


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _terminal_sources(
    root: Path, round_id: str
) -> tuple[
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
    dict[str, str],
    str,
]:
    safe_round_id = base._safe_identifier(round_id, tournament.ROUND_ID_PATTERN, label="terminal round ID")
    operator = root / tournament.OUTPUT_DIRECTORY / safe_round_id / "operator"
    base._require_safe_existing_path(root, operator, label="terminal operator directory", directory=True)
    conclusion, conclusion_payload = base._read_json(
        root, operator / "terminal-conclusion.json", label="terminal conclusion"
    )
    _expect(
        conclusion.get("schema_version"),
        challenger.TERMINAL_CONCLUSION_SCHEMA,
        label="terminal conclusion schema",
    )
    _expect(
        conclusion.get("status"),
        "terminal_current_clone_pool_concluded",
        label="terminal conclusion status",
    )
    base._verify_semantic_hash(conclusion, field="manifest_sha256", label="terminal conclusion")
    rebuilt, rebuilt_path = challenger.build_terminal_conclusion(
        root,
        safe_round_id,
        concluded_at=_string(conclusion.get("concluded_at"), label="concluded_at"),
    )
    _expect(rebuilt_path, operator / "terminal-conclusion.json", label="terminal path")
    _expect(conclusion, rebuilt, label="terminal conclusion reconstruction")

    candidate_map, candidate_map_payload = base._read_json(
        root, operator / "candidate-map.json", label="terminal candidate map"
    )
    _expect(candidate_map.get("schema_version"), challenger.MAP_SCHEMA, label="candidate-map schema")
    _expect(candidate_map.get("round_id"), safe_round_id, label="candidate-map round ID")
    base._verify_semantic_hash(candidate_map, field="manifest_sha256", label="terminal candidate map")
    conclusion_source = _object(conclusion.get("source"), label="terminal source")
    _expect(
        conclusion_source.get("candidate_map_sha256"),
        candidate_map.get("manifest_sha256"),
        label="terminal candidate-map SHA-256",
    )
    _expect(
        conclusion_source.get("candidate_map_byte_sha256"),
        base._sha256_bytes(candidate_map_payload),
        label="terminal candidate-map byte SHA-256",
    )

    challenger_run_id = base._safe_identifier(
        candidate_map.get("source_challenger_run_id"),
        challenger.RUN_ID_PATTERN,
        label="source challenger run ID",
    )
    challenger_path = root / challenger.OUTPUT_DIRECTORY / challenger_run_id / "manifest.json"
    challenger_manifest, challenger_payload = base._read_json(
        root, challenger_path, label="source challenger manifest"
    )
    _expect(
        challenger_manifest.get("manifest_sha256"),
        candidate_map.get("source_challenger_manifest_sha256"),
        label="source challenger semantic SHA-256",
    )
    directory, validated_challenger, voices, workspace = challenger._load_run(
        root,
        challenger_run_id,
        expected_manifest_byte_sha256=base._sha256_bytes(challenger_payload),
        revalidate_sources=True,
    )
    _expect(directory / "manifest.json", challenger_path, label="source challenger path")
    _expect(validated_challenger, challenger_manifest, label="source challenger reconstruction")
    _expect(set(voices), set(blind.CANDIDATE_ORDER), label="resolved Provider voices")
    return (
        conclusion,
        conclusion_payload,
        candidate_map,
        candidate_map_payload,
        challenger_manifest,
        challenger_payload,
        voices,
        workspace,
    )


def build_profile(
    voice_root: Path,
    *,
    terminal_round_id: str,
    prepared_at: str,
) -> tuple[dict[str, Any], Path]:
    """Resolve terminal selections into a private per-character/per-style profile."""

    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    timestamp = challenger._timestamp(prepared_at, label="prepared_at")
    (
        conclusion,
        conclusion_payload,
        candidate_map,
        candidate_map_payload,
        challenger_manifest,
        challenger_payload,
        voices,
        workspace,
    ) = _terminal_sources(root, terminal_round_id)
    source_blind = _object(challenger_manifest.get("source_blind_test"), label="source blind test")

    routes_by_character: dict[str, list[dict[str, Any]]] = {slug: [] for slug in CHARACTERS}
    locked_count = 0
    paused_case_ids: list[str] = []
    identity_routes: list[dict[str, Any]] = []
    for raw_slot in _array(conclusion.get("slots"), label="terminal slots"):
        slot = _object(raw_slot, label="terminal slot")
        case_id = _string(slot.get("case_id"), label="terminal case ID")
        slug = _string(slot.get("character_slug"), label=f"{case_id} character slug")
        character = CHARACTERS.get(slug)
        if character is None:
            raise VoiceRuntimeProfileError(f"{case_id} has an unsupported character")
        _expect(
            slot.get("runtime_character_name"),
            character["runtime_character_name"],
            label=f"{case_id} runtime character name",
        )
        style_anchor = _string(slot.get("style_anchor"), label=f"{case_id} style anchor")
        style = STYLE_BY_ANCHOR.get(style_anchor)
        if style is None:
            raise VoiceRuntimeProfileError(f"{case_id} has an unsupported style anchor")
        disposition = _string(slot.get("disposition"), label=f"{case_id} disposition")
        candidate = slot.get("runtime_candidate_ref")
        route: dict[str, Any] = {
            "case_id": case_id,
            "style": style,
            "source_style_anchor": style_anchor,
        }
        identity_route: dict[str, Any] = {
            "case_id": case_id,
            "character_slug": slug,
            "style": style,
            "disposition": disposition,
        }
        if disposition == "locked_for_slot":
            candidate_key = _string(candidate, label=f"{case_id} runtime candidate")
            if candidate_key not in voices or not candidate_key.startswith(f"{slug}-"):
                raise VoiceRuntimeProfileError(f"{case_id} does not resolve to a voice for its character")
            provider_voice_id = _string(voices[candidate_key], label=f"{case_id} Provider voice ID")
            voice_hash = base._sha256_bytes(provider_voice_id.encode("utf-8"))
            route.update(
                {
                    "status": "locked",
                    "provider_voice_id": provider_voice_id,
                    "provider_voice_id_sha256": voice_hash,
                }
            )
            identity_route["provider_voice_id_sha256"] = voice_hash
            locked_count += 1
        elif disposition == "paused_not_qualified":
            _expect(candidate, None, label=f"{case_id} paused runtime candidate")
            route.update(
                {
                    "status": "paused",
                    "unavailable_reason": "terminal_preference_slot_not_qualified",
                }
            )
            paused_case_ids.append(case_id)
        else:
            raise VoiceRuntimeProfileError(f"{case_id} has an unsupported disposition")
        routes_by_character[slug].append(route)
        identity_routes.append(identity_route)

    characters: list[dict[str, Any]] = []
    for slug, character in CHARACTERS.items():
        routes = routes_by_character[slug]
        _expect([item["style"] for item in routes], list(EXPECTED_STYLE_ORDER), label=f"{slug} style order")
        characters.append({"character_slug": slug, **character, "routes": routes})
    _expect(locked_count, 5, label="locked route count")
    _expect(paused_case_ids, ["vidya-breathy-lexical"], label="paused route set")

    provider_contract = {
        "provider_family": "Alibaba Cloud Model Studio",
        "region": blind.REGION,
        "target_model": blind.MODEL,
        "websocket_endpoint": blind.WEBSOCKET_ENDPOINT,
        "mode": blind.MODE,
        "language_type": blind.LANGUAGE_TYPE,
        "response_format": blind.RESPONSE_FORMAT,
        "sample_rate_hz": blind.SAMPLE_RATE_HZ,
        "channels": blind.CHANNELS,
        "sample_width_bytes": blind.SAMPLE_WIDTH_BYTES,
        "workspace_id": workspace,
        "workspace_id_sha256": base._sha256_bytes(workspace.encode("utf-8")),
        "instruction_control_supported_by_target_model": False,
        "instructions_sent": False,
    }
    stable_identity = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "terminal_conclusion_sha256": conclusion["manifest_sha256"],
        "candidate_map_sha256": candidate_map["manifest_sha256"],
        "challenger_manifest_sha256": challenger_manifest["manifest_sha256"],
        "blind_manifest_sha256": source_blind["manifest_sha256"],
        "provider_contract": {
            key: value for key, value in provider_contract.items() if key != "workspace_id"
        },
        "routes": identity_routes,
    }
    profile_id = "voice-runtime-profile-" + base._semantic_sha256(stable_identity)[:20]
    document: dict[str, Any] = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "profile_id": profile_id,
        "prepared_at": timestamp,
        "status": "offline_local_runtime_profile_ready",
        "source": {
            "terminal_round_id": terminal_round_id,
            "terminal_conclusion_id": conclusion["conclusion_id"],
            "terminal_conclusion_sha256": conclusion["manifest_sha256"],
            "terminal_conclusion_byte_sha256": base._sha256_bytes(conclusion_payload),
            "candidate_map_sha256": candidate_map["manifest_sha256"],
            "candidate_map_byte_sha256": base._sha256_bytes(candidate_map_payload),
            "challenger_run_id": challenger_manifest["run_id"],
            "challenger_manifest_sha256": challenger_manifest["manifest_sha256"],
            "challenger_manifest_byte_sha256": base._sha256_bytes(challenger_payload),
            "blind_test_run_id": source_blind["run_id"],
            "blind_test_manifest_sha256": source_blind["manifest_sha256"],
            "blind_test_manifest_byte_sha256": source_blind["manifest_byte_sha256"],
        },
        "provider_contract": provider_contract,
        "routing_contract": {
            "style_selector": "explicit_internal_style_or_deterministic_lexical_classifier",
            "supported_styles": list(EXPECTED_STYLE_ORDER),
            "paused_slot_fallback_allowed": False,
            "cross_character_fallback_allowed": False,
            "cross_style_fallback_allowed": False,
            "provider_voice_id_exposed_to_client": False,
            "user_supplied_voice_id_allowed": False,
            "user_supplied_model_allowed": False,
            "user_supplied_websocket_endpoint_allowed": False,
            "paralinguistic_ordinals_2_and_3_included": False,
        },
        "authorization_contract": {
            "local_runtime_integration_authorized": True,
            "provider_calls_performed_while_building_profile": False,
            "incremental_provider_cost_usd": "0",
            "publication_authorized": False,
            "public_rollout_authorized": False,
            "provider_voice_creation_authorized": False,
            "provider_voice_deletion_authorized": False,
            "training_or_fine_tuning_authorized": False,
        },
        "characters": characters,
        "summary": {
            "character_count": len(characters),
            "style_slot_count": sum(len(item["routes"]) for item in characters),
            "locked_slot_count": locked_count,
            "paused_slot_count": len(paused_case_ids),
            "paused_case_ids": paused_case_ids,
        },
        "stable_identity_sha256": base._semantic_sha256(stable_identity),
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    destination = root / OUTPUT_DIRECTORY / profile_id
    return document, destination


def _validate_profile_shape(document: dict[str, Any]) -> None:
    _expect(document.get("schema_version"), SCHEMA, label="runtime profile schema")
    profile_id = base._safe_identifier(
        document.get("profile_id"), PROFILE_ID_PATTERN, label="runtime profile ID"
    )
    _expect(
        document.get("status"),
        "offline_local_runtime_profile_ready",
        label="runtime profile status",
    )
    base._verify_semantic_hash(document, field="manifest_sha256", label="runtime profile")
    challenger._timestamp(document.get("prepared_at"), label="prepared_at")
    _expect(
        profile_id,
        "voice-runtime-profile-"
        + _string(document.get("stable_identity_sha256"), label="stable identity")[:20],
        label="runtime profile ID derivation",
    )
    provider = _object(document.get("provider_contract"), label="provider contract")
    for key, expected in (
        ("region", blind.REGION),
        ("target_model", blind.MODEL),
        ("websocket_endpoint", blind.WEBSOCKET_ENDPOINT),
        ("response_format", blind.RESPONSE_FORMAT),
        ("sample_rate_hz", blind.SAMPLE_RATE_HZ),
        ("channels", blind.CHANNELS),
        ("sample_width_bytes", blind.SAMPLE_WIDTH_BYTES),
        ("instruction_control_supported_by_target_model", False),
        ("instructions_sent", False),
    ):
        _expect(provider.get(key), expected, label=f"provider {key}")
    routing = _object(document.get("routing_contract"), label="routing contract")
    for key in (
        "paused_slot_fallback_allowed",
        "cross_character_fallback_allowed",
        "cross_style_fallback_allowed",
        "provider_voice_id_exposed_to_client",
        "user_supplied_voice_id_allowed",
        "user_supplied_model_allowed",
        "user_supplied_websocket_endpoint_allowed",
        "paralinguistic_ordinals_2_and_3_included",
    ):
        _expect(routing.get(key), False, label=f"routing {key}")


def write_profile(voice_root: Path, document: dict[str, Any], destination: Path) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    _validate_profile_shape(document)
    parent = root / OUTPUT_DIRECTORY
    parent.mkdir(exist_ok=True)
    base._require_safe_existing_path(root, parent, label="runtime profile root", directory=True)
    _expect(destination.parent, parent, label="runtime profile destination parent")
    destination.mkdir(exist_ok=True)
    base._require_safe_existing_path(root, destination, label="runtime profile directory", directory=True)
    state = challenger._write_or_verify(
        root,
        destination / "manifest.json",
        _pretty_json_bytes(document),
        label="runtime profile manifest",
    )
    summary = _object(document.get("summary"), label="runtime profile summary")
    return {
        "status": document["status"],
        "write_status": state,
        "profile_id": document["profile_id"],
        "profile_path": str(destination / "manifest.json"),
        "manifest_sha256": document["manifest_sha256"],
        "character_count": summary["character_count"],
        "style_slot_count": summary["style_slot_count"],
        "locked_slot_count": summary["locked_slot_count"],
        "paused_slot_count": summary["paused_slot_count"],
        "paused_case_ids": summary["paused_case_ids"],
        "provider_calls_performed": False,
        "incremental_provider_cost_usd": "0",
        "private_candidate_refs_exposed_in_result": False,
        "private_provider_voice_ids_exposed_in_result": False,
    }


def validate_profile(
    voice_root: Path,
    profile_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    safe_id = base._safe_identifier(profile_id, PROFILE_ID_PATTERN, label="runtime profile ID")
    path = root / OUTPUT_DIRECTORY / safe_id / "manifest.json"
    document, payload = base._read_json(root, path, label="runtime profile manifest")
    _validate_profile_shape(document)
    _expect(document.get("profile_id"), safe_id, label="runtime profile directory ID")
    if expected_manifest_byte_sha256 is not None:
        _expect(
            base._sha256_bytes(payload),
            base._require_sha256(
                expected_manifest_byte_sha256,
                label="expected runtime profile manifest byte SHA-256",
            ),
            label="runtime profile byte SHA-256",
        )
    source = _object(document.get("source"), label="runtime profile source")
    rebuilt, destination = build_profile(
        root,
        terminal_round_id=_string(source.get("terminal_round_id"), label="source terminal round ID"),
        prepared_at=_string(document.get("prepared_at"), label="prepared_at"),
    )
    _expect(destination / "manifest.json", path, label="rebuilt runtime profile path")
    _expect(document, rebuilt, label="runtime profile reconstruction")
    summary = _object(document.get("summary"), label="runtime profile summary")
    return {
        "status": "valid",
        "profile_id": safe_id,
        "manifest_sha256": document["manifest_sha256"],
        "manifest_byte_sha256": base._sha256_bytes(payload),
        "character_count": summary["character_count"],
        "style_slot_count": summary["style_slot_count"],
        "locked_slot_count": summary["locked_slot_count"],
        "paused_slot_count": summary["paused_slot_count"],
        "paused_case_ids": summary["paused_case_ids"],
        "provider_calls_performed": False,
        "incremental_provider_cost_usd": "0",
        "private_candidate_refs_exposed_in_result": False,
        "private_provider_voice_ids_exposed_in_result": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare the private runtime profile")
    prepare.add_argument("--terminal-round-id", default=DEFAULT_TERMINAL_ROUND_ID)
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--execute", action="store_true")
    validate = commands.add_parser("validate", help="validate an existing runtime profile")
    validate.add_argument("--profile-id", required=True)
    validate.add_argument("--expect-manifest-byte-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            document, destination = build_profile(
                arguments.voice_root,
                terminal_round_id=arguments.terminal_round_id,
                prepared_at=arguments.prepared_at,
            )
            if arguments.execute:
                result = write_profile(arguments.voice_root, document, destination)
            else:
                summary = document["summary"]
                result = {
                    "status": "dry_run_offline_local_runtime_profile",
                    "profile_id": document["profile_id"],
                    "profile_path": str(destination / "manifest.json"),
                    "manifest_sha256": document["manifest_sha256"],
                    "manifest_byte_sha256": base._sha256_bytes(_pretty_json_bytes(document)),
                    "character_count": summary["character_count"],
                    "style_slot_count": summary["style_slot_count"],
                    "locked_slot_count": summary["locked_slot_count"],
                    "paused_slot_count": summary["paused_slot_count"],
                    "paused_case_ids": summary["paused_case_ids"],
                    "provider_calls_performed": False,
                    "incremental_provider_cost_usd": "0",
                    "private_candidate_refs_exposed_in_result": False,
                    "private_provider_voice_ids_exposed_in_result": False,
                }
        else:
            result = validate_profile(
                arguments.voice_root,
                arguments.profile_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
            )
    except (
        OSError,
        ValueError,
        VoiceRuntimeProfileError,
        challenger.VoicePreferenceChallengerError,
        tournament.VoicePreferenceTournamentError,
        blind.VoiceProviderBlindTestError,
    ) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
