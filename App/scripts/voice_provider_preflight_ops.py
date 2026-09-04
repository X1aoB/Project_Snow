"""Build an immutable, offline-only provider enrollment preflight package.

The command verifies the approved Vidya/Chenxing A/B selections and the
paralinguistic-event receipt, then writes references and a blind-test plan. It
does not read credentials, call a provider, create a voice, or authorize cost.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_target_ab_review_ops as review
else:
    import voice_paralinguistic_ops as base
    import voice_target_ab_review_ops as review


SCHEMA = "project-snow-private-local-voice-provider-preflight-1"
PLAN_SCHEMA = "project-snow-private-local-voice-blind-test-plan-1"
POLICY_VERSION = "project-snow-offline-voice-enrollment-preflight-1"
BLIND_TEST_POLICY_VERSION = "project-snow-voice-ab-blind-test-1"
OUTPUT_DIRECTORY = "tts_provider_preflights"
PROVIDER_FAMILY = "dashscope_qwen_custom_voice"
DEFAULT_REVIEW_ID = "voice-target-ab-review-9afda082d22702b0adb7"
DEFAULT_PARALINGUISTIC_REVIEW_ID = "voice-paralinguistic-review-92ca0d77e99048d16d66"
PREFLIGHT_ID_PATTERN = re.compile(r"voice-provider-preflight-[0-9a-f]{20}\Z")
CHARACTERS = {
    "5157b8972632": {"name": "薇蒂雅", "slug": "vidya", "runtime_slug": "wdy"},
    "98322bd505f4": {
        "name": "辰星",
        "slug": "chenxing",
        "runtime_slug": "cx",
    },
}
CHARACTER_ORDER = {"5157b8972632": 0, "98322bd505f4": 1}
SCOPE_LIMIT_KEYS = (
    "training_use_approved",
    "voice_cloning_approved",
    "rights_accepted",
    "provider_enrollment_allowed",
    "credential_access_allowed",
    "cost_authorized",
    "paralinguistic_event_bank_approved",
    "publication_approved",
    "public_rollout_allowed",
)
REQUIRED_AUTHORIZATIONS = (
    "source_audio_rights_and_derived_voice_consent",
    "provider_terms_and_voice_cloning_consent",
    "credential_access_and_target_workspace",
    "provider_model_region_retention_and_privacy_choice",
    "explicit_cost_ceiling",
)


class VoiceProviderPreflightError(base.VoiceParalinguisticError):
    """Raised when an offline preflight cannot be proven fail-closed."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceProviderPreflightError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _require_object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceProviderPreflightError(f"{label} must be an object")
    return value


def _require_array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceProviderPreflightError(f"{label} must be an array")
    return value


def _scope_limits() -> dict[str, bool]:
    return {key: False for key in SCOPE_LIMIT_KEYS}


