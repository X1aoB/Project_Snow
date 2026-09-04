from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from scripts import voice_paralinguistic_ops as base
from scripts import voice_preference_tournament_ops as tournament
from scripts import voice_provider_blind_test_ops as blind


def _source_manifest(review: Path) -> dict:
    cases = []
    definitions = (
        ("vidya", "薇蒂雅", "neutral-short", "neutral_short"),
        ("vidya", "薇蒂雅", "breathy-lexical", "restrained_breathy_lexical"),
        ("vidya", "薇蒂雅", "heightened", "heightened_fixated_lexical"),
        ("chenxing", "辰星", "neutral-short", "neutral_short"),
        ("chenxing", "辰星", "breathy-lexical", "restrained_breathy_lexical"),
        ("chenxing", "辰星", "heightened", "heightened_urgent_lexical"),
    )
    for index, (character, name, suffix, category) in enumerate(definitions, start=1):
        samples = []
        for sample_index, opaque in enumerate(("sample-a111", "sample-b222"), start=1):
            pcm_value = 500 + index * 100 + sample_index
            pcm = pcm_value.to_bytes(2, "little", signed=True) * (blind.SAMPLE_RATE_HZ // 2)
            wav, metrics = blind._pcm_to_wav(pcm)
            relative = f"audio/{character}/{index:02d}-{opaque}.wav"
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
                "character_slug": character,
                "runtime_character_name": name,
                "case_id": f"{character}-{suffix}",
                "case_index": index,
                "category": category,
                "text": f"测试台词 {index}",
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


class VoicePreferenceTournamentTests(TestCase):
    def _fixture(self, root: Path) -> tuple[dict, str]:
        review = (
            root
            / blind.OUTPUT_DIRECTORY
            / tournament.DEFAULT_SOURCE_RUN_ID
            / "review"
        )
        review.mkdir(parents=True)
        manifest = _source_manifest(review)
        payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        (review / "manifest.json").write_bytes(payload)
        return manifest, base._sha256_bytes(payload)

    @staticmethod
    def _submission(document: dict) -> dict:
        decisions = {}
        for case in document["cases"]:
            decisions[case["case_id"]] = {
                "relative_choice": "second_sample",
                "selected_opaque_label_id": case["samples"][1]["opaque_label_id"],
                "winning_sample_usable": "not_usable",
                "rejection_reasons": ["wrong_expression_or_character_fit"],
            }
        return {
            "schema_version": tournament.SUBMISSION_SCHEMA,
            "round_id": document["round_id"],
            "source_review_manifest_sha256": document["source"]["review_manifest_sha256"],
            "saved_at": "2026-09-04T03:04:52.330Z",
            "decisions": decisions,
        }

    def test_round_reuses_twelve_audio_files_without_provider_or_numeric_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, source_byte_sha = self._fixture(root)
            document, audio, destination = tournament.build_round(
                root,
                source_run_id=tournament.DEFAULT_SOURCE_RUN_ID,
                expected_source_manifest_sha256=source["manifest_sha256"],
                expected_source_manifest_byte_sha256=source_byte_sha,
                prepared_at="2026-09-04T12:00:00+08:00",
            )

            self.assertEqual(len(document["cases"]), 6)
            self.assertEqual(len(audio), 12)
            self.assertFalse(document["decision_contract"]["numeric_scoring_used"])
            self.assertFalse(
                document["generation_contract"]["provider_calls_performed_for_this_round"]
            )
            self.assertEqual(document["generation_contract"]["incremental_provider_cost_usd"], "0")
            result = tournament.write_round(root, document, audio, destination)
            validated = tournament.validate_round(root, document["round_id"])

            self.assertEqual(result["reused_audio_count"], 12)
            self.assertEqual(validated["audio_count"], 12)
            self.assertEqual(validated["unique_wav_count"], 12)
            self.assertEqual(validated["full_scale_sample_count"], 0)

    def test_review_uses_pairwise_rejection_and_absolute_usability_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, source_byte_sha = self._fixture(root)
            document, _audio, _destination = tournament.build_round(
                root,
                source_run_id=tournament.DEFAULT_SOURCE_RUN_ID,
                expected_source_manifest_sha256=source["manifest_sha256"],
                expected_source_manifest_byte_sha256=source_byte_sha,
                prepared_at="2026-09-04T12:00:00+08:00",
            )

            page = tournament._review_html(document)

            self.assertIn("两个都否", page)
            self.assertIn("reject_both", page)
            self.assertIn("相对胜者能否直接用于项目", page)
            self.assertIn("wrong_voice_identity", page)
            self.assertIn("JSON.stringify(data,null,2)+'\\n'", page)
            self.assertNotIn("data-score", page)
            self.assertNotIn("0–5", page)

    def test_public_package_rejects_private_candidate_keys(self) -> None:
        with self.assertRaises(tournament.VoicePreferenceTournamentError):
            tournament._assert_public_privacy([b'{"candidate":"vidya-a"}'])

    def test_complete_submission_is_ingested_as_an_immutable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, source_byte_sha = self._fixture(root)
            document, audio, destination = tournament.build_round(
                root,
                source_run_id=tournament.DEFAULT_SOURCE_RUN_ID,
                expected_source_manifest_sha256=source["manifest_sha256"],
                expected_source_manifest_byte_sha256=source_byte_sha,
                prepared_at="2026-09-04T12:00:00+08:00",
            )
            tournament.write_round(root, document, audio, destination)
            submission = self._submission(document)
            submission_path = root / "exported-decisions.json"
            submission_path.write_text(
                json.dumps(submission, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            received = tournament.ingest_decision_receipt(
                root, document["round_id"], submission_path
            )
            validated = tournament.validate_decision_receipt(root, document["round_id"])

            self.assertEqual(received["write_status"], "written")
            self.assertEqual(validated["summary"]["case_count"], 6)
            self.assertEqual(validated["summary"]["not_usable_case_count"], 6)
            self.assertEqual(
                validated["summary"]["rejection_reason_counts"][
                    "wrong_expression_or_character_fit"
                ],
                6,
            )
            repeated = tournament.ingest_decision_receipt(
                root, document["round_id"], submission_path
            )
            self.assertEqual(repeated["write_status"], "existing_identical")

    def test_submission_rejects_a_label_that_does_not_match_the_choice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, source_byte_sha = self._fixture(root)
            document, audio, destination = tournament.build_round(
                root,
                source_run_id=tournament.DEFAULT_SOURCE_RUN_ID,
                expected_source_manifest_sha256=source["manifest_sha256"],
                expected_source_manifest_byte_sha256=source_byte_sha,
                prepared_at="2026-09-04T12:00:00+08:00",
            )
            tournament.write_round(root, document, audio, destination)
            submission = self._submission(document)
            first_case = document["cases"][0]
            submission["decisions"][first_case["case_id"]][
                "selected_opaque_label_id"
            ] = first_case["samples"][0]["opaque_label_id"]
            submission_path = root / "invalid-decisions.json"
            submission_path.write_text(json.dumps(submission), encoding="utf-8")

            with self.assertRaises(tournament.VoicePreferenceTournamentError):
                tournament.build_decision_receipt(
                    root, document["round_id"], submission_path
                )
