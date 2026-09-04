from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from scripts import voice_paralinguistic_ops as base
from scripts import voice_preference_challenger_ops as challenger
from scripts import voice_provider_blind_test_ops as blind
from scripts import voice_runtime_profile_ops as runtime_profile


def _payload(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sources() -> tuple:
    slots = []
    definitions = (
        ("vidya-neutral-short", "vidya", "薇蒂雅", "neutral", "vidya-a", True),
        (
            "vidya-breathy-lexical",
            "vidya",
            "薇蒂雅",
            "restrained_breathy_lexical",
            None,
            False,
        ),
        ("vidya-heightened", "vidya", "薇蒂雅", "heightened", "vidya-b", True),
        ("chenxing-neutral-short", "chenxing", "辰星", "neutral", "chenxing-a", True),
        (
            "chenxing-breathy-lexical",
            "chenxing",
            "辰星",
            "restrained_breathy_lexical",
            "chenxing-b",
            True,
        ),
        (
            "chenxing-heightened",
            "chenxing",
            "辰星",
            "heightened",
            "chenxing-a",
            True,
        ),
    )
    for sequence, (case_id, slug, name, style, candidate, locked) in enumerate(definitions, start=1):
        slots.append(
            {
                "sequence": sequence,
                "case_id": case_id,
                "character_slug": slug,
                "runtime_character_name": name,
                "style_anchor": style,
                "disposition": "locked_for_slot" if locked else "paused_not_qualified",
                "runtime_candidate_ref": candidate,
                "relative_preference_candidate_ref": candidate,
                "rejection_reasons": [] if locked else ["wrong_expression_or_character_fit"],
            }
        )
    conclusion = {
        "schema_version": challenger.TERMINAL_CONCLUSION_SCHEMA,
        "conclusion_id": "voice-preference-terminal-conclusion-fixture",
        "concluded_at": "2026-09-04T15:30:00+08:00",
        "status": "terminal_current_clone_pool_concluded",
        "slots": slots,
        "manifest_sha256": "a" * 64,
    }
    candidate_map = {
        "manifest_sha256": "b" * 64,
    }
    challenger_manifest = {
        "run_id": "voice-preference-challenger-run-" + "c" * 20,
        "manifest_sha256": "d" * 64,
        "source_blind_test": {
            "run_id": "voice-provider-blind-test-run-" + "e" * 20,
            "manifest_sha256": "f" * 64,
            "manifest_byte_sha256": "1" * 64,
        },
    }
    voices = {
        "vidya-a": "qwen-voice-vidya-a",
        "vidya-b": "qwen-voice-vidya-b",
        "chenxing-a": "qwen-voice-chenxing-a",
        "chenxing-b": "qwen-voice-chenxing-b",
    }
    return (
        conclusion,
        _payload(conclusion),
        candidate_map,
        _payload(candidate_map),
        challenger_manifest,
        _payload(challenger_manifest),
        voices,
        "ws-fixture-runtime",
    )


class VoiceRuntimeProfileTests(TestCase):
    def test_builds_five_locked_routes_and_one_fail_closed_pause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runtime_profile, "_terminal_sources", return_value=_sources()):
                document, destination = runtime_profile.build_profile(
                    root,
                    terminal_round_id=runtime_profile.DEFAULT_TERMINAL_ROUND_ID,
                    prepared_at="2026-09-04T16:00:00+08:00",
                )

            routes = {
                item["case_id"]: item for character in document["characters"] for item in character["routes"]
            }
            self.assertEqual(document["summary"]["locked_slot_count"], 5)
            self.assertEqual(document["summary"]["paused_case_ids"], ["vidya-breathy-lexical"])
            paused = routes["vidya-breathy-lexical"]
            self.assertEqual(paused["status"], "paused")
            self.assertNotIn("provider_voice_id", paused)
            self.assertFalse(document["routing_contract"]["paused_slot_fallback_allowed"])
            self.assertEqual(destination.parent.name, runtime_profile.OUTPUT_DIRECTORY)

    def test_safe_result_never_exposes_provider_voice_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runtime_profile, "_terminal_sources", return_value=_sources()):
                document, destination = runtime_profile.build_profile(
                    root,
                    terminal_round_id=runtime_profile.DEFAULT_TERMINAL_ROUND_ID,
                    prepared_at="2026-09-04T16:00:00+08:00",
                )
                result = runtime_profile.write_profile(root, document, destination)
                validation = runtime_profile.validate_profile(root, document["profile_id"])

            safe_output = json.dumps({"write": result, "validate": validation})
            for voice_id in _sources()[-2].values():
                self.assertNotIn(voice_id, safe_output)
            self.assertFalse(result["private_provider_voice_ids_exposed_in_result"])
            self.assertFalse(validation["provider_calls_performed"])
            self.assertEqual(validation["incremental_provider_cost_usd"], "0")

    def test_tampered_manifest_is_rejected_before_source_reconstruction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runtime_profile, "_terminal_sources", return_value=_sources()):
                document, destination = runtime_profile.build_profile(
                    root,
                    terminal_round_id=runtime_profile.DEFAULT_TERMINAL_ROUND_ID,
                    prepared_at="2026-09-04T16:00:00+08:00",
                )
                runtime_profile.write_profile(root, document, destination)
            path = destination / "manifest.json"
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["routing_contract"]["paused_slot_fallback_allowed"] = True
            path.write_text(json.dumps(tampered), encoding="utf-8")

            with self.assertRaises(base.VoiceParalinguisticError):
                runtime_profile.validate_profile(root, document["profile_id"])

    def test_contract_is_fixed_to_beijing_qwen_vc_without_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(runtime_profile, "_terminal_sources", return_value=_sources()):
                document, _ = runtime_profile.build_profile(
                    root,
                    terminal_round_id=runtime_profile.DEFAULT_TERMINAL_ROUND_ID,
                    prepared_at="2026-09-04T16:00:00+08:00",
                )

            provider = document["provider_contract"]
            self.assertEqual(provider["region"], blind.REGION)
            self.assertEqual(provider["target_model"], blind.MODEL)
            self.assertEqual(provider["websocket_endpoint"], blind.WEBSOCKET_ENDPOINT)
            self.assertFalse(provider["instruction_control_supported_by_target_model"])
            self.assertFalse(provider["instructions_sent"])