def _verify_scope_limits(value: Any, *, label: str) -> dict[str, bool]:
    limits = base._require_all_false(value, label=label)
    _expect(tuple(limits), SCOPE_LIMIT_KEYS, label=f"{label} keys")
    return limits


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _load_review(
    root: Path,
    review_id: str,
    *,
    expected_byte_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    validation = review.validate_review_receipt(root, review_id)
    if expected_byte_sha256 is not None:
        _expect(
            validation["byte_sha256"],
            base._require_sha256(expected_byte_sha256, label="expected source review byte SHA-256"),
            label="source review byte SHA-256",
        )
    path = root / review.OUTPUT_DIRECTORY / f"{review_id}.json"
    receipt, payload = base._read_json(root, path, label="source target A/B review")
    _expect(
        validation["receipt_sha256"],
        receipt.get("receipt_sha256"),
        label="source review semantic SHA-256",
    )
    _expect(
        validation["byte_sha256"],
        base._sha256_bytes(payload),
        label="source review validated byte SHA-256",
    )
    _verify_scope_limits_from_source(receipt.get("scope_limits"), label="source target A/B scope_limits")
    summary = _require_object(receipt.get("summary"), label="source review summary")
    _expect(summary.get("slot_count"), 4, label="source review slot count")
    _expect(
        summary.get("compacted_accepted_no_issue_count"),
        4,
        label="source review compacted acceptance count",
    )
    approval = _require_object(receipt.get("approval_scope"), label="source review approval_scope")
    for key in (
        "human_listening_completed",
        "compacted_variant_selected_for_local_ab_candidates",
        "no_material_audible_difference_reported",
        "natural_masters_retained",
    ):
        _expect(approval.get(key), True, label=f"source review {key}")
    return receipt, {
        "id": review_id,
        "relative_path": f"{review.OUTPUT_DIRECTORY}/{review_id}.json",
        "receipt_sha256": validation["receipt_sha256"],
        "byte_sha256": validation["byte_sha256"],
        "decision_set_sha256": base._require_sha256(
            receipt.get("decision_set_sha256"),
            label="source review decision_set_sha256",
        ),
    }


def _verify_scope_limits_from_source(value: Any, *, label: str) -> None:
    limits = base._require_all_false(value, label=label)
    missing = set(review.SCOPE_LIMIT_KEYS) - set(limits)
    if missing:
        raise VoiceProviderPreflightError(f"{label} is missing closed gates: {sorted(missing)!r}")


def _load_paralinguistic_review(
    root: Path,
    review_id: str,
    *,
    expected_byte_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = base.validate_receipt(root, review_id)
    if expected_byte_sha256 is not None:
        _expect(
            validation["byte_sha256"],
            base._require_sha256(
                expected_byte_sha256,
                label="expected paralinguistic review byte SHA-256",
            ),
            label="paralinguistic review byte SHA-256",
        )
    path = root / base.OUTPUT_DIRECTORY / f"{review_id}.json"
    receipt, payload = base._read_json(root, path, label="source paralinguistic review")
    _expect(
        validation["receipt_sha256"],
        receipt.get("receipt_sha256"),
        label="paralinguistic review semantic SHA-256",
    )
    _expect(
        validation["byte_sha256"],
        base._sha256_bytes(payload),
        label="paralinguistic review validated byte SHA-256",
    )
    base._require_all_false(receipt.get("scope_limits"), label="paralinguistic review scope_limits")
    _expect(
        validation.get("target_ordinals"),
        [2, 3],
        label="paralinguistic target ordinals",
    )
    events = _require_array(receipt.get("events"), label="paralinguistic events")
    for event in events:
        item = _require_object(event, label="paralinguistic event")
        classification = _require_object(item.get("classification"), label="paralinguistic classification")
        _expect(
            classification.get("base_tts_training"),
            "excluded",
            label="paralinguistic base TTS state",
        )
        _expect(
            classification.get("event_bank_eligibility"),
            "pending_human_event_qa",
            label="paralinguistic event-bank state",
        )
    return receipt, {
        "id": review_id,
        "relative_path": f"{base.OUTPUT_DIRECTORY}/{review_id}.json",
        "receipt_sha256": validation["receipt_sha256"],
        "byte_sha256": validation["byte_sha256"],
        "event_set_sha256": base._require_sha256(receipt.get("event_set_sha256"), label="event_set_sha256"),
        "ordinals": [2, 3],
        "base_tts_training": "excluded",
        "event_bank_eligibility": "pending_human_event_qa",
    }


def _joined_asset_path(
    root: Path,
    package_manifest_relative_path: Any,
    asset_relative_path: Any,
    *,
    label: str,
) -> tuple[Path, str]:
    manifest_relative = base._safe_relative_path(
        package_manifest_relative_path, label=f"{label} package manifest path"
    )
    asset_relative = base._safe_relative_path(asset_relative_path, label=f"{label} asset path")
    combined = manifest_relative.parent / asset_relative
    canonical = PurePosixPath(combined.as_posix())
    path = root.joinpath(*canonical.parts)
    base._require_safe_existing_path(root, path, label=label, directory=False)
    return path, canonical.as_posix()


def _candidate_entries(root: Path, receipt: dict[str, Any]) -> list[dict[str, Any]]:
    packages = _require_array(receipt.get("packages"), label="source review packages")
    candidates: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_audio_hashes: set[str] = set()
    for package in packages:
        package_item = _require_object(package, label="source review package")
        character_id = base._require_string(
            package_item.get("runtime_character_id"), label="runtime character ID"
        )
        character = CHARACTERS.get(character_id)
        if character is None:
            raise VoiceProviderPreflightError(f"unexpected runtime character ID: {character_id!r}")
        _expect(
            package_item.get("runtime_character_name"),
            character["name"],
            label=f"{character_id} runtime character name",
        )
        decisions = _require_array(package_item.get("decisions"), label=f"{character['name']} decisions")
        _expect(len(decisions), 2, label=f"{character['name']} decision count")
        for decision in decisions:
            item = _require_object(decision, label="source review decision")
            slot = item.get("slot")
            if slot not in {"A", "B"}:
                raise VoiceProviderPreflightError("candidate slot must be A or B")
            candidate_key = f"{character['slug']}-{slot.lower()}"
            if candidate_key in seen_keys:
                raise VoiceProviderPreflightError(f"duplicate provider candidate key: {candidate_key}")
            seen_keys.add(candidate_key)
            _expect(
                item.get("selected_variant"),
                "compacted",
                label=f"{candidate_key} selected variant",
            )
            _expect(
                item.get("decision"),
                "compacted_accepted_no_issue",
                label=f"{candidate_key} human decision",
            )
            _expect(item.get("issue_codes"), [], label=f"{candidate_key} issues")
            text = _require_object(item.get("displayed_text"), label=f"{candidate_key} displayed text")
            audio = _require_object(item.get("selected_audio"), label=f"{candidate_key} selected audio")
            text_path, text_relative = _joined_asset_path(
                root,
                package_item.get("relative_path"),
                text.get("relative_path"),
                label=f"{candidate_key} enrollment text",
            )
            audio_path, audio_relative = _joined_asset_path(
                root,
                package_item.get("relative_path"),
                audio.get("relative_path"),
                label=f"{candidate_key} reference audio",
            )
            text_payload = base._read_stable_bytes(root, text_path, label=f"{candidate_key} enrollment text")
            audio_payload = base._read_stable_bytes(
                root, audio_path, label=f"{candidate_key} reference audio"
            )
            text_sha = base._sha256_bytes(text_payload)
            audio_sha = base._sha256_bytes(audio_payload)
            _expect(text_sha, text.get("sha256"), label=f"{candidate_key} text SHA-256")
            _expect(audio_sha, audio.get("sha256"), label=f"{candidate_key} audio SHA-256")
            _expect(len(text_payload), text.get("byte_count"), label=f"{candidate_key} text bytes")
            _expect(len(audio_payload), audio.get("byte_count"), label=f"{candidate_key} audio bytes")
            try:
                text_value = text_payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise VoiceProviderPreflightError(f"{candidate_key} enrollment text is not UTF-8") from error
            if not text_value.strip() or "\x00" in text_value:
                raise VoiceProviderPreflightError(f"{candidate_key} enrollment text is empty or contains NUL")
            if audio_sha in seen_audio_hashes:
                raise VoiceProviderPreflightError(
                    f"selected audio reused across provider candidates: {audio_sha}"
                )
            seen_audio_hashes.add(audio_sha)
            audio_format = _require_object(audio.get("audio_format"), label=f"{candidate_key} audio format")
            candidate_id = f"project-snow-{candidate_key}-{audio_sha[:8]}"
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "candidate_key": candidate_key,
                    "provider_voice_name_hint": (f"ps_{character['slug']}_{slot.lower()}_{audio_sha[:8]}"),
                    "character": {
                        "runtime_character_id": character_id,
                        "runtime_character_name": character["name"],
                        "character_slug": character["slug"],
                        "runtime_slug": character["runtime_slug"],
                    },
                    "source_selection": {
                        "package_id": package_item.get("package_id"),
                        "slot": slot,
                        "candidate_ordinal": item.get("candidate_ordinal"),
                        "original_candidate_id": item.get("original_candidate_id"),
                        "selected_variant": "compacted",
                        "human_decision": "compacted_accepted_no_issue",
                    },
                    "enrollment_text": {
                        "relative_path": text_relative,
                        "utf8_sha256": text_sha,
                        "byte_count": len(text_payload),
                        "text": text_value,
                    },
                    "reference_audio": {
                        "relative_path": audio_relative,
                        "wav_sha256": audio_sha,
                        "pcm_sha256": base._require_sha256(
                            audio.get("pcm_sha256"),
                            label=f"{candidate_key} PCM SHA-256",
                        ),
                        "byte_count": len(audio_payload),
                        "audio_format": audio_format,
                    },
                    "registration_state": {
                        "provider_voice_id": None,
                        "request_id": None,
                        "submitted": False,
                        "network_call_performed": False,
                        "cost_incurred": False,
                    },
                    "isolation": {
                        "one_reference_audio_only": True,
                        "not_concatenated_with_other_slots": True,
                        "not_jointly_weighted_with_other_slots": True,
                    },
                }
            )
    candidates.sort(
        key=lambda item: (
            CHARACTER_ORDER[item["character"]["runtime_character_id"]],
            item["source_selection"]["slot"],
        )
    )
    _expect(
        [item["candidate_key"] for item in candidates],
        ["vidya-a", "vidya-b", "chenxing-a", "chenxing-b"],
        label="exact four provider candidates",
    )
    return candidates


