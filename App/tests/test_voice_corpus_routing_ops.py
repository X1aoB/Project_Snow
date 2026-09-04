from __future__ import annotations

import hashlib
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase

from scripts import voice_corpus_routing_ops as routing
from scripts import voice_paralinguistic_ops as para
from tests.test_voice_paralinguistic_ops import (
    INPUT_AUDIO_SET_SHA,
    INVENTORY_SHA,
    PACKAGE_ID,
    RUN_ID,
    SUBMISSION_ID,
    TRANSCRIPT_SET_SHA,
    VoiceFixture,
    _pretty_bytes,
    _write_json,
    _write_wav,
)

RECORDED_AT = "2026-09-02T05:00:00+00:00"

LEXICAL_CASES = {
    1: ("accepted_exact", "昨天成功摸到分析员的腹肌了,那今天我可以……", None),
    4: ("corrected_from_audio", "是..是分析员", "是，是分析员"),
    5: ("accepted_exact", "果然没白等,今天我是第一个见到分析员的人。", None),
    6: ("corrected_from_audio", "分析员，我可以走近些看看你吗？", "分析员我可以走近些看看你吗"),
    7: (
        "corrected_from_audio",
        "那,我能摸摸你吗?我,我这都是为了下一部作品在取材,嗯。",
        "那我能摸摸你吗我这都是为了下一部作品在取材",
    ),
    8: ("accepted_exact", "如此重要的事情,我希望你不要忘记。", None),
    9: ("corrected_from_audio", "风雨晦明，潮汐云烟", "风雨晦明潮汐云烟"),
    10: ("corrected_from_audio", "皆昭前路", "皆照前路"),
    11: ("accepted_exact", "正确的选择,才能导向正确的结果。", None),
    12: ("accepted_exact", "正确的选择,才能导向正确的结果。", None),
    13: ("accepted_exact", "很快就能结束。", None),
    14: ("accepted_exact", "不要忘记看消息。", None),
    15: ("accepted_exact", "我不想和别人有太多牵扯,也不想制造更多回忆。", None),
}


