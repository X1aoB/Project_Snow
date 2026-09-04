# ruff: noqa: E501
"""Generate and package one fail-closed voice-preference challenger round.

The private run consumes a complete human decision receipt, preserves each
relative winner as the incumbent, and creates only the challengers required by
the decisions. The current Qwen3 VC model has no instruction-control support,
so delivery steering is limited to punctuation-only reshaping of the same
lexical text. Every live request is journaled before the WebSocket is opened.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import sys
import unicodedata
import uuid
from collections.abc import Callable
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_preference_tournament_ops as tournament
    from . import voice_provider_blind_test_ops as blind
    from . import voice_provider_enrollment_ops as enrollment
else:
    import voice_paralinguistic_ops as base
    import voice_preference_tournament_ops as tournament
    import voice_provider_blind_test_ops as blind
    import voice_provider_enrollment_ops as enrollment


SCHEMA = "project-snow-private-voice-preference-challenger-run-1"
AUDIT_SCHEMA = "project-snow-private-voice-preference-challenger-audit-1"
MAP_SCHEMA = "project-snow-private-voice-preference-candidate-map-1"
TERMINAL_CONCLUSION_SCHEMA = (
    "project-snow-private-voice-preference-terminal-conclusion-1"
)
TERMINAL_POLICY_VERSION = "project-snow-current-clone-pool-terminal-selection-1"
POLICY_VERSION = "project-snow-punctuation-only-preference-challenger-1"
UNSEEN_POLICY_VERSION = "project-snow-unseen-dual-candidate-validation-1"
OUTPUT_DIRECTORY = "tts_preference_challenger_runs"
RUN_ID_PATTERN = re.compile(r"voice-preference-challenger-run-[0-9a-f]{20}\Z")
OUTPUT_ID_PATTERN = re.compile(r"challenger-output-[0-9a-f]{16}\Z")
ATTEMPT_ID_PATTERN = re.compile(r"challenger-attempt-[0-9a-f]{32}\Z")

DEFAULT_SOURCE_ROUND_ID = "voice-preference-round-77f40486985a7f7304bb"
DEFAULT_SOURCE_BLIND_MANIFEST_BYTE_SHA256 = (
    "45530d8a7f739c87208a6728ea3b41d5eb58efbd72d3de381da7f128b171a766"
)
INCREMENTAL_COST_CEILING_USD = Decimal("0.01")
EXPECTED_CASE_COUNT = 6
EXPECTED_CURRENT_OUTPUT_COUNT = 7
EXPECTED_UNSEEN_OUTPUT_COUNT = 12

PUNCTUATION_STEERING = {
    "vidya-neutral-short": "今天的状态很稳定。我们可以按计划继续。",
    "vidya-breathy-lexical": "靠近一点，我只想把这句话，轻轻说给你听。",
    "vidya-heightened": "终于等到你了！别想让我现在移开视线！",
    "chenxing-neutral-short": "设备运行正常。下一项检查可以开始。",
    "chenxing-breathy-lexical": "声音放轻些。我就在这里，不必惊动其他人。",
    "chenxing-heightened": "看着我！保持清醒！我们一定能一起回去！",
}

UNSEEN_VALIDATION_TEXT = {
    "vidya-neutral-short": "外面的风小了，我们先把剩下的事情做好。",
    "vidya-breathy-lexical": "别出声，我只想再靠近你一点。",
    "vidya-heightened": "你终于肯回头看我了，这次不许再躲开！",
    "chenxing-neutral-short": "数据已经同步完成，我们继续下一项确认。",
    "chenxing-breathy-lexical": "先别说话，听着我的呼吸，慢慢放松。",
    "chenxing-heightened": "别闭眼！抓住我的手，我现在就带你出去！",
}


class VoicePreferenceChallengerError(base.VoiceParalinguisticError):
    """Raised when a challenger run cannot be proven safe."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoicePreferenceChallengerError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoicePreferenceChallengerError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoicePreferenceChallengerError(f"{label} must be an array")
    return value