def _test_prompts() -> list[dict[str, Any]]:
    return [
        {
            "character_slug": "vidya",
            "runtime_character_name": "薇蒂雅",
            "cases": [
                {
                    "case_id": "vidya-neutral-short",
                    "category": "neutral_short",
                    "text": "今天的状态很稳定，我们可以按计划继续。",
                },
                {
                    "case_id": "vidya-long-punctuation",
                    "category": "long_punctuation",
                    "text": "先别急着回答；等灯光暗下来、四周安静以后，再把你真正担心的事，一件一件告诉我。",
                },
                {
                    "case_id": "vidya-question",
                    "category": "question_information",
                    "text": "如果把时间改到明晚，你还会按约定来见我吗？",
                },
                {
                    "case_id": "vidya-breathy-lexical",
                    "category": "restrained_breathy_lexical",
                    "text": "靠近一点，我只想把这句话轻轻说给你听。",
                },
                {
                    "case_id": "vidya-heightened",
                    "category": "heightened_fixated_lexical",
                    "text": "终于等到你了，别想让我现在移开视线！",
                },
                {
                    "case_id": "vidya-mixed",
                    "category": "numbers_english_pronunciation",
                    "text": "请在九月二日二十一点检查 Project Snow 的 A3 记录。",
                },
            ],
        },
        {
            "character_slug": "chenxing",
            "runtime_character_name": "辰星",
            "cases": [
                {
                    "case_id": "chenxing-neutral-short",
                    "category": "neutral_short",
                    "text": "设备运行正常，下一项检查可以开始。",
                },
                {
                    "case_id": "chenxing-long-punctuation",
                    "category": "long_punctuation",
                    "text": "先确认坐标，再核对路线；即使外面的风雪变大，我们也必须保持联络，并按顺序撤离。",
                },
                {
                    "case_id": "chenxing-question",
                    "category": "question_information",
                    "text": "你刚才记录的温度，是零下十二点五度吗？",
                },
                {
                    "case_id": "chenxing-breathy-lexical",
                    "category": "restrained_breathy_lexical",
                    "text": "声音放轻些，我就在这里，不必惊动其他人。",
                },
                {
                    "case_id": "chenxing-heightened",
                    "category": "heightened_urgent_lexical",
                    "text": "看着我，保持清醒，我们一定能一起回去！",
                },
                {
                    "case_id": "chenxing-mixed",
                    "category": "numbers_english_pronunciation",
                    "text": "倒计时 thirty seconds，随后切换到 B2 通讯频道。",
                },
            ],
        },
    ]


