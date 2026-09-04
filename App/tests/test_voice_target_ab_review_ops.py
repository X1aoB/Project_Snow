from __future__ import annotations

import hashlib
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase

from scripts import voice_target_ab_review_ops as review


def _pretty_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_manifest(path: Path, value: dict) -> tuple[dict, str]:
    document = dict(value)
    document["manifest_sha256"] = review.base._semantic_sha256(document)
    payload = _pretty_bytes(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return document, hashlib.sha256(payload).hexdigest()


class ReviewFixture:
    def __init__(
        self,
        root: Path,
        *,
        overlap: bool = False,
        open_source_gate: bool = False,
    ) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.policy = {
            "duration_seconds": {"minimum": 10.0, "maximum": 20.0},
            "normal_pause_allowance_per_quiet_run_ms": 750,
        }
        self.boundary = {
            "receipt_id": "voice-recording-boundary-adjudication-" + "a" * 20,
            "manifest_sha256": "b" * 64,
            "decision_set_sha256": "c" * 64,
            "candidate_count": 4,
            "accepted_candidate_count": 4,
            "excluded_candidate_count": 0,
        }
        self.package_hashes: dict[str, str] = {}
        package_entries = []
        for target_index, target in enumerate(review.TARGETS):
            package, byte_hash = self._make_package(
                target,
                target_index=target_index,
                overlap=overlap and target_index == 1,
                open_source_gate=open_source_gate and target_index == 0,
            )
            self.package_hashes[target["package_id"]] = byte_hash
            package_entries.append(
                {
                    "profile_id": f"voice-dialogue-profile-{target_index + 1:020x}",
                    "runtime_character_id": target["character_id"],
                    "runtime_character_name": target["character_name"],
                    "package_id": target["package_id"],
                    "manifest_sha256": package["manifest_sha256"],
                }
            )
        batch = {
            "schema_version": review.BATCH_SCHEMA,
            "batch_id": review.DEFAULT_BATCH_ID,
            "source_corpus_analysis": {
                "analysis_id": "voice-recording-dialogue-corpus-analysis-" + "d" * 20,
                "manifest_sha256": "e" * 64,
            },
            "source_policy_review": {
                "review_id": "voice-recording-dialogue-review-" + "f" * 20,
                "manifest_sha256": "1" * 64,
                "approved_policy_sha256": review.base._semantic_sha256(self.policy),
            },
            "source_boundary_adjudication": self.boundary,
            "packages": package_entries,
            "scope_limits": review._scope_limits(),
            "executed": True,
        }
        batch_path = (
            root
            / review.BATCH_DIRECTORY
            / review.DEFAULT_BATCH_ID
            / "manifest.json"
        )
        self.batch, self.batch_hash = _write_manifest(batch_path, batch)

    def _write_asset(self, directory: Path, relative: str, payload: bytes) -> dict:
        path = directory.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {
            "relative_path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_count": len(payload),
        }

    def _audio(
        self,
        directory: Path,
        relative: str,
        payload: bytes,
        *,
        duration: float,
    ) -> dict:
        asset = self._write_asset(directory, relative, payload)
        return {
            "relative_path": relative,
            "wav_sha256": asset["sha256"],
            "pcm_sha256": hashlib.sha256(b"pcm:" + payload).hexdigest(),
            "byte_count": asset["byte_count"],
            "audio_format": {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 24000,
                "channels": 1,
                "sample_width_bytes": 2,
                "frame_count": int(duration * 24000),
                "duration_seconds": duration,
            },
        }

    def _make_sample(
        self,
        directory: Path,
        target: dict[str, str],
        *,
        target_index: int,
        slot: str,
        overlap: bool,
    ) -> dict:
        slot_index = 0 if slot == "A" else 1
        text_payload = f"{target['character_name']} {slot} 屏幕文本".encode()
        text_asset = self._write_asset(
            directory, f"{slot}/displayed_text.txt", text_payload
        )
        natural_payload = f"natural-{target_index}-{slot}".encode("ascii")
        cut_count = 0 if target_index == 0 and slot == "B" else 1
        compacted_payload = (
            natural_payload
            if cut_count == 0
            else f"compacted-{target_index}-{slot}".encode("ascii")
        )
        natural = self._audio(
            directory,
            f"{slot}/natural.wav",
            natural_payload,
            duration=12.5,
        )
        compacted = self._audio(
            directory,
            f"{slot}/compacted.wav",
            compacted_payload,
            duration=12.5 if cut_count == 0 else 11.5,
        )
        start = 0 if slot == "A" else (50 if overlap else 200)
        end = 100 if slot == "A" else 300
        input_hash = hashlib.sha256(
            f"input-{target_index}-{slot}".encode("ascii")
        ).hexdigest()
        return {
            "slot": slot,
            "parent_candidate_count": 1,
            "internal_silence_compaction_cut_count": cut_count,
            "candidate_ordinal": target_index * 10 + slot_index + 1,
            "original_candidate_id": f"candidate-{target_index}-{slot}",
            "source_id": f"source-{target_index}",
            "runtime_character_id": target["character_id"],
            "runtime_character_name": target["character_name"],
            "authoritative_transcript_utf8_sha256": text_asset["sha256"],
            "displayed_text": {
                "relative_path": text_asset["relative_path"],
                "utf8_sha256": text_asset["sha256"],
                "byte_count": text_asset["byte_count"],
            },
            "input_candidate_audio": {
                "wav_sha256": input_hash,
                "source_frame_range": {
                    "start_frame": start,
                    "end_frame_exclusive": end,
                },
            },
            "internal_silence_compaction_cuts": [
                {"cut": 1} for _ in range(cut_count)
            ],
            "natural_audio": natural,
            "compacted_audio": compacted,
            "quality_gates": {
                "duration_10_to_20_seconds": True,
                "true_peak_at_or_below_minus_1_dbtp": True,
                "no_clipped_samples": True,
                "sustained_silence_excess_at_or_below_20_percent": True,
                "snr_at_or_above_25_db": True,
            },
            "all_quality_gates_passed": True,
        }

    def _make_package(
        self,
        target: dict[str, str],
        *,
        target_index: int,
        overlap: bool,
        open_source_gate: bool,
    ) -> tuple[dict, str]:
        directory = self.root / review.PACKAGE_DIRECTORY / target["package_id"]
        review_page = self._write_asset(
            directory, "review.html", f"review-{target_index}".encode("ascii")
        )
        samples = [
            self._make_sample(
                directory,
                target,
                target_index=target_index,
                slot=slot,
                overlap=overlap,
            )
            for slot in ("A", "B")
        ]
        scope_limits = {
            "human_listening_approved": False,
            "natural_vs_compacted_comparison_approved": False,
            **review._scope_limits(),
        }
        if open_source_gate:
            scope_limits["training_use_approved"] = True
        package = {
            "schema_version": review.PACKAGE_SCHEMA,
            "package_id": target["package_id"],
            "source_boundary_adjudication": self.boundary,
            "quality_policy": self.policy,
            "sample_count": 2,
            "samples": samples,
            "review_page": review_page,
            "review_status": "awaiting_human_natural_vs_compacted_comparison",
            "scope_limits": scope_limits,
            "executed": True,
        }
        return _write_manifest(directory / "manifest.json", package)

    def build(self) -> tuple[dict, Path]:
        return review.build_review_receipt(
            self.root,
            reviewed_at="2026-09-02T13:25:58.6728955+08:00",
            expected_batch_sha256=self.batch_hash,
            expected_package_sha256=self.package_hashes,
        )


class VoiceTargetABReviewOpsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = ReviewFixture(Path(self.temporary.name) / "Voice")

    def test_dry_run_verifies_four_slots_without_writing(self) -> None:
        receipt, destination = self.fixture.build()

        self.assertFalse(destination.parent.exists())
        self.assertEqual(receipt["summary"]["slot_count"], 4)
        self.assertEqual(receipt["summary"]["verified_asset_count"], 14)
        self.assertEqual(
            receipt["summary"]["natural_and_compacted_identical_count"], 1
        )
        self.assertTrue(all(value is False for value in receipt["scope_limits"].values()))

    def test_execute_validate_and_replay_are_idempotent(self) -> None:
        receipt, destination = self.fixture.build()

        status, stored = review.write_review_receipt(
            self.fixture.root, receipt, destination
        )
        self.assertEqual(status, "created")
        validation = review.validate_review_receipt(
            self.fixture.root, receipt["review_id"]
        )
        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["verified_asset_count"], 14)

        status, replay = review.write_review_receipt(
            self.fixture.root, receipt, destination
        )
        self.assertEqual(status, "existing_valid")
        self.assertEqual(stored, replay)
        self.assertEqual(list(destination.parent.glob("*.partial")), [])

    def test_selected_audio_tampering_is_rejected(self) -> None:
        target = review.TARGETS[0]
        path = (
            self.fixture.root
            / review.PACKAGE_DIRECTORY
            / target["package_id"]
            / "A"
            / "compacted.wav"
        )
        path.write_bytes(path.read_bytes() + b"tampered")

        with self.assertRaisesRegex(review.VoiceTargetABReviewError, "SHA-256"):
            self.fixture.build()

    def test_overlapping_source_ranges_are_rejected(self) -> None:
        fixture = ReviewFixture(
            Path(self.temporary.name) / "OverlapVoice", overlap=True
        )

        with self.assertRaisesRegex(
            review.VoiceTargetABReviewError, "source frame ranges overlap"
        ):
            fixture.build()

    def test_open_source_gate_is_rejected(self) -> None:
        fixture = ReviewFixture(
            Path(self.temporary.name) / "OpenGateVoice", open_source_gate=True
        )

        with self.assertRaisesRegex(
            review.base.VoiceParalinguisticError, "must remain false"
        ):
            fixture.build()

    def test_wrong_user_statement_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            review.VoiceTargetABReviewError, "exact user approval statement"
        ):
            review.build_review_receipt(
                self.fixture.root,
                reviewed_at="2026-09-02T13:25:58.6728955+08:00",
                user_statement="not approved",
            )

    def test_cli_execute_requires_explicit_confirmation(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = review.main(
                [
                    "record",
                    "--voice-root",
                    str(self.fixture.root),
                    "--reviewed-at",
                    "2026-09-02T13:25:58.6728955+08:00",
                    "--execute",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("--confirm-four-compacted-selections", stderr.getvalue())
        self.assertFalse((self.fixture.root / review.OUTPUT_DIRECTORY).exists())
