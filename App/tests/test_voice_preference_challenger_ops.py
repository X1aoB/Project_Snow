from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from scripts import voice_paralinguistic_ops as base
from scripts import voice_preference_challenger_ops as challenger
from scripts import voice_preference_tournament_ops as tournament
from scripts import voice_provider_blind_test_ops as blind


def _source_manifest(review: Path) -> dict:
    definitions = (
        ("vidya", "薇蒂雅", "neutral-short", "neutral_short"),
        ("vidya", "薇蒂雅", "breathy-lexical", "restrained_breathy_lexical"),
        ("vidya", "薇蒂雅", "heightened", "heightened_fixated_lexical"),
        ("chenxing", "辰星", "neutral-short", "neutral_short"),
        ("chenxing", "辰星", "breathy-lexical", "restrained_breathy_lexical"),
        ("chenxing", "辰星", "heightened", "heightened_urgent_lexical"),
    )
    texts = {
        "vidya-neutral-short": "今天的状态很稳定，我们可以按计划继续。",
        "vidya-breathy-lexical": "靠近一点，我只想把这句话轻轻说给你听。",
        "vidya-heightened": "终于等到你了，别想让我现在移开视线！",
        "chenxing-neutral-short": "设备运行正常，下一项检查可以开始。",
        "chenxing-breathy-lexical": "声音放轻些，我就在这里，不必惊动其他人。",
        "chenxing-heightened": "看着我，保持清醒，我们一定能一起回去！",
    }
    labels = {
        "vidya": ("sample-v111", "sample-v222"),
        "chenxing": ("sample-c111", "sample-c222"),
    }
    cases = []
    for index, (slug, name, suffix, category) in enumerate(definitions, start=1):
        case_id = f"{slug}-{suffix}"
        samples = []
        for sample_index, opaque in enumerate(labels[slug], start=1):
            pcm_value = 400 + index * 100 + sample_index
            pcm = pcm_value.to_bytes(2, "little", signed=True) * (
                blind.SAMPLE_RATE_HZ // 2
            )
            wav, metrics = blind._pcm_to_wav(pcm)
            relative = f"audio/{slug}/{index:02d}-{opaque}.wav"
            path = review.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(wav)
            samples.append(
                {
                    "opaque_label_id": opaque,
                    "display_label": opaque.upper(),
                    "audio_relative_path": relative,
                    "duration_seconds": metrics["duration_seconds"],
                    "wav_sha256": metrics["wav_sha256"],
                    "full_scale_sample_count": 0,
                }
            )
        cases.append(
            {
                "character_slug": slug,
                "runtime_character_name": name,
                "case_id": case_id,
                "case_index": index,
                "category": category,
                "text": texts[case_id],
                "samples": samples,
            }
        )
    manifest = {
        "schema_version": blind.PUBLIC_SCHEMA,
        "rating_submission_schema_version": blind.RATING_SCHEMA,
        "blind_test_run_id": tournament.DEFAULT_SOURCE_RUN_ID,
        "status": "ready_for_local_human_blind_review",
        "privacy_contract": {
            "candidate_mapping_included": False,
            "provider_voice_ids_included": False,
            "candidate_a_b_labels_included": False,
            "local_review_only": True,
            "publication_authorized": False,
        },
        "cases": cases,
    }
    manifest["manifest_sha256"] = base._semantic_sha256(manifest)
    return manifest