def _blind_test_plan(
    preflight_id: str,
    candidates: list[dict[str, Any]],
    paralinguistic_anchor: dict[str, Any],
) -> dict[str, Any]:
    candidate_map = [
        {
            "candidate_id": item["candidate_id"],
            "candidate_key": item["candidate_key"],
            "character_slug": item["character"]["character_slug"],
            "source_slot": item["source_selection"]["slot"],
        }
        for item in candidates
    ]
    return {
        "schema_version": PLAN_SCHEMA,
        "plan_id": f"{preflight_id}-blind-test",
        "source_preflight_id": preflight_id,
        "policy_version": BLIND_TEST_POLICY_VERSION,
        "status": "planned_not_rendered",
        "operator_only_candidate_map": candidate_map,
        "blindness_protocol": {
            "rater_must_not_receive_operator_candidate_map": True,
            "future_outputs_receive_random_opaque_labels_per_character": True,
            "label_assignment_requires_cryptographic_randomness": True,
            "same_prompt_and_synthesis_parameters_within_each_character_pair": True,
            "audio_loudness_normalization_for_comparison": "same_non_destructive_policy",
            "provider_voice_ids_must_not_appear_in_rater_files": True,
        },
        "registration_protocol": {
            "candidate_count": 4,
            "one_ephemeral_provider_voice_per_candidate": True,
            "a_b_candidates_are_never_concatenated": True,
            "a_b_candidates_are_never_jointly_weighted": True,
            "registration_order_must_not_change_synthesis_parameters": True,
        },
        "lexical_test_prompts": _test_prompts(),
        "rating_rubric": {
            "scale": {"minimum": 0, "maximum": 5, "integer_only": True},
            "dimensions": [
                "speaker_identity_similarity",
                "intelligibility",
                "naturalness",
                "character_fit",
                "prosody_and_breath_stability",
                "artifact_absence",
            ],
            "critical_failures": [
                "wrong_or_unstable_voice_identity",
                "truncation_or_missing_content",
                "hallucinated_or_repeated_words",
                "clipping_discontinuity_or_audible_seam",
            ],
        },
        "decision_rule": {
            "no_critical_failures": True,
            "minimum_median_per_dimension": 4.0,
            "minimum_paired_case_wins_out_of_six": 4,
            "minimum_mean_composite_lead": 0.25,
            "otherwise": "tie_or_collect_more_samples",
            "winner_scope": "one_winner_per_character_only",
        },
        "paralinguistic_event_lane": {
            "source_review": paralinguistic_anchor,
            "current_action": "do_not_submit_as_enrollment_text_or_reference_audio",
            "base_tts_training": "excluded",
            "event_bank_eligibility": "pending_human_event_qa",
            "future_hybrid_test_after_separate_authorization": [
                "base_tts_only",
                "base_tts_plus_curated_recorded_event_bank",
            ],
            "purpose": "preserve_nonlexical_delivery_without_polluting_base_voice_identity",
        },
        "rendered_outputs": [],
        "ratings": [],
        "winner_decisions": [],
    }


