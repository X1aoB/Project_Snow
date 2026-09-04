from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from scripts import voice_paralinguistic_ops as ops

SUBMISSION_ID = "voice-span-asr-review-submission-11111111111111111111"
RUN_ID = "voice-span-asr-run-22222222222222222222"
PACKAGE_ID = "voice-span-review-package-33333333333333333333"
CREATED_AT = "2026-09-01T08:00:00+08:00"
RECORDED_AT = "2026-09-01T00:30:00+00:00"
INVENTORY_SHA = "4" * 64
INPUT_AUDIO_SET_SHA = "5" * 64
TRANSCRIPT_SET_SHA = "6" * 64


def _pretty_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_json(path: Path, value: dict, *, hash_field: str | None = None) -> tuple[dict, str]:
    document = json.loads(json.dumps(value, ensure_ascii=False))
    if hash_field is not None:
        document.pop(hash_field, None)
        document[hash_field] = ops._semantic_sha256(document)
    payload = _pretty_bytes(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return document, hashlib.sha256(payload).hexdigest()


def _write_wav(path: Path, *, frame_count: int) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x00\x00" * frame_count)
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


class VoiceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.queue_path = root / "operator_span_review_queue.json"
        self.package_path = root / "span_review_packages" / PACKAGE_ID / "manifest.json"
        self.run_path = root / "span_asr_runs" / f"{RUN_ID}.json"
        self.submission_path = root / "span_asr_review_submissions" / f"{SUBMISSION_ID}.json"
        self.clip_paths: dict[int, Path] = {}
        self.hashes: dict[str, str] = {}
        self._create()

    def _create(self) -> None:
        self.root.mkdir(parents=True)
        queue = {
            "schema_version": ops.QUEUE_SCHEMA,
            "status": "needs_human_span_and_transcript_alignment_review",
            "scope_limits": {
                "audio_files_created": False,
                "source_files_modified": False,
                "span_approved": False,
                "rights_accepted": False,
                "publication_approved": False,
                "provider_enrollment_allowed": False,
            },
            "characters": [
                {
                    "character_id": "character-vidya",
                    "character_name": "薇蒂雅",
                    "selected_source": {
                        "source_page_id": "source-page",
                        "armor_id": "armor-tequila",
                        "armor_name": "龙舌兰",
                        "armor_semantics": "explicit_selected_armor_voice_source",
                    },
                }
            ],
        }
        self.queue, self.hashes["queue"] = _write_json(self.queue_path, queue)

        package_clips = []
        run_clips = []
        decisions = []
        for ordinal, surface, frame_count, start_frame in (
            (2, "啊啊啊", 240, 1000),
            (3, "啊", 480, 2000),
        ):
            span_id = f"voice-span-{ordinal:020x}"
            relative = f"clips/{ordinal:03d}-{span_id}.wav"
            clip_path = self.package_path.parent / relative
            self.clip_paths[ordinal] = clip_path
            audio_sha, byte_count = _write_wav(clip_path, frame_count=frame_count)
            duration = frame_count / 24000
            end_frame = start_frame + frame_count
            audio_format = {
                "encoding": "pcm_s16le",
                "sample_rate_hz": 24000,
                "channels": 1,
                "sample_width_bytes": 2,
                "slicing": "exact_half_open_pcm_frame_interval",
            }
            package_clips.append(
                {
                    "ordinal": ordinal,
                    "span_id": span_id,
                    "character_id": "character-vidya",
                    "character_name": "薇蒂雅",
                    "slot": "A",
                    "start_frame": start_frame,
                    "end_frame_exclusive": end_frame,
                    "frame_count": frame_count,
                    "duration_seconds": duration,
                    "clip_path": relative,
                    "output_wav_sha256": audio_sha,
                    "audio_format": audio_format,
                }
            )
            surface_sha = hashlib.sha256(surface.encode("utf-8")).hexdigest()
            run_clips.append(
                {
                    "ordinal": ordinal,
                    "span_id": span_id,
                    "audio_sha256": audio_sha,
                    "audio_byte_count": byte_count,
                    "audio_format": {
                        **{key: value for key, value in audio_format.items() if key != "slicing"},
                        "frame_count": frame_count,
                        "duration_seconds": duration,
                    },
                    "source_range": {
                        "start_frame": start_frame,
                        "end_frame_exclusive": end_frame,
                        "sample_rate_hz": 24000,
                    },
                    "hypothesis_status": "pending_human_audio_review",
                    "text": surface,
                    "text_utf8_sha256": surface_sha,
                    "duration_seconds": duration,
                }
            )
            decisions.append(
                {
                    "ordinal": ordinal,
                    "span_id": span_id,
                    "audio_sha256": audio_sha,
                    "asr_hypothesis_text_sha256": surface_sha,
                    "transcript_review": {
                        "decision": "needs_clarification",
                        "reviewed_text": None,
                        "reviewed_text_utf8_sha256": None,
                        "reason_code": "needs_exact_transcript_decision",
                        "operator_note": "只有发出的啊啊声，不确定是否能用于训练",
                    },
                    "training_use": {
                        "disposition": "excluded_from_current_training_set",
                        "reason_code": "nonlexical_vocalization",
                        "duplicate_of_ordinal": None,
                    },
                }
            )

        package = {
            "schema_version": ops.PACKAGE_SCHEMA,
            "package_id": PACKAGE_ID,
            "span_review_queue_sha256": self.hashes["queue"],
            "inventory_sha256": INVENTORY_SHA,
            "review_status": "pending_human_listening",
            "materialization_authorization": {
                "authorization_scope": "local_human_span_listening_only",
                "review_clip_materialization_allowed": True,
                "source_files_may_be_modified": False,
                "span_review_may_be_marked_complete": False,
                "concatenation_allowed": False,
                "rights_accepted": False,
                "publication_approved": False,
                "provider_enrollment_allowed": False,
            },
            "scope_limits": {
                "source_files_modified": False,
                "span_approved": False,
                "human_span_review_completed": False,
                "rights_accepted": False,
                "publication_approved": False,
                "provider_enrollment_allowed": False,
            },
            "clip_count": len(package_clips),
            "clips": package_clips,
        }
        self.package, self.hashes["package"] = _write_json(
            self.package_path, package, hash_field="manifest_sha256"
        )

        run = {
            "schema_version": ops.RUN_SCHEMA,
            "run_id": RUN_ID,
            "created_at": CREATED_AT,
            "source_package": {
                "package_id": PACKAGE_ID,
                "manifest_sha256": self.package["manifest_sha256"],
                "span_review_queue_sha256": self.hashes["queue"],
                "inventory_sha256": INVENTORY_SHA,
                "clip_count": len(run_clips),
            },
            "input_policy": {
                "input_kind": "wav_bytes_only",
                "prompt_used": False,
                "reference_transcript_used": False,
                "hotwords_used": False,
                "character_metadata_used": False,
                "cross_clip_context_used": False,
                "external_audio_disclosure": False,
            },
            "input_audio_set_sha256": INPUT_AUDIO_SET_SHA,
            "transcript_set_sha256": TRANSCRIPT_SET_SHA,
            "clip_count": len(run_clips),
            "clips": run_clips,
            "scope_limits": {
                "human_transcript_review_completed": False,
                "transcript_accepted": False,
                "span_approved": False,
                "rights_accepted": False,
                "publication_approved": False,
                "provider_enrollment_allowed": False,
            },
        }
        self.run, self.hashes["run"] = _write_json(self.run_path, run, hash_field="manifest_sha256")

        submission = {
            "schema_version": ops.SUBMISSION_SCHEMA,
            "submission_id": SUBMISSION_ID,
            "recorded_at": "2026-09-01T00:10:00+00:00",
            "reviewer_id": "xiaob",
            "source_run": {
                "run_id": RUN_ID,
                "manifest_sha256": self.run["manifest_sha256"],
                "created_at": "2026-09-01T00:00:00+00:00",
                "source_package_manifest_sha256": self.package["manifest_sha256"],
                "input_audio_set_sha256": INPUT_AUDIO_SET_SHA,
                "transcript_set_sha256": TRANSCRIPT_SET_SHA,
                "clip_count": len(decisions),
            },
            "reviewer_assertions": {"audio_only_review": True},
            "clip_count": len(decisions),
            "decisions": decisions,
            "review_status": "needs_clarification",
            "unresolved_ordinals": [2, 3],
            "scope_limits": {
                "human_transcript_review_completed": False,
                "transcript_accepted": False,
                "span_approved": False,
                "training_use_approved": False,
                "rights_accepted": False,
                "publication_approved": False,
                "provider_enrollment_allowed": False,
            },
        }
        self.submission, self.hashes["submission"] = _write_json(
            self.submission_path, submission, hash_field="receipt_sha256"
        )

    def rebuild_run_and_submission(self) -> None:
        self.run, self.hashes["run"] = _write_json(
            self.run_path, self.run, hash_field="manifest_sha256"
        )
        self.submission["source_run"]["manifest_sha256"] = self.run["manifest_sha256"]
        self.submission, self.hashes["submission"] = _write_json(
            self.submission_path, self.submission, hash_field="receipt_sha256"
        )

    def build(self, *, recorded_at: str = RECORDED_AT) -> tuple[dict, Path]:
        return ops.build_receipt(
            self.root,
            SUBMISSION_ID,
            reviewer_id="xiaob",
            recorded_at=recorded_at,
            expected_submission_sha256=self.hashes["submission"],
            expected_run_sha256=self.hashes["run"],
            expected_package_sha256=self.hashes["package"],
            expected_queue_sha256=self.hashes["queue"],
        )


class VoiceParalinguisticOpsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = VoiceFixture(Path(self.temporary.name) / "Voice")

    def test_dry_run_builds_exact_retention_receipt_without_writing(self) -> None:
        receipt, destination = self.fixture.build()

        self.assertFalse(destination.parent.exists())
        self.assertEqual([event["ordinal"] for event in receipt["events"]], [2, 3])
        self.assertEqual(
            [event["observation"]["phonetic_surface"] for event in receipt["events"]],
            ["啊啊啊", "啊"],
        )
        self.assertTrue(
            all(
                event["classification"]["base_tts_training"] == "excluded"
                for event in receipt["events"]
            )
        )
        self.assertTrue(all(value is False for value in receipt["scope_limits"].values()))

    def test_write_validate_and_later_retry_are_immutable_and_idempotent(self) -> None:
        receipt, destination = self.fixture.build()
        status, stored = ops.write_receipt(self.fixture.root, receipt, destination)

        self.assertEqual(status, "created")
        validation = ops.validate_receipt(self.fixture.root, receipt["review_id"])
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(validation["all_scope_gates_closed"])

        later, same_destination = self.fixture.build(recorded_at="2026-09-02T00:30:00+00:00")
        retry_status, retry_stored = ops.write_receipt(self.fixture.root, later, same_destination)
        self.assertEqual(retry_status, "existing_valid")
        self.assertEqual(retry_stored, stored)
        self.assertNotEqual(later["receipt_sha256"], retry_stored["receipt_sha256"])

    def test_audio_tampering_is_rejected(self) -> None:
        with self.fixture.clip_paths[2].open("ab") as stream:
            stream.write(b"tampered")

        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "audio byte SHA-256"):
            self.fixture.build()

    def test_duplicate_json_keys_are_rejected(self) -> None:
        original = self.fixture.submission_path.read_text(encoding="utf-8")
        self.fixture.submission_path.write_text(
            '{"schema_version":"duplicate",' + original[1:], encoding="utf-8"
        )

        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "duplicate key"):
            ops.build_receipt(
                self.fixture.root,
                SUBMISSION_ID,
                reviewer_id="xiaob",
                recorded_at=RECORDED_AT,
            )

    def test_opened_source_scope_gate_is_rejected_even_with_rehashed_submission(self) -> None:
        self.fixture.submission["scope_limits"]["rights_accepted"] = True
        self.fixture.submission, self.fixture.hashes["submission"] = _write_json(
            self.fixture.submission_path,
            self.fixture.submission,
            hash_field="receipt_sha256",
        )

        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "rights_accepted must remain false"):
            self.fixture.build()

    def test_lexical_surface_is_rejected_even_when_hash_chain_is_rebuilt(self) -> None:
        run_clip = next(item for item in self.fixture.run["clips"] if item["ordinal"] == 2)
        run_clip["text"] = "你好"
        surface_sha = hashlib.sha256("你好".encode()).hexdigest()
        run_clip["text_utf8_sha256"] = surface_sha
        decision = next(item for item in self.fixture.submission["decisions"] if item["ordinal"] == 2)
        decision["asr_hypothesis_text_sha256"] = surface_sha
        self.fixture.rebuild_run_and_submission()

        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "nonlexical surface"):
            self.fixture.build()

    def test_unexpected_unresolved_ordinal_set_is_rejected(self) -> None:
        self.fixture.submission["unresolved_ordinals"] = [2]
        self.fixture.submission, self.fixture.hashes["submission"] = _write_json(
            self.fixture.submission_path,
            self.fixture.submission,
            hash_field="receipt_sha256",
        )

        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "unresolved ordinals"):
            self.fixture.build()

    def test_symlinked_audio_is_rejected(self) -> None:
        clip_path = self.fixture.clip_paths[2]
        outside = Path(self.temporary.name) / "outside.wav"
        outside.write_bytes(clip_path.read_bytes())
        clip_path.unlink()
        try:
            os.symlink(outside, clip_path)
        except OSError as error:
            self.skipTest(f"symlink creation is unavailable: {error}")

        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "link or reparse point"):
            self.fixture.build()

    def test_parent_directory_escape_is_rejected_without_filesystem_privileges(self) -> None:
        with self.assertRaisesRegex(ops.VoiceParalinguisticError, "safe relative path"):
            ops._safe_relative_path("../outside.wav", label="test clip path")

    def test_existing_invalid_receipt_is_never_overwritten(self) -> None:
        receipt, destination = self.fixture.build()
        destination.parent.mkdir()
        original = b'{"unrelated":true}\n'
        destination.write_bytes(original)

        with self.assertRaises(ops.VoiceParalinguisticError):
            ops.write_receipt(self.fixture.root, receipt, destination)
        self.assertEqual(destination.read_bytes(), original)

    def test_racing_identical_writer_is_accepted_without_overwrite(self) -> None:
        receipt, destination = self.fixture.build()
        real_link = os.link

        def rival_link(source: Path, target: Path) -> None:
            real_link(source, target)
            raise FileExistsError(target)

        with patch.object(ops.os, "link", side_effect=rival_link):
            status, stored = ops.write_receipt(self.fixture.root, receipt, destination)

        self.assertEqual(status, "existing_valid")
        self.assertEqual(stored, receipt)
        self.assertEqual(ops.validate_receipt(self.fixture.root, receipt["review_id"])["status"], "valid")

    def test_receipt_tampering_is_detected(self) -> None:
        receipt, destination = self.fixture.build()
        ops.write_receipt(self.fixture.root, receipt, destination)
        tampered = json.loads(destination.read_text(encoding="utf-8"))
        tampered["events"][0]["classification"]["base_tts_training"] = "included"
        tampered["receipt_sha256"] = ops._semantic_sha256(
            {key: value for key, value in tampered.items() if key != "receipt_sha256"}
        )
        destination.write_bytes(_pretty_bytes(tampered))

        with self.assertRaises(ops.VoiceParalinguisticError):
            ops.validate_receipt(self.fixture.root, receipt["review_id"])

    def test_cli_requires_explicit_confirmation_before_execute(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = ops.main(
                [
                    "record",
                    "--voice-root",
                    str(self.fixture.root),
                    "--submission-id",
                    SUBMISSION_ID,
                    "--execute",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-retain-as-paralinguistic-only", stderr.getvalue())
        self.assertFalse((self.fixture.root / ops.OUTPUT_DIRECTORY).exists())