class RoutingFixture(VoiceFixture):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._augment_to_fifteen_spans()
        self.paralinguistic_receipt: dict | None = None
        self.paralinguistic_byte_sha: str | None = None

    def _augment_to_fifteen_spans(self) -> None:
        self.queue["characters"].append(
            {
                "character_id": "character-chenxing",
                "character_name": "辰星",
                "selected_source": {
                    "source_page_id": "chenxing-source-page",
                    "armor_id": "",
                    "armor_name": "",
                    "armor_semantics": "selected_default_armor_voice_source",
                },
            }
        )

        for ordinal, (review_decision, reviewed_text, alternate_asr) in LEXICAL_CASES.items():
            span_id = f"voice-span-{ordinal:020x}"
            relative = f"clips/{ordinal:03d}-{span_id}.wav"
            clip_path = self.package_path.parent / relative
            self.clip_paths[ordinal] = clip_path
            frame_count = 240 + ordinal * 8
            audio_sha, byte_count = _write_wav(
                clip_path, frame_count=frame_count
            )
            duration = frame_count / 24000
            start_frame = ordinal * 1000
            end_frame = start_frame + frame_count
            audio_format = {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 24000,
                "channels": 1,
                "sample_width_bytes": 2,
                "slicing": "exact_half_open_pcm_frame_interval",
            }
            character_id = (
                "character-vidya" if ordinal <= 7 else "character-chenxing"
            )
            character_name = "薇蒂雅" if ordinal <= 7 else "辰星"
            slot = "A" if ordinal in {1, 2, 3, 4, 8, 9, 10, 11} else "B"
            self.package["clips"].append(
                {
                    "ordinal": ordinal,
                    "span_id": span_id,
                    "character_id": character_id,
                    "character_name": character_name,
                    "slot": slot,
                    "start_frame": start_frame,
                    "end_frame_exclusive": end_frame,
                    "frame_count": frame_count,
                    "duration_seconds": duration,
                    "clip_path": relative,
                    "output_wav_sha256": audio_sha,
                    "audio_format": audio_format,
                }
            )
            asr_text = alternate_asr or reviewed_text
            asr_sha = hashlib.sha256(asr_text.encode("utf-8")).hexdigest()
            self.run["clips"].append(
                {
                    "ordinal": ordinal,
                    "span_id": span_id,
                    "audio_sha256": audio_sha,
                    "audio_byte_count": byte_count,
                    "audio_format": {
                        **{
                            key: value
                            for key, value in audio_format.items()
                            if key != "slicing"
                        },
                        "frame_count": frame_count,
                        "duration_seconds": duration,
                    },
                    "source_range": {
                        "start_frame": start_frame,
                        "end_frame_exclusive": end_frame,
                        "sample_rate_hz": 24000,
                    },
                    "hypothesis_status": "pending_human_audio_review",
                    "text": asr_text,
                    "text_utf8_sha256": asr_sha,
                    "duration_seconds": duration,
                }
            )
            reviewed_sha = hashlib.sha256(
                reviewed_text.encode("utf-8")
            ).hexdigest()
            training_use = {
                "disposition": "not_assessed",
                "reason_code": None,
                "duplicate_of_ordinal": None,
            }
            if ordinal == routing.DUPLICATE_ORDINAL:
                training_use = {
                    "disposition": "excluded_from_current_training_set",
                    "reason_code": "duplicate_utterance_text",
                    "duplicate_of_ordinal": routing.DUPLICATE_OF_ORDINAL,
                }
            self.submission["decisions"].append(
                {
                    "ordinal": ordinal,
                    "span_id": span_id,
                    "audio_sha256": audio_sha,
                    "asr_hypothesis_text_sha256": asr_sha,
                    "transcript_review": {
                        "decision": review_decision,
                        "reviewed_text": reviewed_text,
                        "reviewed_text_utf8_sha256": reviewed_sha,
                        "reason_code": None,
                        "operator_note": None,
                    },
                    "training_use": training_use,
                }
            )

        self.package["clips"].sort(key=lambda item: item["ordinal"])
        self.run["clips"].sort(key=lambda item: item["ordinal"])
        self.submission["decisions"].sort(key=lambda item: item["ordinal"])
        self.queue["prediction_method"] = {
            "format": "pcm_s16le",
            "sample_rate_hz": 24000,
            "channels": 1,
            "join_silence_ms": 150,
            "silence_threshold_dbfs": -45,
            "silence_window_ms": 20,
            "minimum_qualifying_silence_run_ms": 200,
        }
        for character in self.queue["characters"]:
            character_clips = [
                clip
                for clip in self.package["clips"]
                if clip["character_id"] == character["character_id"]
            ]
            character["proposals"] = []
            for slot in ("A", "B"):
                slot_clips = [
                    clip for clip in character_clips if clip["slot"] == slot
                ]
                if not slot_clips:
                    continue
                duration = sum(
                    clip["duration_seconds"] for clip in slot_clips
                ) + 0.15 * (len(slot_clips) - 1)
                character["proposals"].append(
                    {
                        "slot": slot,
                        "cross_slot_overlap_candidate_ids": [],
                        "spans": [
                            {
                                "span_id": clip["span_id"],
                                "duration_seconds": clip["duration_seconds"],
                            }
                            for clip in slot_clips
                        ],
                        "predicted_composite_qc": {
                            "duration_seconds": round(duration, 6),
                            "status": "prediction_only",
                        },
                    }
                )
        self.queue, self.hashes["queue"] = _write_json(
            self.queue_path, self.queue
        )
        self._rebuild_all()

    def _rebuild_all(self) -> None:
        self.package["span_review_queue_sha256"] = self.hashes["queue"]
        self.package["clip_count"] = len(self.package["clips"])
        self.package, self.hashes["package"] = _write_json(
            self.package_path,
            self.package,
            hash_field="manifest_sha256",
        )
        self.run["source_package"] = {
            "package_id": PACKAGE_ID,
            "manifest_sha256": self.package["manifest_sha256"],
            "span_review_queue_sha256": self.hashes["queue"],
            "inventory_sha256": INVENTORY_SHA,
            "clip_count": len(self.run["clips"]),
        }
        self.run["input_audio_set_sha256"] = INPUT_AUDIO_SET_SHA
        self.run["transcript_set_sha256"] = TRANSCRIPT_SET_SHA
        self.run["clip_count"] = len(self.run["clips"])
        self.run, self.hashes["run"] = _write_json(
            self.run_path, self.run, hash_field="manifest_sha256"
        )
        self.submission["source_run"] = {
            "run_id": RUN_ID,
            "manifest_sha256": self.run["manifest_sha256"],
            "created_at": "2026-09-01T00:00:00+00:00",
            "source_package_manifest_sha256": self.package["manifest_sha256"],
            "input_audio_set_sha256": INPUT_AUDIO_SET_SHA,
            "transcript_set_sha256": TRANSCRIPT_SET_SHA,
            "clip_count": len(self.submission["decisions"]),
        }
        self.submission["clip_count"] = len(self.submission["decisions"])
        self.submission, self.hashes["submission"] = _write_json(
            self.submission_path,
            self.submission,
            hash_field="receipt_sha256",
        )

    def rewrite_submission(self) -> None:
        self.submission, self.hashes["submission"] = _write_json(
            self.submission_path,
            self.submission,
            hash_field="receipt_sha256",
        )

    def rebuild_run_and_submission(self) -> None:
        self.run, self.hashes["run"] = _write_json(
            self.run_path, self.run, hash_field="manifest_sha256"
        )
        self.submission["source_run"]["manifest_sha256"] = self.run[
            "manifest_sha256"
        ]
        self.rewrite_submission()

    def create_paralinguistic_receipt(self) -> dict:
        receipt, destination = self.build(recorded_at=RECORDED_AT)
        status, stored = para.write_receipt(self.root, receipt, destination)
        if status not in {"created", "existing_valid"}:
            raise AssertionError(status)
        self.paralinguistic_receipt = stored
        self.paralinguistic_byte_sha = hashlib.sha256(
            destination.read_bytes()
        ).hexdigest()
        return stored

    def build_routing(
        self, *, recorded_at: str = RECORDED_AT
    ) -> tuple[dict, Path]:
        if self.paralinguistic_receipt is None:
            self.create_paralinguistic_receipt()
        return routing.build_routing_receipt(
            self.root,
            SUBMISSION_ID,
            self.paralinguistic_receipt["review_id"],
            reviewer_id="xiaob",
            recorded_at=recorded_at,
            expected_submission_sha256=self.hashes["submission"],
            expected_run_sha256=self.hashes["run"],
            expected_package_sha256=self.hashes["package"],
            expected_queue_sha256=self.hashes["queue"],
            expected_paralinguistic_sha256=self.paralinguistic_byte_sha,
        )


class VoiceCorpusRoutingOpsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = RoutingFixture(Path(self.temporary.name) / "Voice")

    def test_dry_run_routes_every_span_exactly_once_without_writing(self) -> None:
        receipt, destination = self.fixture.build_routing()

        self.assertFalse(destination.parent.exists())
        self.assertEqual(receipt["summary"]["source_span_count"], 15)
        self.assertEqual(receipt["summary"]["transcript_resolved_count"], 13)
        self.assertEqual(receipt["summary"]["lexical_candidate_count"], 12)
        self.assertEqual(receipt["summary"]["paralinguistic_candidate_count"], 2)
        self.assertEqual(receipt["summary"]["duplicate_excluded_count"], 1)
        tracks = {
            route["ordinal"]: route["routing"]["track"]
            for route in receipt["routes"]
        }
        self.assertEqual(tracks[2], routing.TRACK_PARALINGUISTIC)
        self.assertEqual(tracks[3], routing.TRACK_PARALINGUISTIC)
        self.assertEqual(tracks[12], routing.TRACK_DUPLICATE)
        self.assertTrue(all(value is False for value in receipt["scope_limits"].values()))

    def test_write_validate_and_retry_are_immutable_and_idempotent(self) -> None:
        receipt, destination = self.fixture.build_routing()
        status, stored = routing.write_routing_receipt(
            self.fixture.root, receipt, destination
        )

        self.assertEqual(status, "created")
        validation = routing.validate_routing_receipt(
            self.fixture.root, receipt["routing_id"]
        )
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(validation["all_scope_gates_closed"])

        later, same_destination = self.fixture.build_routing(
            recorded_at="2026-09-03T05:00:00+00:00"
        )
        retry_status, retry_stored = routing.write_routing_receipt(
            self.fixture.root, later, same_destination
        )
        self.assertEqual(retry_status, "existing_valid")
        self.assertEqual(retry_stored, stored)

    def test_wrong_duplicate_target_is_rejected(self) -> None:
        decision = next(
            item
            for item in self.fixture.submission["decisions"]
            if item["ordinal"] == routing.DUPLICATE_ORDINAL
        )
        decision["training_use"]["duplicate_of_ordinal"] = 10
        self.fixture.rewrite_submission()
        self.fixture.create_paralinguistic_receipt()

        with self.assertRaisesRegex(
            routing.VoiceCorpusRoutingError, "duplicate target ordinal"
        ):
            self.fixture.build_routing()

    def test_duplicate_lexical_text_is_rejected_after_rehashed_sources(self) -> None:
        target_text = LEXICAL_CASES[11][1]
        target_sha = hashlib.sha256(target_text.encode("utf-8")).hexdigest()
        run_clip = next(
            item for item in self.fixture.run["clips"] if item["ordinal"] == 13
        )
        run_clip["text"] = target_text
        run_clip["text_utf8_sha256"] = target_sha
        decision = next(
            item
            for item in self.fixture.submission["decisions"]
            if item["ordinal"] == 13
        )
        decision["asr_hypothesis_text_sha256"] = target_sha
        decision["transcript_review"]["reviewed_text"] = target_text
        decision["transcript_review"]["reviewed_text_utf8_sha256"] = target_sha
        self.fixture.rebuild_run_and_submission()
        self.fixture.create_paralinguistic_receipt()

        with self.assertRaisesRegex(
            routing.VoiceCorpusRoutingError, "duplicate reviewed text"
        ):
            self.fixture.build_routing()

    def test_paralinguistic_receipt_tampering_is_rejected(self) -> None:
        receipt = self.fixture.create_paralinguistic_receipt()
        path = (
            self.fixture.root
            / para.OUTPUT_DIRECTORY
            / f"{receipt['review_id']}.json"
        )
        with path.open("ab") as stream:
            stream.write(b"tampered")

        with self.assertRaises(para.VoiceParalinguisticError):
            self.fixture.build_routing()

    def test_non_target_audio_tampering_is_rejected(self) -> None:
        self.fixture.create_paralinguistic_receipt()
        with self.fixture.clip_paths[1].open("ab") as stream:
            stream.write(b"tampered")

        with self.assertRaisesRegex(
            routing.VoiceCorpusRoutingError, "audio byte SHA-256"
        ):
            self.fixture.build_routing()

    def test_routing_scope_tampering_is_rejected_even_when_rehashed(self) -> None:
        receipt, destination = self.fixture.build_routing()
        routing.write_routing_receipt(self.fixture.root, receipt, destination)
        tampered = json.loads(destination.read_text(encoding="utf-8"))
        tampered["scope_limits"]["training_use_approved"] = True
        tampered["receipt_sha256"] = para._semantic_sha256(
            {
                key: value
                for key, value in tampered.items()
                if key != "receipt_sha256"
            }
        )
        destination.write_bytes(_pretty_bytes(tampered))

        with self.assertRaisesRegex(
            para.VoiceParalinguisticError,
            "training_use_approved must remain false",
        ):
            routing.validate_routing_receipt(
                self.fixture.root, receipt["routing_id"]
            )

    def test_cli_requires_candidate_only_confirmation_before_execute(self) -> None:
        receipt = self.fixture.create_paralinguistic_receipt()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = routing.main(
                [
                    "record",
                    "--voice-root",
                    str(self.fixture.root),
                    "--submission-id",
                    SUBMISSION_ID,
                    "--paralinguistic-review-id",
                    receipt["review_id"],
                    "--execute",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-candidate-routing-only", stderr.getvalue())
        self.assertFalse((self.fixture.root / routing.OUTPUT_DIRECTORY).exists())