def _readme(preflight_id: str) -> str:
    return f"""# Project Snow voice provider preflight

Preflight ID: `{preflight_id}`

This directory is an offline-only, immutable handoff package. It references the
four human-approved compacted A/B samples; it does not copy or alter the source
audio. No provider request, credential read, voice creation, training, charge,
publication, or rollout has occurred.

`manifest.json` contains the hash-pinned candidate references and closed gates.
`blind_test_plan.json` contains the operator-side A/B protocol. Do not give its
operator candidate map to a rater.

Before any provider enrollment, obtain every authorization listed in the
manifest and create a separate execution receipt with the chosen provider
model, region, workspace, retention policy, and cost ceiling. A and B remain
independent ephemeral voices. Ordinals 2/3 remain excluded from base TTS and
may only enter a separately approved recorded-event-bank experiment.
"""


def build_preflight(
    voice_root: Path,
    *,
    prepared_at: str,
    source_review_id: str = DEFAULT_REVIEW_ID,
    source_paralinguistic_review_id: str = DEFAULT_PARALINGUISTIC_REVIEW_ID,
    expected_review_byte_sha256: str | None = None,
    expected_paralinguistic_byte_sha256: str | None = None,
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    prepared = base._parse_recorded_at(prepared_at)
    receipt, review_anchor = _load_review(
        root,
        source_review_id,
        expected_byte_sha256=expected_review_byte_sha256,
    )
    _, paralinguistic_anchor = _load_paralinguistic_review(
        root,
        source_paralinguistic_review_id,
        expected_byte_sha256=expected_paralinguistic_byte_sha256,
    )
    candidates = _candidate_entries(root, receipt)
    candidate_anchors = [
        {
            "candidate_id": item["candidate_id"],
            "candidate_key": item["candidate_key"],
            "text_sha256": item["enrollment_text"]["utf8_sha256"],
            "audio_sha256": item["reference_audio"]["wav_sha256"],
        }
        for item in candidates
    ]
    stable_basis = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "blind_test_policy_version": BLIND_TEST_POLICY_VERSION,
        "prepared_at": prepared,
        "source_review": review_anchor,
        "source_paralinguistic_review": paralinguistic_anchor,
        "provider_family": PROVIDER_FAMILY,
        "candidate_anchors": candidate_anchors,
    }
    stable_identity = base._semantic_sha256(stable_basis)
    preflight_id = f"voice-provider-preflight-{stable_identity[:20]}"
    plan = _blind_test_plan(preflight_id, candidates, paralinguistic_anchor)
    plan_payload = _pretty_json_bytes(plan)
    readme = _readme(preflight_id)
    readme_payload = readme.encode("utf-8")
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "preflight_id": preflight_id,
        "prepared_at": prepared,
        "artifact_purpose": "offline_only_provider_enrollment_preflight_and_blind_test_plan",
        "policy_version": POLICY_VERSION,
        "source_review": review_anchor,
        "source_paralinguistic_review": paralinguistic_anchor,
        "provider_intent": {
            "provider_family": PROVIDER_FAMILY,
            "operation": "four_independent_ephemeral_ab_enrollment_candidates",
            "provider_model": None,
            "region": None,
            "workspace_id": None,
            "retention_policy": None,
            "credential_source": None,
            "credentials_read": False,
            "network_calls_performed": False,
            "provider_voice_ids_created": [],
            "estimated_cost": None,
            "cost_ceiling": None,
            "cost_incurred": False,
        },
        "candidate_count": 4,
        "candidates": candidates,
        "artifacts": {
            "blind_test_plan": {
                "relative_path": "blind_test_plan.json",
                "semantic_sha256": base._semantic_sha256(plan),
                "byte_sha256": base._sha256_bytes(plan_payload),
                "byte_count": len(plan_payload),
            },
            "operator_readme": {
                "relative_path": "README.md",
                "byte_sha256": base._sha256_bytes(readme_payload),
                "byte_count": len(readme_payload),
            },
        },
        "required_next_authorizations": list(REQUIRED_AUTHORIZATIONS),
        "next_status": "await_explicit_rights_provider_cost_authorization",
        "scope_limits": _scope_limits(),
        "executed": True,
        "stable_identity": stable_identity,
    }
    manifest["manifest_sha256"] = base._semantic_sha256(manifest)
    destination = root / OUTPUT_DIRECTORY / preflight_id
    return {"manifest": manifest, "plan": plan, "readme": readme}, destination