def _string(value: Any, *, label: str) -> str:
    try:
        return base._require_string(value, label=label)
    except base.VoiceParalinguisticError as error:
        raise VoicePreferenceChallengerError(str(error)) from error


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _timestamp(value: str, *, label: str) -> str:
    text = _string(value, label=label)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise VoicePreferenceChallengerError(f"{label} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VoicePreferenceChallengerError(f"{label} must include a UTC offset")
    return text


def _safe_directory(root: Path, path: Path, *, label: str) -> Path:
    if not path.exists():
        try:
            path.mkdir()
        except FileExistsError:
            pass
    try:
        return base._require_safe_existing_path(root, path, label=label, directory=True)
    except base.VoiceParalinguisticError as error:
        raise VoicePreferenceChallengerError(str(error)) from error


def _write_new(path: Path, payload: bytes, *, label: str) -> Path:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise VoicePreferenceChallengerError(f"{label} already exists") from error
    return path


def _write_or_verify(root: Path, path: Path, payload: bytes, *, label: str) -> str:
    if path.exists():
        existing = base._read_stable_bytes(root, path, label=label)
        _expect(existing, payload, label=label)
        return "existing_identical"
    blind._write_atomic_new(path, payload, label=label)
    return "written"


def _lexical_text(value: str) -> str:
    return "".join(
        character
        for character in value
        if not character.isspace() and not unicodedata.category(character).startswith("P")
    )


def _load_source_round(
    root: Path, round_id: str
) -> tuple[dict[str, Any], bytes, dict[str, Any], bytes, Path]:
    tournament.validate_round(root, round_id)
    tournament.validate_decision_receipt(root, round_id)
    manifest, manifest_payload, review_directory = tournament._load_round_manifest(
        root, round_id
    )
    receipt_path = (
        root
        / tournament.OUTPUT_DIRECTORY
        / round_id
        / "operator"
        / "decision-receipt.json"
    )
    receipt, receipt_payload = base._read_json(
        root, receipt_path, label="source decision receipt"
    )
    return manifest, manifest_payload, receipt, receipt_payload, review_directory


def _candidate_mapping(private_blind: dict[str, Any]) -> tuple[dict[str, str], dict[str, list[str]]]:
    by_label: dict[str, str] = {}
    by_character: dict[str, list[str]] = {slug: [] for slug in blind.CHARACTER_ORDER}
    for raw in _array(
        private_blind.get("operator_only_candidate_mapping"), label="candidate mapping"
    ):
        mapping = _object(raw, label="character candidate mapping")
        slug = _string(mapping.get("character_slug"), label="candidate character")
        if slug not in by_character:
            raise VoicePreferenceChallengerError("candidate mapping contains an unexpected character")
        for raw_label in _array(mapping.get("labels"), label=f"{slug} candidate labels"):
            item = _object(raw_label, label="candidate label")
            label_id = base._safe_identifier(
                item.get("opaque_label_id"), blind.OPAQUE_ID_PATTERN, label="opaque label ID"
            )
            candidate = _string(item.get("candidate_key"), label="candidate key")
            if label_id in by_label or candidate in by_character[slug]:
                raise VoicePreferenceChallengerError("candidate mapping is not one-to-one")
            by_label[label_id] = candidate
            by_character[slug].append(candidate)
    _expect(
        sorted(
            [candidate for slug in blind.CHARACTER_ORDER for candidate in by_character[slug]],
            key=blind.CANDIDATE_ORDER.index,
        ),
        list(blind.CANDIDATE_ORDER),
        label="candidate order",
    )
    return by_label, by_character


def _random_public_label(existing: set[str]) -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    while True:
        value = "sample-" + "".join(secrets.choice(alphabet) for _ in range(4))
        if value not in existing:
            existing.add(value)
            return value


def _find_prior_sample(case: dict[str, Any], label_id: str) -> dict[str, Any]:
    matches = [
        _object(item, label="prior sample")
        for item in _array(case.get("samples"), label="prior samples")
        if isinstance(item, dict) and item.get("opaque_label_id") == label_id
    ]
    _expect(len(matches), 1, label="selected prior sample count")
    return matches[0]


def build_run(
    voice_root: Path,
    *,
    source_round_id: str,
    source_blind_manifest_byte_sha256: str,
    prepared_at: str,
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    timestamp = _timestamp(prepared_at, label="prepared_at")
    round_manifest, round_payload, receipt, receipt_payload, _ = _load_source_round(
        root, source_round_id
    )
    source_round_index = int(round_manifest.get("round_index", 0))
    if source_round_index == 1:
        strategy = "same_lexical_text_punctuation_only"
        policy_version = POLICY_VERSION
        expected_output_count = EXPECTED_CURRENT_OUTPUT_COUNT
    elif source_round_index == 2:
        strategy = "unseen_text_dual_candidate_validation"
        policy_version = UNSEEN_POLICY_VERSION
        expected_output_count = EXPECTED_UNSEEN_OUTPUT_COUNT
    else:
        raise VoicePreferenceChallengerError(
            "this workflow supports source preference rounds one and two only"
        )
    source = _object(round_manifest.get("source"), label="source round source")
    blind_run_id = _string(source.get("blind_test_run_id"), label="source blind-test run ID")
    private_directory, private_blind, voices = blind.load_run(
        root,
        blind_run_id,
        expected_manifest_byte_sha256=source_blind_manifest_byte_sha256,
        revalidate_sources=True,
    )
    if set(voices) != set(blind.CANDIDATE_ORDER):
        raise VoicePreferenceChallengerError("source voice receipts are incomplete")
    private_payload = base._read_stable_bytes(
        root, private_directory / "manifest.json", label="source private blind manifest"
    )
    by_label, by_character = _candidate_mapping(private_blind)
    decisions = _object(receipt.get("decisions"), label="source decisions")
    cases = _array(round_manifest.get("cases"), label="source round cases")
    _expect(len(cases), EXPECTED_CASE_COUNT, label="source case count")

    planned_outputs: list[dict[str, Any]] = []
    public_pairing_plan: list[dict[str, Any]] = []
    used_public_labels: set[str] = set()
    for sequence, raw_case in enumerate(cases, start=1):
        case = _object(raw_case, label="source case")
        case_id = _string(case.get("case_id"), label="source case ID")
        slug = _string(case.get("character_slug"), label=f"{case_id} character")
        decision = _object(decisions.get(case_id), label=f"{case_id} decision")
        choice = _string(decision.get("relative_choice"), label=f"{case_id} choice")
        selected_label = decision.get("selected_opaque_label_id")
        if strategy == "same_lexical_text_punctuation_only":
            original_text = _string(case.get("text"), label=f"{case_id} text")
            synthesis_text = PUNCTUATION_STEERING.get(case_id)
            if synthesis_text is None:
                raise VoicePreferenceChallengerError(
                    f"no punctuation steering plan for {case_id}"
                )
            _expect(
                _lexical_text(synthesis_text),
                _lexical_text(original_text),
                label=f"{case_id} punctuation-only lexical text",
            )
        else:
            synthesis_text = UNSEEN_VALIDATION_TEXT.get(case_id)
            if synthesis_text is None:
                raise VoicePreferenceChallengerError(
                    f"no unseen validation text for {case_id}"
                )
            if _lexical_text(synthesis_text) == _lexical_text(
                _string(case.get("text"), label=f"{case_id} prior text")
            ):
                raise VoicePreferenceChallengerError(
                    f"{case_id} unseen validation text duplicates the prior text"
                )
        if strategy == "unseen_text_dual_candidate_validation":
            candidates = list(by_character[slug])
        elif choice == "reject_both":
            candidates = list(by_character[slug])
        else:
            if not isinstance(selected_label, str) or selected_label not in by_label:
                raise VoicePreferenceChallengerError(f"{case_id} winner cannot be mapped")
            candidates = [by_label[selected_label]]
        outputs_for_case: list[dict[str, Any]] = []
        for candidate in candidates:
            output_id = f"challenger-output-{secrets.token_hex(8)}"
            output = {
                "output_id": output_id,
                "sequence": len(planned_outputs) + 1,
                "case_id": case_id,
                "character_slug": slug,
                "candidate_key": candidate,
                "text": synthesis_text,
                "text_sha256": base._sha256_bytes(synthesis_text.encode("utf-8")),
                "input_character_count": len(synthesis_text),
                "billing_character_count": blind._billing_character_count(synthesis_text),
                "delivery_strategy": strategy,
                "private_audio_relative_path": (
                    f"audio/{slug}/{len(planned_outputs) + 1:02d}-{output_id}.wav"
                ),
            }
            planned_outputs.append(output)
            outputs_for_case.append(output)

        pair_sources: list[dict[str, Any]] = []
        if strategy == "same_lexical_text_punctuation_only" and choice != "reject_both":
            prior = _find_prior_sample(case, _string(selected_label, label="selected label"))
            pair_sources.append(
                {
                    "origin": "prior_relative_winner",
                    "candidate_key": by_label[selected_label],
                    "prior_opaque_label_id": selected_label,
                    "prior_audio_relative_path": prior["audio_relative_path"],
                    "prior_wav_sha256": prior["wav_sha256"],
                }
            )
        for output in outputs_for_case:
            pair_sources.append(
                {
                    "origin": "new_challenger",
                    "candidate_key": output["candidate_key"],
                    "output_id": output["output_id"],
                }
            )
        _expect(len(pair_sources), 2, label=f"{case_id} public pair source count")
        secrets.SystemRandom().shuffle(pair_sources)
        public_samples = []
        for display_order, item in enumerate(pair_sources, start=1):
            public_label = _random_public_label(used_public_labels)
            public_samples.append(
                {
                    **item,
                    "display_order": display_order,
                    "opaque_label_id": public_label,
                    "display_label": f"样本 {public_label.removeprefix('sample-').upper()}",
                }
            )
        public_pairing_plan.append(
            {
                "sequence": sequence,
                "case_id": case_id,
                "character_slug": slug,
                "samples": public_samples,
            }
        )

    _expect(len(planned_outputs), expected_output_count, label="challenger output count")
    planned_billable = sum(item["billing_character_count"] for item in planned_outputs)
    estimated_cost = blind._estimated_cost(
        planned_billable, blind.PRICE_USD_PER_10K_CHARACTERS
    )
    if estimated_cost > INCREMENTAL_COST_CEILING_USD:
        raise VoicePreferenceChallengerError("planned challenger cost exceeds the ceiling")
    run_id = f"voice-preference-challenger-run-{secrets.token_hex(10)}"
    document: dict[str, Any] = {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "policy_version": policy_version,
        "prepared_at": timestamp,
        "status": "prepared_for_authorized_challenger_synthesis",
        "source_round": {
            "round_id": source_round_id,
            "round_manifest_sha256": round_manifest["manifest_sha256"],
            "round_manifest_byte_sha256": base._sha256_bytes(round_payload),
            "decision_receipt_sha256": receipt["receipt_sha256"],
            "decision_receipt_byte_sha256": base._sha256_bytes(receipt_payload),
            "decision_set_sha256": receipt["decision_set_sha256"],
        },
        "source_blind_test": {
            "run_id": blind_run_id,
            "manifest_sha256": private_blind["manifest_sha256"],
            "manifest_byte_sha256": base._sha256_bytes(private_payload),
        },
        "provider_contract": {
            "region": blind.REGION,
            "target_model": blind.MODEL,
            "websocket_endpoint": blind.WEBSOCKET_ENDPOINT,
            "synthesis_parameters": private_blind["provider_contract"][
                "synthesis_parameters"
            ],
            "instruction_control_supported_by_target_model": False,
            "instructions_sent": False,
            "steering_scope": strategy,
        },
        "pricing_contract": {
            "billing_counting_rule": "cjk_ideographs_count_as_two_characters_other_codepoints_as_one",
            "planned_output_count": len(planned_outputs),
            "planned_billable_input_characters": planned_billable,
            "official_price_usd_per_10000_input_characters": str(
                blind.PRICE_USD_PER_10K_CHARACTERS
            ),
            "estimated_incremental_cost_usd": str(estimated_cost),
            "incremental_synthesis_cost_ceiling_usd": str(
                INCREMENTAL_COST_CEILING_USD
            ),
            "actual_charge_status": "unknown_until_provider_billing_reconciliation",
        },
        "authorization_contract": {
            "challenger_synthesis_and_local_review_authorized": True,
            "provider_voice_creation_authorized": False,
            "provider_voice_deletion_authorized": False,
            "training_or_fine_tuning_authorized": False,
            "paralinguistic_ordinals_2_and_3_authorized": False,
            "publication_authorized": False,
            "rollout_authorized": False,
        },
        "planned_outputs": planned_outputs,
        "operator_only_public_pairing_plan": public_pairing_plan,
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    destination = root / OUTPUT_DIRECTORY / run_id
    return document, destination


def write_run(voice_root: Path, document: dict[str, Any], destination: Path) -> Path:
    root = base._absolute_lexical(voice_root)
    parent = _safe_directory(root, root / OUTPUT_DIRECTORY, label="challenger run root")
    _expect(destination.parent, parent, label="challenger destination parent")
    try:
        destination.mkdir()
    except FileExistsError as error:
        raise VoicePreferenceChallengerError("challenger run already exists") from error
    _safe_directory(root, destination / "audits", label="challenger audit directory")
    audio = _safe_directory(root, destination / "audio", label="challenger audio directory")
    for slug in blind.CHARACTER_ORDER:
        _safe_directory(root, audio / slug, label=f"{slug} challenger audio directory")
    _write_new(
        destination / "manifest.json", _pretty_json_bytes(document), label="challenger manifest"
    )
    _write_new(
        destination / "README.md",
        (
            b"# Private voice-preference challenger run\n\n"
            b"This directory contains private candidate mapping and Provider audit records. "
            b"Never copy it into a public review package. An attempt without a matching result "
            b"blocks automatic continuation.\n"
        ),
        label="challenger README",
    )
    return destination


def _load_workspace_id(root: Path, private_blind: dict[str, Any]) -> str:
    source = _object(private_blind.get("source_enrollment"), label="source enrollment")
    enrollment_run_id = _string(source.get("run_id"), label="source enrollment run ID")
    audit_directory = root / enrollment.AUDIT_DIRECTORY / enrollment_run_id
    base._require_safe_existing_path(
        root, audit_directory, label="source enrollment audit directory", directory=True
    )
    workspace_ids: set[str] = set()
    for path in sorted(audit_directory.glob("*-attempt.json")):
        record, _ = base._read_json(root, path, label="source enrollment attempt")
        if record.get("action") != "create":
            continue
        workspace = record.get("workspace_id")
        if isinstance(workspace, str) and enrollment.WORKSPACE_PATTERN.fullmatch(
            workspace.strip().lower()
        ):
            workspace_ids.add(workspace.strip().lower())
    _expect(len(workspace_ids), 1, label="source Beijing workspace count")
    return next(iter(workspace_ids))


def _load_run(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
    revalidate_sources: bool = True,
) -> tuple[Path, dict[str, Any], dict[str, str], str]:
    root = base._absolute_lexical(voice_root)
    safe_id = base._safe_identifier(run_id, RUN_ID_PATTERN, label="challenger run ID")
    directory = root / OUTPUT_DIRECTORY / safe_id
    base._require_safe_existing_path(root, directory, label="challenger run", directory=True)
    manifest, payload = base._read_json(root, directory / "manifest.json", label="challenger manifest")
    _expect(manifest.get("schema_version"), SCHEMA, label="challenger schema")
    _expect(manifest.get("run_id"), safe_id, label="challenger run ID")
    base._verify_semantic_hash(manifest, field="manifest_sha256", label="challenger manifest")
    _timestamp(manifest.get("prepared_at"), label="prepared_at")
    _expect(
        manifest.get("status"),
        "prepared_for_authorized_challenger_synthesis",
        label="challenger status",
    )
    if expected_manifest_byte_sha256 is not None:
        _expect(
            base._sha256_bytes(payload),
            base._require_sha256(
                expected_manifest_byte_sha256, label="expected challenger manifest byte SHA-256"
            ),
            label="challenger manifest byte SHA-256",
        )
    source_round = _object(manifest.get("source_round"), label="source round")
    round_id = _string(source_round.get("round_id"), label="source round ID")
    round_manifest, round_payload, receipt, receipt_payload, _ = _load_source_round(
        root, round_id
    )
    _expect(
        source_round.get("round_manifest_sha256"),
        round_manifest["manifest_sha256"],
        label="source round manifest SHA-256",
    )
    _expect(
        source_round.get("round_manifest_byte_sha256"),
        base._sha256_bytes(round_payload),
        label="source round manifest byte SHA-256",
    )
    _expect(
        source_round.get("decision_receipt_sha256"),
        receipt["receipt_sha256"],
        label="source decision receipt SHA-256",
    )
    _expect(
        source_round.get("decision_receipt_byte_sha256"),
        base._sha256_bytes(receipt_payload),
        label="source decision receipt byte SHA-256",
    )
    source_blind = _object(manifest.get("source_blind_test"), label="source blind test")
    blind_run_id = _string(source_blind.get("run_id"), label="source blind run ID")
    private_directory, private_blind, voices = blind.load_run(
        root,
        blind_run_id,
        expected_manifest_byte_sha256=_string(
            source_blind.get("manifest_byte_sha256"),
            label="source blind manifest byte SHA-256",
        ),
        revalidate_sources=revalidate_sources,
    )
    private_payload = base._read_stable_bytes(
        root, private_directory / "manifest.json", label="source blind manifest"
    )
    _expect(
        source_blind.get("manifest_sha256"),
        private_blind["manifest_sha256"],
        label="source blind manifest SHA-256",
    )
    _expect(
        source_blind.get("manifest_byte_sha256"),
        base._sha256_bytes(private_payload),
        label="source blind manifest byte SHA-256",
    )
    provider = _object(manifest.get("provider_contract"), label="provider contract")
    _expect(provider.get("region"), blind.REGION, label="provider region")
    _expect(provider.get("target_model"), blind.MODEL, label="provider model")
    _expect(
        provider.get("instruction_control_supported_by_target_model"),
        False,
        label="instruction-control support",
    )
    _expect(provider.get("instructions_sent"), False, label="instructions sent")
    policy_version = _string(manifest.get("policy_version"), label="challenger policy")
    expected_by_policy = {
        POLICY_VERSION: (
            EXPECTED_CURRENT_OUTPUT_COUNT,
            "same_lexical_text_punctuation_only",
        ),
        UNSEEN_POLICY_VERSION: (
            EXPECTED_UNSEEN_OUTPUT_COUNT,
            "unseen_text_dual_candidate_validation",
        ),
    }
    if policy_version not in expected_by_policy:
        raise VoicePreferenceChallengerError("unknown challenger policy")
    expected_output_count, expected_strategy = expected_by_policy[policy_version]
    _expect(provider.get("steering_scope"), expected_strategy, label="steering scope")
    pricing = _object(manifest.get("pricing_contract"), label="pricing contract")
    _expect(
        pricing.get("incremental_synthesis_cost_ceiling_usd"),
        str(INCREMENTAL_COST_CEILING_USD),
        label="challenger cost ceiling",
    )
    planned = _array(manifest.get("planned_outputs"), label="planned outputs")
    _expect(len(planned), expected_output_count, label="planned output count")
    _expect(
        pricing.get("planned_output_count"),
        expected_output_count,
        label="priced output count",
    )
    output_ids: set[str] = set()
    paths: set[str] = set()
    billable = 0
    for raw in planned:
        output = _object(raw, label="planned output")
        output_id = base._safe_identifier(
            output.get("output_id"), OUTPUT_ID_PATTERN, label="challenger output ID"
        )
        if output_id in output_ids:
            raise VoicePreferenceChallengerError("duplicate challenger output ID")
        output_ids.add(output_id)
        candidate = _string(output.get("candidate_key"), label="challenger candidate")
        if candidate not in voices:
            raise VoicePreferenceChallengerError("challenger references an unavailable voice")
        text = _string(output.get("text"), label="challenger text")
        _expect(
            output.get("delivery_strategy"),
            expected_strategy,
            label="challenger delivery strategy",
        )
        _expect(
            output.get("text_sha256"),
            base._sha256_bytes(text.encode("utf-8")),
            label="challenger text SHA-256",
        )
        calculated = blind._billing_character_count(text)
        _expect(
            output.get("billing_character_count"), calculated, label="challenger billing characters"
        )
        billable += calculated
        relative = base._safe_relative_path(
            output.get("private_audio_relative_path"), label="challenger audio path"
        )
        if relative.parts[:2] != ("audio", output.get("character_slug")) or relative.suffix != ".wav":
            raise VoicePreferenceChallengerError("challenger audio path violates the run layout")
        if relative.as_posix() in paths:
            raise VoicePreferenceChallengerError("duplicate challenger audio path")
        paths.add(relative.as_posix())
    _expect(
        pricing.get("planned_billable_input_characters"),
        billable,
        label="planned billable characters",
    )
    if blind._estimated_cost(billable, blind.PRICE_USD_PER_10K_CHARACTERS) > INCREMENTAL_COST_CEILING_USD:
        raise VoicePreferenceChallengerError("planned challenger cost exceeds the ceiling")
    pairings = _array(
        manifest.get("operator_only_public_pairing_plan"), label="public pairing plan"
    )
    _expect(len(pairings), EXPECTED_CASE_COUNT, label="public pairing case count")
    for relative, label in (("audits", "audit directory"), ("audio", "audio directory")):
        base._require_safe_existing_path(
            root, directory / relative, label=label, directory=True
        )
    workspace = _load_workspace_id(root, private_blind) if revalidate_sources else ""
    return directory, manifest, voices, workspace


def _write_audit(directory: Path, record: dict[str, Any], filename: str) -> Path:
    if not re.fullmatch(
        r"challenger-attempt-[0-9a-f]{32}-(?:attempt|result)\.json", filename
    ):
        raise VoicePreferenceChallengerError("invalid challenger audit filename")
    document = dict(record)
    document["record_sha256"] = base._semantic_sha256(document)
    return _write_new(
        directory / filename, _pretty_json_bytes(document), label="challenger audit"
    )


def _audit_state(root: Path, directory: Path, run_id: str) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        record, _ = base._read_json(root, path, label="challenger audit")
        _expect(record.get("schema_version"), AUDIT_SCHEMA, label="challenger audit schema")
        base._verify_semantic_hash(record, field="record_sha256", label="challenger audit")
        _expect(record.get("run_id"), run_id, label="challenger audit run ID")
        attempt_id = base._safe_identifier(
            record.get("attempt_id"), ATTEMPT_ID_PATTERN, label="challenger attempt ID"
        )
        stage = record.get("stage")
        target = attempts if stage == "attempt_started" else results if stage == "result_committed" else None
        if target is None or attempt_id in target:
            raise VoicePreferenceChallengerError("challenger audit history is ambiguous")
        target[attempt_id] = record
    if set(results) - set(attempts):
        raise VoicePreferenceChallengerError("challenger result lacks an attempt")
    pending = [item for key, item in attempts.items() if key not in results]
    by_output: dict[str, dict[str, Any]] = {}
    for attempt_id, result in results.items():
        _expect(result.get("outcome"), "audio_rendered", label="challenger result outcome")
        output_id = base._safe_identifier(
            result.get("output_id"), OUTPUT_ID_PATTERN, label="challenger result output ID"
        )
        _expect(
            attempts[attempt_id].get("output_id"), output_id, label="challenger attempt/result output"
        )
        if output_id in by_output:
            raise VoicePreferenceChallengerError("challenger output has multiple results")
        by_output[output_id] = result
    return {
        "attempts": attempts,
        "results": results,
        "pending": pending,
        "by_output": by_output,
    }


def _validate_result_audio(
    root: Path,
    directory: Path,
    output: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    _expect(
        result.get("private_audio_relative_path"),
        output["private_audio_relative_path"],
        label="challenger result audio path",
    )
    relative = base._safe_relative_path(
        output["private_audio_relative_path"], label="challenger audio path"
    )
    payload = base._read_stable_bytes(
        root, directory.joinpath(*relative.parts), label="challenger audio"
    )
    metrics = blind._validate_wav_bytes(payload)
    _expect(metrics, result.get("audio_metrics"), label="challenger audio metrics")
    return metrics


def validate_run(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
    revalidate_sources: bool = True,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, _, _ = _load_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=revalidate_sources,
    )
    state = _audit_state(root, directory / "audits", run_id)
    planned = {item["output_id"]: item for item in manifest["planned_outputs"]}
    if not set(state["by_output"]).issubset(planned):
        raise VoicePreferenceChallengerError("audit references an unplanned output")
    full_scale = 0
    for output_id, result in state["by_output"].items():
        full_scale += _validate_result_audio(
            root, directory, planned[output_id], result
        )["full_scale_sample_count"]
    usage = sum(
        int(item.get("provider_usage_characters") or 0)
        for item in state["by_output"].values()
    )
    estimated = blind._estimated_cost(usage, blind.PRICE_USD_PER_10K_CHARACTERS)
    if estimated > INCREMENTAL_COST_CEILING_USD:
        raise VoicePreferenceChallengerError("completed challenger cost exceeds the ceiling")
    return {
        "status": "valid",
        "run_id": run_id,
        "path": str(directory),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_byte_sha256": base._sha256_bytes(
            base._read_stable_bytes(root, directory / "manifest.json", label="challenger manifest")
        ),
        "planned_output_count": len(planned),
        "successful_output_count": len(state["by_output"]),
        "pending_attempt_count": len(state["pending"]),
        "provider_usage_characters": usage,
        "estimated_incremental_cost_usd": str(estimated),
        "full_scale_sample_count": full_scale,
        "review_ready": len(state["by_output"]) == len(planned) and not state["pending"],
    }


def render_one(
    voice_root: Path,
    run_id: str,
    output_id: str,
    *,
    dotenv_file: Path | None,
    expected_manifest_byte_sha256: str | None,
    confirm_run_id: str,
    confirm_output_id: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_synthesis_and_local_review_authorized: bool,
    confirm_instruction_control_unavailable: bool,
    confirm_paralinguistic_ordinals_excluded: bool,
    provider: Callable[..., tuple[bytes, dict[str, Any]]] = blind.provider_synthesize_pcm,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, voices, workspace_id = _load_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    _expect(confirm_run_id, run_id, label="confirmed run ID")
    _expect(confirm_output_id, output_id, label="confirmed output ID")
    _expect(confirm_model, blind.MODEL, label="confirmed model")
    _expect(confirm_region, blind.REGION, label="confirmed region")
    _expect(
        Decimal(confirm_cost_ceiling_usd),
        INCREMENTAL_COST_CEILING_USD,
        label="confirmed cost ceiling",
    )
    if not confirm_synthesis_and_local_review_authorized:
        raise VoicePreferenceChallengerError("challenger synthesis requires authorization")
    if not confirm_instruction_control_unavailable:
        raise VoicePreferenceChallengerError("VC instruction-control limitation must be confirmed")
    if not confirm_paralinguistic_ordinals_excluded:
        raise VoicePreferenceChallengerError("paralinguistic ordinals 2/3 must remain excluded")
    matches = [item for item in manifest["planned_outputs"] if item["output_id"] == output_id]
    _expect(len(matches), 1, label="planned challenger output count")
    output = matches[0]
    state = _audit_state(root, directory / "audits", run_id)
    if state["pending"]:
        raise VoicePreferenceChallengerError(
            "an uncertain challenger attempt exists; do not retry automatically"
        )
    if output_id in state["by_output"]:
        raise VoicePreferenceChallengerError("challenger output already rendered")
    completed_usage = sum(
        int(item.get("provider_usage_characters") or 0)
        for item in state["by_output"].values()
    )
    projected = completed_usage + int(output["billing_character_count"])
    if blind._estimated_cost(projected, blind.PRICE_USD_PER_10K_CHARACTERS) > INCREMENTAL_COST_CEILING_USD:
        raise VoicePreferenceChallengerError("challenger cost ceiling would be exceeded")
    effective_dotenv = dotenv_file
    if effective_dotenv is None:
        effective_dotenv = root.parent.parent / "App" / ".env"
    api_key = enrollment._read_secret(
        None,
        voice_root=root,
        region=blind.REGION,
        dotenv_file=effective_dotenv,
    )
    voice_id = voices[output["candidate_key"]]
    attempt_id = f"challenger-attempt-{uuid.uuid4().hex}"
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
        "text_sha256": output["text_sha256"],
        "billing_character_count": output["billing_character_count"],
        "provider_voice_id_sha256": base._sha256_bytes(voice_id.encode("utf-8")),
        "workspace_id_sha256": base._sha256_bytes(workspace_id.encode("utf-8")),
        "target_model": blind.MODEL,
        "region": blind.REGION,
        "instructions_sent": False,
        "delivery_strategy": output["delivery_strategy"],
        "private_audio_relative_path": output["private_audio_relative_path"],
        "incremental_synthesis_cost_ceiling_usd": str(INCREMENTAL_COST_CEILING_USD),
        "paralinguistic_ordinals_2_and_3_included": False,
    }
    attempt_path = _write_audit(
        directory / "audits", attempt, f"{attempt_id}-attempt.json"
    )
    try:
        pcm, metadata = provider(
            api_key=api_key,
            workspace_id=workspace_id,
            voice_id=voice_id,
            text=output["text"],
        )
        wav, metrics = blind._pcm_to_wav(pcm)
        relative = base._safe_relative_path(
            output["private_audio_relative_path"], label="challenger audio path"
        )
        audio_path = directory.joinpath(*relative.parts)
        blind._write_atomic_new(audio_path, wav, label="challenger audio")
        verified = blind._validate_wav_bytes(
            base._read_stable_bytes(root, audio_path, label="challenger audio"), metrics
        )
    except Exception as error:
        if isinstance(error, VoicePreferenceChallengerError):
            raise
        raise VoicePreferenceChallengerError(
            "challenger synthesis failed after attempt commit; do not retry automatically"
        ) from error
    usage = metadata.get("provider_usage_characters")
    if not isinstance(usage, int) or isinstance(usage, bool) or usage < 0:
        usage = int(output["billing_character_count"])
        usage_basis = "official_billing_rule_fallback_provider_usage_missing"
    else:
        usage_basis = "provider_response_done_usage_characters"
    maximum = int(
        (
            INCREMENTAL_COST_CEILING_USD
            * Decimal(10_000)
            / blind.PRICE_USD_PER_10K_CHARACTERS
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    if completed_usage + usage > maximum:
        raise VoicePreferenceChallengerError(
            "provider usage exceeded the fixed ceiling after audio commit; stop the run"
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
        "outcome": "audio_rendered",
        "target_model": blind.MODEL,
        "provider_session_id": metadata["session_id"],
        "provider_response_id": metadata["response_id"],
        "provider_usage_characters": usage,
        "usage_basis": usage_basis,
        "estimated_provider_cost_usd": str(
            blind._estimated_cost(usage, blind.PRICE_USD_PER_10K_CHARACTERS)
        ),
        "actual_provider_charge_usd": None,
        "charge_status": "unknown_until_provider_billing_reconciliation",
        "private_audio_relative_path": output["private_audio_relative_path"],
        "audio_metrics": verified,
        "attempt_record_relative_path": attempt_path.name,
    }
    result_path = _write_audit(
        directory / "audits", result, f"{attempt_id}-result.json"
    )
    return {
        "status": "challenger_audio_rendered",
        "run_id": run_id,
        "output_id": output_id,
        "case_id": output["case_id"],
        "duration_seconds": verified["duration_seconds"],
        "provider_usage_characters": usage,
        "audio_path": str(audio_path),
        "result_audit_path": str(result_path),
    }


def render_all(
    voice_root: Path,
    run_id: str,
    *,
    dotenv_file: Path | None,
    expected_manifest_byte_sha256: str | None,
    confirm_run_id: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_synthesis_and_local_review_authorized: bool,
    confirm_instruction_control_unavailable: bool,
    confirm_paralinguistic_ordinals_excluded: bool,
    provider: Callable[..., tuple[bytes, dict[str, Any]]] = blind.provider_synthesize_pcm,
) -> dict[str, Any]:
    _expect(confirm_run_id, run_id, label="confirmed run ID")
    rendered = []
    while True:
        root = base._absolute_lexical(voice_root)
        directory, manifest, _, _ = _load_run(
            root,
            run_id,
            expected_manifest_byte_sha256=expected_manifest_byte_sha256,
            revalidate_sources=True,
        )
        state = _audit_state(root, directory / "audits", run_id)
        if state["pending"]:
            raise VoicePreferenceChallengerError(
                "an uncertain challenger attempt exists; remaining outputs were not submitted"
            )
        next_output = next(
            (
                item
                for item in manifest["planned_outputs"]
                if item["output_id"] not in state["by_output"]
            ),
            None,
        )
        if next_output is None:
            break
        rendered.append(
            render_one(
                root,
                run_id,
                next_output["output_id"],
                dotenv_file=dotenv_file,
                expected_manifest_byte_sha256=expected_manifest_byte_sha256,
                confirm_run_id=run_id,
                confirm_output_id=next_output["output_id"],
                confirm_model=confirm_model,
                confirm_region=confirm_region,
                confirm_cost_ceiling_usd=confirm_cost_ceiling_usd,
                confirm_synthesis_and_local_review_authorized=(
                    confirm_synthesis_and_local_review_authorized
                ),
                confirm_instruction_control_unavailable=(
                    confirm_instruction_control_unavailable
                ),
                confirm_paralinguistic_ordinals_excluded=(
                    confirm_paralinguistic_ordinals_excluded
                ),
                provider=provider,
            )
        )
    validation = validate_run(
        voice_root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    return {
        "status": "challenger_render_batch_complete",
        "run_id": run_id,
        "rendered_in_this_command": len(rendered),
        **{
            key: validation[key]
            for key in (
                "successful_output_count",
                "planned_output_count",
                "pending_attempt_count",
                "provider_usage_characters",
                "estimated_incremental_cost_usd",
                "review_ready",
            )
        },
    }


def _public_sample_from_plan(
    root: Path,
    run_directory: Path,
    run_manifest: dict[str, Any],
    source_review_directory: Path,
    source_case: dict[str, Any],
    raw_plan: dict[str, Any],
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    plan = _object(raw_plan, label="public sample plan")
    origin = _string(plan.get("origin"), label="public sample origin")
    candidate = _string(plan.get("candidate_key"), label="public sample candidate")
    if origin == "prior_relative_winner":
        prior_label = _string(
            plan.get("prior_opaque_label_id"), label="prior opaque label"
        )
        prior = _find_prior_sample(source_case, prior_label)
        relative = base._safe_relative_path(
            prior.get("audio_relative_path"), label="prior audio path"
        )
        payload = base._read_stable_bytes(
            root,
            source_review_directory.joinpath(*relative.parts),
            label="prior winner audio",
        )
        _expect(
            base._sha256_bytes(payload), plan.get("prior_wav_sha256"), label="prior winner WAV"
        )
    elif origin == "new_challenger":
        output_id = base._safe_identifier(
            plan.get("output_id"), OUTPUT_ID_PATTERN, label="public challenger output ID"
        )
        matches = [item for item in run_manifest["planned_outputs"] if item["output_id"] == output_id]
        _expect(len(matches), 1, label="public challenger output count")
        output = matches[0]
        _expect(output.get("candidate_key"), candidate, label="public challenger candidate")
        relative = base._safe_relative_path(
            output.get("private_audio_relative_path"), label="challenger audio path"
        )
        payload = base._read_stable_bytes(
            root, run_directory.joinpath(*relative.parts), label="challenger audio"
        )
    else:
        raise VoicePreferenceChallengerError("unknown public sample origin")
    metrics = blind._validate_wav_bytes(payload)
    opaque = base._safe_identifier(
        plan.get("opaque_label_id"), blind.OPAQUE_ID_PATTERN, label="public opaque label"
    )
    public = {
        "display_order": int(plan["display_order"]),
        "opaque_label_id": opaque,
        "display_label": _string(plan.get("display_label"), label="public display label"),
        "audio_relative_path": "",
        "duration_seconds": metrics["duration_seconds"],
        "wav_sha256": metrics["wav_sha256"],
        "full_scale_sample_count": metrics["full_scale_sample_count"],
    }
    private_map = {
        "opaque_label_id": opaque,
        "candidate_key": candidate,
        "origin": origin,
    }
    return public, payload, private_map


def finalize_review(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    validation = validate_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    if not validation["review_ready"]:
        raise VoicePreferenceChallengerError("challenger run is not ready for review")
    run_directory, run_manifest, voices, _ = _load_run(
        root,
        run_id,
        expected_manifest_byte_sha256=expected_manifest_byte_sha256,
        revalidate_sources=True,
    )
    source_round_id = run_manifest["source_round"]["round_id"]
    source_manifest, _, receipt, _, source_review_directory = _load_source_round(
        root, source_round_id
    )
    source_cases = {
        item["case_id"]: _object(item, label="source case")
        for item in _array(source_manifest.get("cases"), label="source cases")
    }
    audio_payloads: dict[str, bytes] = {}
    cases: list[dict[str, Any]] = []
    private_entries: list[dict[str, Any]] = []
    pairings = _array(
        run_manifest.get("operator_only_public_pairing_plan"), label="public pairings"
    )
    run_strategy = _string(
        _object(run_manifest.get("provider_contract"), label="provider contract").get(
            "steering_scope"
        ),
        label="run steering scope",
    )
    for pairing in pairings:
        pairing_object = _object(pairing, label="public pairing")
        case_id = _string(pairing_object.get("case_id"), label="public pairing case ID")
        source_case = source_cases[case_id]
        sequence = int(pairing_object["sequence"])
        slug = _string(pairing_object.get("character_slug"), label="public pairing character")
        if run_strategy == "unseen_text_dual_candidate_validation":
            text_values = {
                _string(item.get("text"), label=f"{case_id} validation text")
                for item in run_manifest["planned_outputs"]
                if item.get("case_id") == case_id
            }
            _expect(len(text_values), 1, label=f"{case_id} validation text count")
            public_text_value = next(iter(text_values))
        else:
            public_text_value = source_case["text"]
        public_samples = []
        mapped_samples = []
        for raw_sample in _array(pairing_object.get("samples"), label="public pair samples"):
            public, payload, private_map = _public_sample_from_plan(
                root,
                run_directory,
                run_manifest,
                source_review_directory,
                source_case,
                raw_sample,
            )
            relative = f"audio/{slug}/{sequence:02d}-{public['opaque_label_id']}.wav"
            public["audio_relative_path"] = relative
            if relative in audio_payloads:
                raise VoicePreferenceChallengerError("duplicate public review audio path")
            audio_payloads[relative] = payload
            public_samples.append(public)
            mapped_samples.append(private_map)
        public_samples.sort(key=lambda item: item["display_order"])
        _expect(len(public_samples), 2, label=f"{case_id} public sample count")
        cases.append(
            {
                "sequence": sequence,
                "character_slug": slug,
                "runtime_character_name": source_case["runtime_character_name"],
                "case_id": case_id,
                "source_case_index": source_case["source_case_index"],
                "style_anchor": source_case["style_anchor"],
                "text": public_text_value,
                "samples": public_samples,
            }
        )
        private_entries.append(
            {
                "case_id": case_id,
                "character_slug": slug,
                "samples": mapped_samples,
            }
        )
    cases.sort(key=lambda item: item["sequence"])
    identity = {
        "policy_version": tournament.POLICY_VERSION,
        "source_round_id": source_round_id,
        "source_decision_set_sha256": receipt["decision_set_sha256"],
        "challenger_run_manifest_sha256": run_manifest["manifest_sha256"],
        "round_index": int(source_manifest["round_index"]) + 1,
    }
    round_id = f"voice-preference-round-{base._semantic_sha256(identity)[:20]}"
    actual_cost = validation["estimated_incremental_cost_usd"]
    document: dict[str, Any] = {
        "schema_version": tournament.SCHEMA,
        "decision_submission_schema_version": tournament.SUBMISSION_SCHEMA,
        "policy_version": tournament.POLICY_VERSION,
        "round_id": round_id,
        "round_index": int(source_manifest["round_index"]) + 1,
        "prepared_at": run_manifest["prepared_at"],
        "status": "awaiting_local_pairwise_rejection_decisions",
        "source": {
            "previous_round_id": source_round_id,
            "previous_round_manifest_sha256": source_manifest["manifest_sha256"],
            "previous_decision_receipt_sha256": receipt["receipt_sha256"],
            "previous_decision_set_sha256": receipt["decision_set_sha256"],
            "blind_test_run_id": run_manifest["source_blind_test"]["run_id"],
            "challenger_run_manifest_sha256": run_manifest["manifest_sha256"],
        },
        "privacy_contract": {
            "candidate_mapping_included": False,
            "provider_voice_ids_included": False,
            "candidate_a_b_labels_included": False,
            "workspace_id_included": False,
            "local_review_only": True,
            "publication_authorized": False,
        },
        "generation_contract": {
            "provider_calls_performed_for_this_round": True,
            "new_synthesis_outputs_created": len(run_manifest["planned_outputs"]),
            "reused_existing_blind_outputs": sum(
                sample["origin"] == "prior_relative_winner"
                for item in pairings
                for sample in item["samples"]
            ),
            "provider_usage_characters": validation["provider_usage_characters"],
            "incremental_provider_cost_usd": actual_cost,
            "incremental_provider_cost_ceiling_usd": str(
                INCREMENTAL_COST_CEILING_USD
            ),
            "instruction_control_supported_by_target_model": False,
            "instructions_sent": False,
            "steering_scope": run_strategy,
            "unseen_validation_text": (
                run_strategy == "unseen_text_dual_candidate_validation"
            ),
            "next_provider_generation_requires_complete_human_submission": True,
            "provider_learns_from_rejection": False,
        },
        "decision_contract": source_manifest["decision_contract"],
        "paralinguistic_event_lane": source_manifest["paralinguistic_event_lane"],
        "cases": cases,
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    public_text = (
        _pretty_json_bytes(document)
        + tournament._review_html(document).encode("utf-8")
    ).decode("utf-8")
    for private_value in (*blind.CANDIDATE_ORDER, *voices.values()):
        if json.dumps(private_value, ensure_ascii=False) in public_text:
            raise VoicePreferenceChallengerError(
                "public challenger review contains private operator data"
            )
    destination = root / tournament.OUTPUT_DIRECTORY / round_id / "review"
    write_result = tournament.write_round(root, document, audio_payloads, destination)
    operator_directory = _safe_directory(
        root,
        root / tournament.OUTPUT_DIRECTORY / round_id / "operator",
        label="round-two operator directory",
    )
    candidate_map: dict[str, Any] = {
        "schema_version": MAP_SCHEMA,
        "round_id": round_id,
        "source_challenger_run_id": run_id,
        "source_challenger_manifest_sha256": run_manifest["manifest_sha256"],
        "entries": private_entries,
    }
    candidate_map["manifest_sha256"] = base._semantic_sha256(candidate_map)
    map_state = _write_or_verify(
        root,
        operator_directory / "candidate-map.json",
        _pretty_json_bytes(candidate_map),
        label="round candidate map",
    )
    round_validation = tournament.validate_round(root, round_id)
    return {
        "status": "awaiting_local_pairwise_rejection_decisions",
        "write_status": write_result["write_status"],
        "candidate_map_write_status": map_state,
        "round_id": round_id,
        "review_html_path": str(destination / "review.html"),
        "review_manifest_path": str(destination / "manifest.json"),
        "review_manifest_sha256": document["manifest_sha256"],
        "review_manifest_byte_sha256": round_validation["manifest_byte_sha256"],
        "case_count": len(cases),
        "audio_count": len(audio_payloads),
        "new_provider_outputs": len(run_manifest["planned_outputs"]),
        "reused_incumbent_outputs": document["generation_contract"][
            "reused_existing_blind_outputs"
        ],
        "provider_usage_characters": validation["provider_usage_characters"],
        "incremental_provider_cost_usd": actual_cost,
        "incremental_provider_cost_ceiling_usd": str(INCREMENTAL_COST_CEILING_USD),
        "numeric_scoring_used": False,
    }


def build_terminal_conclusion(
    voice_root: Path,
    round_id: str,
    *,
    concluded_at: str,
) -> tuple[dict[str, Any], Path]:
    """Resolve an unseen-text terminal round without scheduling more synthesis."""

    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    timestamp = _timestamp(concluded_at, label="concluded_at")
    tournament.validate_round(root, round_id)
    receipt_validation = tournament.validate_decision_receipt(root, round_id)
    round_manifest, round_payload, _ = tournament._load_round_manifest(root, round_id)
    generation = _object(
        round_manifest.get("generation_contract"), label="round generation contract"
    )
    _expect(
        generation.get("unseen_validation_text"),
        True,
        label="terminal unseen-validation gate",
    )
    _expect(
        generation.get("steering_scope"),
        "unseen_text_dual_candidate_validation",
        label="terminal validation strategy",
    )

    operator_directory = root / tournament.OUTPUT_DIRECTORY / round_id / "operator"
    base._require_safe_existing_path(
        root, operator_directory, label="round operator directory", directory=True
    )
    receipt, receipt_payload = base._read_json(
        root,
        operator_directory / "decision-receipt.json",
        label="terminal source decision receipt",
    )
    candidate_map, candidate_map_payload = base._read_json(
        root,
        operator_directory / "candidate-map.json",
        label="terminal candidate map",
    )
    _expect(candidate_map.get("schema_version"), MAP_SCHEMA, label="candidate-map schema")
    _expect(candidate_map.get("round_id"), round_id, label="candidate-map round ID")
    candidate_map_sha256 = base._verify_semantic_hash(
        candidate_map, field="manifest_sha256", label="candidate map"
    )
    source = _object(round_manifest.get("source"), label="round source")
    _expect(
        candidate_map.get("source_challenger_manifest_sha256"),
        source.get("challenger_run_manifest_sha256"),
        label="candidate-map challenger manifest SHA-256",
    )

    cases = {
        _string(item.get("case_id"), label="round case ID"): _object(
            item, label="round case"
        )
        for item in _array(round_manifest.get("cases"), label="round cases")
    }
    map_entries = {
        _string(item.get("case_id"), label="candidate-map case ID"): _object(
            item, label="candidate-map entry"
        )
        for item in _array(candidate_map.get("entries"), label="candidate-map entries")
    }
    _expect(set(map_entries), set(cases), label="candidate-map case IDs")
    decisions = _object(receipt.get("decisions"), label="terminal decisions")
    _expect(set(decisions), set(cases), label="terminal decision case IDs")

    slots: list[dict[str, Any]] = []
    locked_count = 0
    paused_case_ids: list[str] = []
    for case in sorted(cases.values(), key=lambda item: int(item["sequence"])):
        case_id = _string(case.get("case_id"), label="terminal case ID")
        character_slug = _string(
            case.get("character_slug"), label=f"{case_id} character slug"
        )
        entry = map_entries[case_id]
        _expect(
            entry.get("character_slug"), character_slug, label=f"{case_id} character"
        )
        public_labels = {
            _string(sample.get("opaque_label_id"), label=f"{case_id} public label")
            for sample in _array(case.get("samples"), label=f"{case_id} public samples")
        }
        private_by_label: dict[str, str] = {}
        for raw_sample in _array(entry.get("samples"), label=f"{case_id} mapped samples"):
            sample = _object(raw_sample, label=f"{case_id} mapped sample")
            label = _string(
                sample.get("opaque_label_id"), label=f"{case_id} mapped label"
            )
            candidate = _string(
                sample.get("candidate_key"), label=f"{case_id} candidate reference"
            )
            if label in private_by_label:
                raise VoicePreferenceChallengerError(
                    f"{case_id} candidate map contains a duplicate label"
                )
            if candidate not in blind.CANDIDATE_ORDER:
                raise VoicePreferenceChallengerError(
                    f"{case_id} candidate map contains an unknown candidate"
                )
            _expect(
                sample.get("origin"),
                "new_challenger",
                label=f"{case_id} terminal sample origin",
            )
            private_by_label[label] = candidate
        _expect(set(private_by_label), public_labels, label=f"{case_id} mapped labels")

        decision = _object(decisions[case_id], label=f"{case_id} terminal decision")
        selected_label = decision.get("selected_opaque_label_id")
        relative_candidate = (
            private_by_label.get(selected_label) if isinstance(selected_label, str) else None
        )
        if isinstance(selected_label, str) and relative_candidate is None:
            raise VoicePreferenceChallengerError(
                f"{case_id} selected label is absent from the private candidate map"
            )
        usable = decision.get("winning_sample_usable") == "usable"
        if usable:
            if relative_candidate is None:
                raise VoicePreferenceChallengerError(
                    f"{case_id} usable decision has no selected candidate"
                )
            disposition = "locked_for_slot"
            runtime_candidate = relative_candidate
            locked_count += 1
        else:
            disposition = "paused_not_qualified"
            runtime_candidate = None
            paused_case_ids.append(case_id)
        slots.append(
            {
                "sequence": int(case["sequence"]),
                "case_id": case_id,
                "character_slug": character_slug,
                "runtime_character_name": _string(
                    case.get("runtime_character_name"),
                    label=f"{case_id} runtime character name",
                ),
                "style_anchor": _string(
                    case.get("style_anchor"), label=f"{case_id} style anchor"
                ),
                "disposition": disposition,
                "runtime_candidate_ref": runtime_candidate,
                "relative_preference_candidate_ref": relative_candidate,
                "rejection_reasons": list(
                    _array(
                        decision.get("rejection_reasons"),
                        label=f"{case_id} rejection reasons",
                    )
                ),
            }
        )

    identity = {
        "policy_version": TERMINAL_POLICY_VERSION,
        "round_id": round_id,
        "decision_receipt_sha256": receipt_validation["receipt_sha256"],
        "candidate_map_sha256": candidate_map_sha256,
    }
    conclusion_id = (
        "voice-preference-terminal-conclusion-"
        + base._semantic_sha256(identity)[:20]
    )
    document: dict[str, Any] = {
        "schema_version": TERMINAL_CONCLUSION_SCHEMA,
        "policy_version": TERMINAL_POLICY_VERSION,
        "conclusion_id": conclusion_id,
        "concluded_at": timestamp,
        "status": "terminal_current_clone_pool_concluded",
        "source": {
            "round_id": round_id,
            "round_manifest_sha256": round_manifest["manifest_sha256"],
            "round_manifest_byte_sha256": base._sha256_bytes(round_payload),
            "decision_receipt_sha256": receipt_validation["receipt_sha256"],
            "decision_receipt_byte_sha256": base._sha256_bytes(receipt_payload),
            "decision_set_sha256": receipt_validation["decision_set_sha256"],
            "candidate_map_sha256": candidate_map_sha256,
            "candidate_map_byte_sha256": base._sha256_bytes(candidate_map_payload),
        },
        "terminal_contract": {
            "terminal_for_current_existing_clone_pool": True,
            "automatic_additional_pairwise_rounds_allowed": False,
            "paused_slots_trigger_resampling": False,
            "reopen_requires": (
                "new_source_material_or_new_model_or_explicit_user_instruction"
            ),
            "provider_calls_performed": False,
            "incremental_provider_cost_usd": "0",
        },
        "summary": {
            "slot_count": len(slots),
            "locked_slot_count": locked_count,
            "paused_slot_count": len(paused_case_ids),
            "paused_case_ids": paused_case_ids,
        },
        "slots": slots,
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    return document, operator_directory / "terminal-conclusion.json"


def write_terminal_conclusion(
    voice_root: Path, document: dict[str, Any], destination: Path
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    state = _write_or_verify(
        root,
        destination,
        _pretty_json_bytes(document),
        label="terminal conclusion",
    )
    summary = _object(document.get("summary"), label="terminal conclusion summary")
    return {
        "status": document["status"],
        "write_status": state,
        "conclusion_id": document["conclusion_id"],
        "conclusion_path": str(destination),
        "manifest_sha256": document["manifest_sha256"],
        "slot_count": int(summary["slot_count"]),
        "locked_slot_count": int(summary["locked_slot_count"]),
        "paused_slot_count": int(summary["paused_slot_count"]),
        "paused_case_ids": list(summary["paused_case_ids"]),
        "automatic_additional_pairwise_rounds_allowed": False,
        "provider_calls_performed": False,
        "incremental_provider_cost_usd": "0",
        "private_candidate_refs_exposed_in_result": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", type=Path, required=True)
    parser.add_argument("--dotenv-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare one private challenger run")
    prepare.add_argument("--source-round-id", default=DEFAULT_SOURCE_ROUND_ID)
    prepare.add_argument(
        "--expect-source-blind-manifest-byte-sha256",
        default=DEFAULT_SOURCE_BLIND_MANIFEST_BYTE_SHA256,
    )
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--confirm-synthesis-only", action="store_true")
    prepare.add_argument("--execute", action="store_true")
    validate = commands.add_parser("validate", help="validate a challenger run")
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--expect-run-manifest-byte-sha256")
    render = commands.add_parser("render-all", help="render all remaining challengers")
    render.add_argument("--run-id", required=True)
    render.add_argument("--expect-run-manifest-byte-sha256")
    render.add_argument("--confirm-run-id", default="")
    render.add_argument("--confirm-model", default="")
    render.add_argument("--confirm-region", default="")
    render.add_argument("--confirm-cost-ceiling-usd", default="")
    render.add_argument(
        "--confirm-synthesis-and-local-review-authorized", action="store_true"
    )
    render.add_argument("--confirm-instruction-control-unavailable", action="store_true")
    render.add_argument("--confirm-paralinguistic-ordinals-excluded", action="store_true")
    render.add_argument("--execute", action="store_true")
    finalize = commands.add_parser("finalize", help="build the next public preference round")
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--expect-run-manifest-byte-sha256")
    finalize.add_argument("--execute", action="store_true")
    conclude = commands.add_parser(
        "conclude", help="lock or pause every slot in a terminal unseen-text round"
    )
    conclude.add_argument("--round-id", required=True)
    conclude.add_argument("--concluded-at", required=True)
    conclude.add_argument("--execute", action="store_true")
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
                raise VoicePreferenceChallengerError(
                    "--execute requires --confirm-synthesis-only"
                )
            manifest, destination = build_run(
                arguments.voice_root,
                source_round_id=arguments.source_round_id,
                source_blind_manifest_byte_sha256=(
                    arguments.expect_source_blind_manifest_byte_sha256
                ),
                prepared_at=arguments.prepared_at,
            )
            if arguments.execute:
                write_run(arguments.voice_root, manifest, destination)
            result = {
                "status": "prepared" if arguments.execute else "dry_run",
                "run_id": manifest["run_id"],
                "path": str(destination),
                "manifest_sha256": manifest["manifest_sha256"],
                "manifest_byte_sha256": base._sha256_bytes(_pretty_json_bytes(manifest)),
                "planned_output_count": len(manifest["planned_outputs"]),
                "planned_billable_input_characters": manifest["pricing_contract"][
                    "planned_billable_input_characters"
                ],
                "estimated_incremental_cost_usd": manifest["pricing_contract"][
                    "estimated_incremental_cost_usd"
                ],
                "incremental_cost_ceiling_usd": str(INCREMENTAL_COST_CEILING_USD),
                "credentials_read": False,
                "network_calls_performed": False,
            }
        elif arguments.command == "validate":
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
        elif arguments.command == "render-all" and not arguments.execute:
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
            result["note"] = "render-all remains offline unless --execute is present"
        elif arguments.command == "render-all":
            result = render_all(
                arguments.voice_root,
                arguments.run_id,
                dotenv_file=arguments.dotenv_file,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
                confirm_run_id=arguments.confirm_run_id,
                confirm_model=arguments.confirm_model,
                confirm_region=arguments.confirm_region,
                confirm_cost_ceiling_usd=arguments.confirm_cost_ceiling_usd,
                confirm_synthesis_and_local_review_authorized=(
                    arguments.confirm_synthesis_and_local_review_authorized
                ),
                confirm_instruction_control_unavailable=(
                    arguments.confirm_instruction_control_unavailable
                ),
                confirm_paralinguistic_ordinals_excluded=(
                    arguments.confirm_paralinguistic_ordinals_excluded
                ),
            )
        elif arguments.command == "conclude":
            document, destination = build_terminal_conclusion(
                arguments.voice_root,
                arguments.round_id,
                concluded_at=arguments.concluded_at,
            )
            if arguments.execute:
                result = write_terminal_conclusion(
                    arguments.voice_root, document, destination
                )
            else:
                summary = document["summary"]
                result = {
                    "status": "dry_run_terminal_current_clone_pool_conclusion",
                    "conclusion_id": document["conclusion_id"],
                    "conclusion_path": str(destination),
                    "manifest_sha256": document["manifest_sha256"],
                    "slot_count": summary["slot_count"],
                    "locked_slot_count": summary["locked_slot_count"],
                    "paused_slot_count": summary["paused_slot_count"],
                    "paused_case_ids": summary["paused_case_ids"],
                    "automatic_additional_pairwise_rounds_allowed": False,
                    "provider_calls_performed": False,
                    "incremental_provider_cost_usd": "0",
                    "private_candidate_refs_exposed_in_result": False,
                }
        elif not arguments.execute:
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
            result["note"] = "finalize remains a dry run unless --execute is present"
        else:
            result = finalize_review(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_run_manifest_byte_sha256,
            )
    except (
        OSError,
        ValueError,
        VoicePreferenceChallengerError,
        tournament.VoicePreferenceTournamentError,
        blind.VoiceProviderBlindTestError,
    ) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