class VoicePreferenceChallengerTests(TestCase):
    def _round_fixture(self, root: Path) -> tuple[dict, dict]:
        review = root / blind.OUTPUT_DIRECTORY / tournament.DEFAULT_SOURCE_RUN_ID / "review"
        review.mkdir(parents=True)
        source = _source_manifest(review)
        source_payload = (json.dumps(source, ensure_ascii=False, indent=2) + "\n").encode()
        (review / "manifest.json").write_bytes(source_payload)
        round_document, audio, destination = tournament.build_round(
            root,
            source_run_id=tournament.DEFAULT_SOURCE_RUN_ID,
            expected_source_manifest_sha256=source["manifest_sha256"],
            expected_source_manifest_byte_sha256=base._sha256_bytes(source_payload),
            prepared_at="2026-09-04T10:00:00+08:00",
        )
        tournament.write_round(root, round_document, audio, destination)
        decisions = {}
        for case in round_document["cases"]:
            reject_both = case["case_id"] == "vidya-breathy-lexical"
            decisions[case["case_id"]] = {
                "relative_choice": "reject_both" if reject_both else "second_sample",
                "selected_opaque_label_id": (
                    None if reject_both else case["samples"][1]["opaque_label_id"]
                ),
                "winning_sample_usable": "not_usable",
                "rejection_reasons": ["wrong_expression_or_character_fit"],
            }
        submission = {
            "schema_version": tournament.SUBMISSION_SCHEMA,
            "round_id": round_document["round_id"],
            "source_review_manifest_sha256": source["manifest_sha256"],
            "saved_at": "2026-09-04T03:04:52.330Z",
            "decisions": decisions,
        }
        submission_path = root / "decisions.json"
        submission_path.write_text(json.dumps(submission), encoding="utf-8")
        tournament.ingest_decision_receipt(
            root, round_document["round_id"], submission_path
        )
        private_manifest = {
            "manifest_sha256": "",
            "operator_only_candidate_mapping": [
                {
                    "character_slug": "vidya",
                    "labels": [
                        {
                            "candidate_key": "vidya-a",
                            "opaque_label_id": "sample-v111",
                        },
                        {
                            "candidate_key": "vidya-b",
                            "opaque_label_id": "sample-v222",
                        },
                    ],
                },
                {
                    "character_slug": "chenxing",
                    "labels": [
                        {
                            "candidate_key": "chenxing-a",
                            "opaque_label_id": "sample-c111",
                        },
                        {
                            "candidate_key": "chenxing-b",
                            "opaque_label_id": "sample-c222",
                        },
                    ],
                },
            ],
            "provider_contract": {
                "synthesis_parameters": {
                    "mode": blind.MODE,
                    "language_type": blind.LANGUAGE_TYPE,
                    "response_format": blind.RESPONSE_FORMAT,
                    "sample_rate_hz": blind.SAMPLE_RATE_HZ,
                    "channels": blind.CHANNELS,
                    "sample_width_bytes": blind.SAMPLE_WIDTH_BYTES,
                    "post_processing": "none",
                    "loudness_policy": "same_non_destructive_policy_no_normalization",
                }
            },
            "source_enrollment": {"run_id": "fixture-enrollment"},
        }
        private_manifest["manifest_sha256"] = base._semantic_sha256(private_manifest)
        private_directory = root / "private-blind"
        private_directory.mkdir()
        private_payload = (
            json.dumps(private_manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        (private_directory / "manifest.json").write_bytes(private_payload)
        voices = {
            "vidya-a": "voice_vidya_a",
            "vidya-b": "voice_vidya_b",
            "chenxing-a": "voice_chenxing_a",
            "chenxing-b": "voice_chenxing_b",
        }
        private_manifest["_fixture_byte_sha256"] = base._sha256_bytes(private_payload)
        return round_document, {
            "directory": private_directory,
            "manifest": private_manifest,
            "voices": voices,
        }

    @staticmethod
    def _second_round_fixture(root: Path, first_round: dict) -> dict:
        second_round = json.loads(json.dumps(first_round))
        second_round["round_id"] = "voice-preference-round-22222222222222222222"
        second_round["round_index"] = 2
        second_round["prepared_at"] = "2026-09-04T12:00:00+08:00"
        second_round["source"] = {
            "previous_round_id": first_round["round_id"],
            "previous_round_manifest_sha256": first_round["manifest_sha256"],
            "blind_test_run_id": tournament.DEFAULT_SOURCE_RUN_ID,
        }
        second_round["generation_contract"] = {
            "provider_calls_performed_for_this_round": True,
            "new_synthesis_outputs_created": 7,
            "reused_existing_blind_outputs": 5,
            "incremental_provider_cost_usd": "0.003",
            "next_provider_generation_requires_complete_human_submission": True,
            "provider_learns_from_rejection": False,
        }
        second_round.pop("manifest_sha256")
        second_round["manifest_sha256"] = base._semantic_sha256(second_round)
        first_review = (
            root
            / tournament.OUTPUT_DIRECTORY
            / first_round["round_id"]
            / "review"
        )
        audio = {}
        for case in second_round["cases"]:
            for sample in case["samples"]:
                audio[sample["audio_relative_path"]] = first_review.joinpath(
                    *sample["audio_relative_path"].split("/")
                ).read_bytes()
        destination = (
            root
            / tournament.OUTPUT_DIRECTORY
            / second_round["round_id"]
            / "review"
        )
        tournament.write_round(root, second_round, audio, destination)
        decisions = {}
        for case in second_round["cases"]:
            reject_both = case["case_id"] == "chenxing-heightened"
            decisions[case["case_id"]] = {
                "relative_choice": "reject_both" if reject_both else "second_sample",
                "selected_opaque_label_id": (
                    None if reject_both else case["samples"][1]["opaque_label_id"]
                ),
                "winning_sample_usable": "not_usable" if reject_both else "usable",
                "rejection_reasons": ["wrong_voice_identity"] if reject_both else [],
            }
        submission = {
            "schema_version": tournament.SUBMISSION_SCHEMA,
            "round_id": second_round["round_id"],
            "source_review_manifest_sha256": second_round["manifest_sha256"],
            "saved_at": "2026-09-04T13:00:00+08:00",
            "decisions": decisions,
        }
        submission_path = root / "second-round-decisions.json"
        submission_path.write_text(json.dumps(submission), encoding="utf-8")
        tournament.ingest_decision_receipt(
            root, second_round["round_id"], submission_path
        )
        return second_round

    def test_builds_seven_punctuation_only_challengers_under_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            round_document, private = self._round_fixture(root)
            manifest_for_loader = dict(private["manifest"])
            manifest_for_loader.pop("_fixture_byte_sha256")
            with patch.object(
                challenger.blind,
                "load_run",
                return_value=(
                    private["directory"],
                    manifest_for_loader,
                    private["voices"],
                ),
            ):
                manifest, _ = challenger.build_run(
                    root,
                    source_round_id=round_document["round_id"],
                    source_blind_manifest_byte_sha256=private["manifest"][
                        "_fixture_byte_sha256"
                    ],
                    prepared_at="2026-09-04T12:00:00+08:00",
                )

            self.assertEqual(len(manifest["planned_outputs"]), 7)
            self.assertLessEqual(
                Decimal(manifest["pricing_contract"]["estimated_incremental_cost_usd"]),
                challenger.INCREMENTAL_COST_CEILING_USD,
            )
            self.assertFalse(
                manifest["provider_contract"][
                    "instruction_control_supported_by_target_model"
                ]
            )
            self.assertTrue(
                all(
                    item["delivery_strategy"] == "same_lexical_text_punctuation_only"
                    for item in manifest["planned_outputs"]
                )
            )

    def test_fake_render_finalizes_a_private_mapping_and_public_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            round_document, private = self._round_fixture(root)
            manifest_for_loader = dict(private["manifest"])
            manifest_for_loader.pop("_fixture_byte_sha256")
            load_result = (
                private["directory"],
                manifest_for_loader,
                private["voices"],
            )
            with patch.object(challenger.blind, "load_run", return_value=load_result):
                manifest, destination = challenger.build_run(
                    root,
                    source_round_id=round_document["round_id"],
                    source_blind_manifest_byte_sha256=private["manifest"][
                        "_fixture_byte_sha256"
                    ],
                    prepared_at="2026-09-04T12:00:00+08:00",
                )
                challenger.write_run(root, manifest, destination)

                counter = iter(range(1, 100))

                def fake_provider(**_kwargs):
                    number = next(counter)
                    pcm = number.to_bytes(2, "little", signed=True) * blind.SAMPLE_RATE_HZ
                    return pcm, {
                        "session_id": f"session-{number}",
                        "response_id": f"response-{number}",
                        "provider_usage_characters": 1,
                    }

                with (
                    patch.object(challenger, "_load_workspace_id", return_value="ws-fixture"),
                    patch.object(challenger.enrollment, "_read_secret", return_value="secret"),
                ):
                    rendered = challenger.render_all(
                        root,
                        manifest["run_id"],
                        dotenv_file=None,
                        expected_manifest_byte_sha256=base._sha256_bytes(
                            challenger._pretty_json_bytes(manifest)
                        ),
                        confirm_run_id=manifest["run_id"],
                        confirm_model=blind.MODEL,
                        confirm_region=blind.REGION,
                        confirm_cost_ceiling_usd="0.01",
                        confirm_synthesis_and_local_review_authorized=True,
                        confirm_instruction_control_unavailable=True,
                        confirm_paralinguistic_ordinals_excluded=True,
                        provider=fake_provider,
                    )
                    final = challenger.finalize_review(
                        root,
                        manifest["run_id"],
                        expected_manifest_byte_sha256=base._sha256_bytes(
                            challenger._pretty_json_bytes(manifest)
                        ),
                    )

            self.assertEqual(rendered["successful_output_count"], 7)
            self.assertEqual(final["audio_count"], 12)
            self.assertEqual(final["new_provider_outputs"], 7)
            self.assertEqual(final["reused_incumbent_outputs"], 5)
            public_page = Path(final["review_html_path"]).read_text(encoding="utf-8")
            self.assertNotIn('"vidya-a"', public_page)
            self.assertNotIn('"chenxing-b"', public_page)
            candidate_map = (
                root
                / tournament.OUTPUT_DIRECTORY
                / final["round_id"]
                / "operator"
                / "candidate-map.json"
            )
            self.assertTrue(candidate_map.is_file())

    def test_round_two_receipt_builds_twelve_unseen_dual_candidate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_round, private = self._round_fixture(root)
            second_round = self._second_round_fixture(root, first_round)
            manifest_for_loader = dict(private["manifest"])
            manifest_for_loader.pop("_fixture_byte_sha256")
            with patch.object(
                challenger.blind,
                "load_run",
                return_value=(
                    private["directory"],
                    manifest_for_loader,
                    private["voices"],
                ),
            ):
                manifest, _ = challenger.build_run(
                    root,
                    source_round_id=second_round["round_id"],
                    source_blind_manifest_byte_sha256=private["manifest"][
                        "_fixture_byte_sha256"
                    ],
                    prepared_at="2026-09-04T14:00:00+08:00",
                )

            self.assertEqual(manifest["policy_version"], challenger.UNSEEN_POLICY_VERSION)
            self.assertEqual(len(manifest["planned_outputs"]), 12)
            self.assertEqual(
                {item["delivery_strategy"] for item in manifest["planned_outputs"]},
                {"unseen_text_dual_candidate_validation"},
            )
            self.assertEqual(
                {
                    sample["origin"]
                    for pairing in manifest["operator_only_public_pairing_plan"]
                    for sample in pairing["samples"]
                },
                {"new_challenger"},
            )
            review_html = tournament._review_html(
                {
                    **second_round,
                    "round_index": 3,
                    "generation_contract": {
                        **second_round["generation_contract"],
                        "unseen_validation_text": True,
                    },
                }
            )
            self.assertIn("终局未见台词验证", review_html)
            self.assertIn("不再自动生成同类 A/B 轮次", review_html)
            self.assertIn("该槽位记为暂不合格并暂停", review_html)
            for case in second_round["cases"]:
                texts = {
                    item["text"]
                    for item in manifest["planned_outputs"]
                    if item["case_id"] == case["case_id"]
                }
                self.assertEqual(texts, {challenger.UNSEEN_VALIDATION_TEXT[case["case_id"]]})
                self.assertNotEqual(texts, {case["text"]})

    def test_terminal_conclusion_locks_or_pauses_without_resampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_round, _private = self._round_fixture(root)
            second_round = self._second_round_fixture(root, first_round)
            third_round = json.loads(json.dumps(second_round))
            third_round["round_id"] = "voice-preference-round-33333333333333333333"
            third_round["round_index"] = 3
            third_round["prepared_at"] = "2026-09-04T14:30:00+08:00"
            third_round["source"] = {
                "previous_round_id": second_round["round_id"],
                "previous_round_manifest_sha256": second_round["manifest_sha256"],
                "challenger_run_manifest_sha256": "a" * 64,
            }
            third_round["generation_contract"] = {
                "provider_calls_performed_for_this_round": True,
                "new_synthesis_outputs_created": 12,
                "reused_existing_blind_outputs": 0,
                "incremental_provider_cost_usd": "0.005",
                "steering_scope": "unseen_text_dual_candidate_validation",
                "unseen_validation_text": True,
                "next_provider_generation_requires_complete_human_submission": True,
                "provider_learns_from_rejection": False,
            }
            third_round.pop("manifest_sha256")
            third_round["manifest_sha256"] = base._semantic_sha256(third_round)

            second_review = (
                root
                / tournament.OUTPUT_DIRECTORY
                / second_round["round_id"]
                / "review"
            )
            audio = {
                sample["audio_relative_path"]: second_review.joinpath(
                    *sample["audio_relative_path"].split("/")
                ).read_bytes()
                for case in third_round["cases"]
                for sample in case["samples"]
            }
            destination = (
                root
                / tournament.OUTPUT_DIRECTORY
                / third_round["round_id"]
                / "review"
            )
            tournament.write_round(root, third_round, audio, destination)

            entries = []
            for case in third_round["cases"]:
                entries.append(
                    {
                        "case_id": case["case_id"],
                        "character_slug": case["character_slug"],
                        "samples": [
                            {
                                "opaque_label_id": sample["opaque_label_id"],
                                "candidate_key": (
                                    f"{case['character_slug']}-"
                                    + ("a" if index == 0 else "b")
                                ),
                                "origin": "new_challenger",
                            }
                            for index, sample in enumerate(case["samples"])
                        ],
                    }
                )
            candidate_map = {
                "schema_version": challenger.MAP_SCHEMA,
                "round_id": third_round["round_id"],
                "source_challenger_run_id": (
                    "voice-preference-challenger-run-" + "b" * 20
                ),
                "source_challenger_manifest_sha256": "a" * 64,
                "entries": entries,
            }
            candidate_map["manifest_sha256"] = base._semantic_sha256(candidate_map)
            operator = destination.parent / "operator"
            operator.mkdir()
            (operator / "candidate-map.json").write_bytes(
                challenger._pretty_json_bytes(candidate_map)
            )

            decisions = {}
            for case in third_round["cases"]:
                paused = case["case_id"] == "vidya-breathy-lexical"
                decisions[case["case_id"]] = {
                    "relative_choice": "second_sample",
                    "selected_opaque_label_id": case["samples"][1]["opaque_label_id"],
                    "winning_sample_usable": "not_usable" if paused else "usable",
                    "rejection_reasons": (
                        ["wrong_expression_or_character_fit"] if paused else []
                    ),
                }
            submission = {
                "schema_version": tournament.SUBMISSION_SCHEMA,
                "round_id": third_round["round_id"],
                "source_review_manifest_sha256": third_round["manifest_sha256"],
                "saved_at": "2026-09-04T15:00:00+08:00",
                "decisions": decisions,
            }
            submission_path = root / "terminal-decisions.json"
            submission_path.write_text(json.dumps(submission), encoding="utf-8")
            tournament.ingest_decision_receipt(
                root, third_round["round_id"], submission_path
            )

            conclusion, conclusion_path = challenger.build_terminal_conclusion(
                root,
                third_round["round_id"],
                concluded_at="2026-09-04T15:30:00+08:00",
            )
            result = challenger.write_terminal_conclusion(
                root, conclusion, conclusion_path
            )
            by_case = {slot["case_id"]: slot for slot in conclusion["slots"]}

            self.assertEqual(result["locked_slot_count"], 5)
            self.assertEqual(result["paused_case_ids"], ["vidya-breathy-lexical"])
            self.assertEqual(
                by_case["vidya-breathy-lexical"]["disposition"],
                "paused_not_qualified",
            )
            self.assertIsNone(
                by_case["vidya-breathy-lexical"]["runtime_candidate_ref"]
            )
            self.assertEqual(
                by_case["chenxing-heightened"]["disposition"], "locked_for_slot"
            )
            self.assertFalse(
                conclusion["terminal_contract"][
                    "automatic_additional_pairwise_rounds_allowed"
                ]
            )
            self.assertFalse(result["provider_calls_performed"])