def _validate_manifest_shape(manifest: dict[str, Any]) -> str:
    _expect(manifest.get("schema_version"), SCHEMA, label="preflight schema")
    preflight_id = base._safe_identifier(
        manifest.get("preflight_id"), PREFLIGHT_ID_PATTERN, label="preflight_id"
    )
    stable_identity = base._require_sha256(manifest.get("stable_identity"), label="preflight stable_identity")
    _expect(
        preflight_id,
        f"voice-provider-preflight-{stable_identity[:20]}",
        label="preflight ID derivation",
    )
    base._verify_semantic_hash(manifest, field="manifest_sha256", label="provider preflight manifest")
    _verify_scope_limits(manifest.get("scope_limits"), label="preflight scope_limits")
    _expect(manifest.get("candidate_count"), 4, label="preflight candidate count")
    _expect(
        manifest.get("required_next_authorizations"),
        list(REQUIRED_AUTHORIZATIONS),
        label="required authorizations",
    )
    provider = _require_object(manifest.get("provider_intent"), label="provider_intent")
    for key in (
        "credentials_read",
        "network_calls_performed",
        "cost_incurred",
    ):
        _expect(provider.get(key), False, label=f"provider_intent.{key}")
    _expect(
        provider.get("provider_voice_ids_created"),
        [],
        label="provider voice IDs",
    )
    return preflight_id


def validate_preflight(
    voice_root: Path,
    preflight_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    safe_id = base._safe_identifier(preflight_id, PREFLIGHT_ID_PATTERN, label="preflight_id")
    directory = root / OUTPUT_DIRECTORY / safe_id
    base._require_safe_existing_path(root, directory, label="provider preflight directory", directory=True)
    manifest, manifest_payload = base._read_json(
        root, directory / "manifest.json", label="provider preflight manifest"
    )
    _expect(_validate_manifest_shape(manifest), safe_id, label="preflight directory ID")
    if expected_manifest_byte_sha256 is not None:
        _expect(
            base._sha256_bytes(manifest_payload),
            base._require_sha256(
                expected_manifest_byte_sha256,
                label="expected manifest byte SHA-256",
            ),
            label="manifest byte SHA-256",
        )
    plan, plan_payload = base._read_json(root, directory / "blind_test_plan.json", label="blind test plan")
    readme_payload = base._read_stable_bytes(root, directory / "README.md", label="provider preflight README")
    artifacts = _require_object(manifest.get("artifacts"), label="manifest artifacts")
    plan_anchor = _require_object(artifacts.get("blind_test_plan"), label="blind test plan anchor")
    readme_anchor = _require_object(artifacts.get("operator_readme"), label="operator README anchor")
    _expect(plan.get("schema_version"), PLAN_SCHEMA, label="blind test plan schema")
    _expect(plan.get("source_preflight_id"), safe_id, label="plan preflight ID")
    _expect(
        base._semantic_sha256(plan),
        plan_anchor.get("semantic_sha256"),
        label="blind test plan semantic SHA-256",
    )
    _expect(
        base._sha256_bytes(plan_payload),
        plan_anchor.get("byte_sha256"),
        label="blind test plan byte SHA-256",
    )
    _expect(len(plan_payload), plan_anchor.get("byte_count"), label="blind test plan bytes")
    _expect(
        base._sha256_bytes(readme_payload),
        readme_anchor.get("byte_sha256"),
        label="operator README byte SHA-256",
    )
    _expect(len(readme_payload), readme_anchor.get("byte_count"), label="operator README bytes")
    source_review = _require_object(manifest.get("source_review"), label="manifest source_review")
    source_events = _require_object(
        manifest.get("source_paralinguistic_review"),
        label="manifest source_paralinguistic_review",
    )
    rebuilt, rebuilt_destination = build_preflight(
        root,
        prepared_at=manifest.get("prepared_at"),
        source_review_id=source_review.get("id"),
        source_paralinguistic_review_id=source_events.get("id"),
        expected_review_byte_sha256=source_review.get("byte_sha256"),
        expected_paralinguistic_byte_sha256=source_events.get("byte_sha256"),
    )
    _expect(rebuilt_destination, directory, label="rebuilt preflight path")
    _expect(rebuilt["manifest"], manifest, label="preflight/source reconstruction")
    _expect(rebuilt["plan"], plan, label="blind plan/source reconstruction")
    _expect(
        rebuilt["readme"].encode("utf-8"),
        readme_payload,
        label="README/source reconstruction",
    )
    return {
        "status": "valid",
        "preflight_id": safe_id,
        "path": str(directory),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_byte_sha256": base._sha256_bytes(manifest_payload),
        "blind_test_plan_byte_sha256": base._sha256_bytes(plan_payload),
        "candidate_count": 4,
        "all_scope_gates_closed": True,
        "provider_interactions_performed": False,
        "next_status": manifest["next_status"],
    }


def _remove_partial(directory: Path) -> None:
    for name in ("manifest.json", "blind_test_plan.json", "README.md"):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass


def _existing(root: Path, artifacts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    manifest = artifacts["manifest"]
    validate_preflight(root, manifest["preflight_id"])
    path = root / OUTPUT_DIRECTORY / manifest["preflight_id"] / "manifest.json"
    existing, _ = base._read_json(root, path, label="existing preflight manifest")
    if existing != manifest:
        return "existing_conflict", existing
    return "existing_valid", existing


def _validate_artifacts_for_write(
    root: Path,
    artifacts: dict[str, Any],
    destination: Path,
) -> None:
    manifest = _require_object(artifacts.get("manifest"), label="manifest")
    plan = _require_object(artifacts.get("plan"), label="blind test plan")
    readme = artifacts.get("readme")
    if not isinstance(readme, str) or not readme:
        raise VoiceProviderPreflightError("README must be non-empty text")
    preflight_id = _validate_manifest_shape(manifest)
    _expect(
        base._absolute_lexical(destination),
        root / OUTPUT_DIRECTORY / preflight_id,
        label="preflight destination",
    )
    anchors = _require_object(manifest.get("artifacts"), label="manifest artifacts")
    plan_anchor = _require_object(anchors.get("blind_test_plan"), label="blind test plan anchor")
    readme_anchor = _require_object(anchors.get("operator_readme"), label="operator README anchor")
    plan_payload = _pretty_json_bytes(plan)
    readme_payload = readme.encode("utf-8")
    _expect(
        base._semantic_sha256(plan),
        plan_anchor.get("semantic_sha256"),
        label="in-memory blind test plan semantic SHA-256",
    )
    _expect(
        base._sha256_bytes(plan_payload),
        plan_anchor.get("byte_sha256"),
        label="in-memory blind test plan byte SHA-256",
    )
    _expect(
        base._sha256_bytes(readme_payload),
        readme_anchor.get("byte_sha256"),
        label="in-memory README byte SHA-256",
    )
    source_review = _require_object(manifest.get("source_review"), label="manifest source_review")
    source_events = _require_object(
        manifest.get("source_paralinguistic_review"),
        label="manifest source_paralinguistic_review",
    )
    rebuilt, rebuilt_destination = build_preflight(
        root,
        prepared_at=manifest.get("prepared_at"),
        source_review_id=source_review.get("id"),
        source_paralinguistic_review_id=source_events.get("id"),
        expected_review_byte_sha256=source_review.get("byte_sha256"),
        expected_paralinguistic_byte_sha256=source_events.get("byte_sha256"),
    )
    _expect(rebuilt_destination, destination, label="rebuilt write destination")
    _expect(rebuilt, artifacts, label="preflight artifacts/source reconstruction")


def write_preflight(
    voice_root: Path,
    artifacts: dict[str, Any],
    destination: Path,
) -> tuple[str, dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    manifest = _require_object(artifacts.get("manifest"), label="manifest")
    plan = _require_object(artifacts.get("plan"), label="blind test plan")
    readme = artifacts.get("readme")
    if not isinstance(readme, str) or not readme:
        raise VoiceProviderPreflightError("README must be non-empty text")
    _validate_artifacts_for_write(root, artifacts, destination)
    preflight_id = manifest["preflight_id"]
    output = root / OUTPUT_DIRECTORY
    if output.exists():
        base._require_safe_existing_path(root, output, label="preflight output directory", directory=True)
    else:
        try:
            output.mkdir()
        except FileExistsError:
            pass
        base._require_safe_existing_path(root, output, label="preflight output directory", directory=True)
    expected_destination = output / preflight_id
    _expect(
        base._absolute_lexical(destination),
        expected_destination,
        label="preflight destination",
    )
    if expected_destination.exists():
        return _existing(root, artifacts)
    temporary = output / f".{preflight_id}.{uuid.uuid4().hex}.partial"
    temporary.mkdir()
    created_destination = False
    try:
        payloads = {
            "blind_test_plan.json": _pretty_json_bytes(plan),
            "README.md": readme.encode("utf-8"),
            "manifest.json": _pretty_json_bytes(manifest),
        }
        for name, payload in payloads.items():
            path = temporary / name
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            base._require_safe_existing_path(root, path, label=f"temporary {name}", directory=False)
        try:
            os.rename(temporary, expected_destination)
        except OSError:
            if expected_destination.exists():
                return _existing(root, artifacts)
            raise
        created_destination = True
        validation = validate_preflight(root, preflight_id)
        _expect(
            validation["manifest_sha256"],
            manifest["manifest_sha256"],
            label="written preflight manifest SHA-256",
        )
        return "created", manifest
    except Exception:
        if created_destination:
            _remove_partial(expected_destination)
        raise
    finally:
        if temporary.exists():
            _remove_partial(temporary)


def _summary(
    artifacts: dict[str, Any],
    destination: Path,
    *,
    mode: str,
    write_status: str | None,
) -> dict[str, Any]:
    manifest = artifacts["manifest"]
    return {
        "status": "ok",
        "mode": mode,
        "write_status": write_status,
        "preflight_id": manifest["preflight_id"],
        "path": str(destination),
        "manifest_sha256": manifest["manifest_sha256"],
        "candidate_count": manifest["candidate_count"],
        "candidate_keys": [item["candidate_key"] for item in manifest["candidates"]],
        "all_scope_gates_closed": True,
        "provider_interactions_performed": False,
        "next_status": manifest["next_status"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser(
        "prepare", help="dry-run or write the immutable offline preflight package"
    )
    prepare.add_argument("--voice-root", type=Path, required=True)
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--source-review-id", default=DEFAULT_REVIEW_ID)
    prepare.add_argument(
        "--source-paralinguistic-review-id",
        default=DEFAULT_PARALINGUISTIC_REVIEW_ID,
    )
    prepare.add_argument("--expect-review-byte-sha256")
    prepare.add_argument("--expect-paralinguistic-byte-sha256")
    prepare.add_argument("--confirm-offline-only", action="store_true")
    prepare.add_argument("--execute", action="store_true")
    validate = subparsers.add_parser("validate", help="revalidate a preflight and every pinned source asset")
    validate.add_argument("--voice-root", type=Path, required=True)
    validate.add_argument("--preflight-id", required=True)
    validate.add_argument("--expect-manifest-byte-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate":
            result = validate_preflight(
                arguments.voice_root,
                arguments.preflight_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
            )
        else:
            if arguments.execute and not arguments.confirm_offline_only:
                raise VoiceProviderPreflightError("--execute requires --confirm-offline-only")
            artifacts, destination = build_preflight(
                arguments.voice_root,
                prepared_at=arguments.prepared_at,
                source_review_id=arguments.source_review_id,
                source_paralinguistic_review_id=(arguments.source_paralinguistic_review_id),
                expected_review_byte_sha256=arguments.expect_review_byte_sha256,
                expected_paralinguistic_byte_sha256=(arguments.expect_paralinguistic_byte_sha256),
            )
            write_status = None
            if arguments.execute:
                write_status, existing = write_preflight(arguments.voice_root, artifacts, destination)
                if write_status == "existing_conflict":
                    raise VoiceProviderPreflightError(
                        "an immutable preflight already exists with conflicting content"
                    )
                artifacts["manifest"] = existing
            result = _summary(
                artifacts,
                destination,
                mode="execute" if arguments.execute else "dry_run",
                write_status=write_status,
            )
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
