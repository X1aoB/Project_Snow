from __future__ import annotations

import hashlib
import io
import json
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from scripts import voice_provider_preflight_ops as preflight


def _pretty_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class PreflightFixture:
    def __init__(self, root: Path, *, open_review_gate: bool = False) -> None:
        self.root = root
        root.mkdir(parents=True)
        packages = []
        ordinal = 1
        for target in preflight.review.TARGETS:
            package_id = target["package_id"]
            package_directory = root / preflight.review.PACKAGE_DIRECTORY / package_id
            decisions = []
            for slot in ("A", "B"):
                text_payload = f"{target['character_name']} {slot} 本地参考文本".encode()
                audio_payload = f"wav-{target['character_id']}-{slot}".encode("ascii")
                text_relative = f"{slot}/displayed_text.txt"
                audio_relative = f"{slot}/compacted.wav"
                self._write(package_directory / text_relative, text_payload)
                self._write(package_directory / audio_relative, audio_payload)
                decisions.append(
                    {
                        "slot": slot,
                        "candidate_ordinal": ordinal,
                        "original_candidate_id": f"candidate-{ordinal}",
                        "displayed_text": {
                            "relative_path": text_relative,
                            "sha256": hashlib.sha256(text_payload).hexdigest(),
                            "byte_count": len(text_payload),
                        },
                        "selected_audio": {
                            "relative_path": audio_relative,
                            "sha256": hashlib.sha256(audio_payload).hexdigest(),
                            "pcm_sha256": hashlib.sha256(b"pcm:" + audio_payload).hexdigest(),
                            "byte_count": len(audio_payload),
                            "audio_format": {
                                "encoding": "pcm_s16le",
                                "sample_rate_hz": 24000,
                                "channels": 1,
                                "sample_width_bytes": 2,
                                "frame_count": 288000,
                                "duration_seconds": 12.0,
                            },
                        },
                        "selected_variant": "compacted",
                        "decision": "compacted_accepted_no_issue",
                        "issue_codes": [],
                    }
                )
                ordinal += 1
            packages.append(
                {
                    "package_id": package_id,
                    "runtime_character_id": target["character_id"],
                    "runtime_character_name": target["character_name"],
                    "relative_path": (f"{preflight.review.PACKAGE_DIRECTORY}/{package_id}/manifest.json"),
                    "decisions": decisions,
                }
            )
        review_limits = preflight.review._scope_limits()
        if open_review_gate:
            review_limits["provider_enrollment_allowed"] = True
        self.review_receipt = {
            "review_id": preflight.DEFAULT_REVIEW_ID,
            "receipt_sha256": "1" * 64,
            "decision_set_sha256": "2" * 64,
            "packages": packages,
            "summary": {
                "slot_count": 4,
                "compacted_accepted_no_issue_count": 4,
            },
            "approval_scope": {
                "human_listening_completed": True,
                "compacted_variant_selected_for_local_ab_candidates": True,
                "no_material_audible_difference_reported": True,
                "natural_masters_retained": True,
            },
            "scope_limits": review_limits,
        }
        self.review_path = root / preflight.review.OUTPUT_DIRECTORY / f"{preflight.DEFAULT_REVIEW_ID}.json"
        self._write(self.review_path, _pretty_bytes(self.review_receipt))
        self.event_receipt = {
            "review_id": preflight.DEFAULT_PARALINGUISTIC_REVIEW_ID,
            "receipt_sha256": "3" * 64,
            "event_set_sha256": "4" * 64,
            "events": [
                {
                    "ordinal": ordinal,
                    "classification": {
                        "base_tts_training": "excluded",
                        "event_bank_eligibility": "pending_human_event_qa",
                    },
                }
                for ordinal in (2, 3)
            ],
            "scope_limits": {
                "base_tts_training_approved": False,
                "event_bank_approved": False,
                "provider_enrollment_allowed": False,
            },
        }
        self.event_path = (
            root / preflight.base.OUTPUT_DIRECTORY / f"{preflight.DEFAULT_PARALINGUISTIC_REVIEW_ID}.json"
        )
        self._write(self.event_path, _pretty_bytes(self.event_receipt))

    @staticmethod
    def _write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def review_validation(self, root: Path, review_id: str) -> dict:
        self.assert_root(root)
        if review_id != preflight.DEFAULT_REVIEW_ID:
            raise AssertionError("unexpected review ID")
        payload = self.review_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
        return {
            "receipt_sha256": document["receipt_sha256"],
            "byte_sha256": hashlib.sha256(payload).hexdigest(),
        }

    def event_validation(self, root: Path, review_id: str) -> dict:
        self.assert_root(root)
        if review_id != preflight.DEFAULT_PARALINGUISTIC_REVIEW_ID:
            raise AssertionError("unexpected event review ID")
        payload = self.event_path.read_bytes()
        document = json.loads(payload.decode("utf-8"))
        return {
            "receipt_sha256": document["receipt_sha256"],
            "byte_sha256": hashlib.sha256(payload).hexdigest(),
            "target_ordinals": [2, 3],
        }

    def assert_root(self, root: Path) -> None:
        if Path(root) != self.root:
            raise AssertionError(f"unexpected root: {root}")

    @contextmanager
    def validators(self):
        with (
            patch.object(
                preflight.review,
                "validate_review_receipt",
                side_effect=self.review_validation,
            ),
            patch.object(
                preflight.base,
                "validate_receipt",
                side_effect=self.event_validation,
            ),
        ):
            yield


class VoiceProviderPreflightOpsTests(TestCase):
    PREPARED_AT = "2026-09-02T15:28:35.5396116+08:00"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Voice"
        self.fixture = PreflightFixture(self.root)

    def _build(self):
        return preflight.build_preflight(
            self.root,
            prepared_at=self.PREPARED_AT,
        )

    def test_dry_run_builds_four_independent_candidates_without_writing(self) -> None:
        with self.fixture.validators():
            artifacts, destination = self._build()
        self.assertFalse((self.root / preflight.OUTPUT_DIRECTORY).exists())
        self.assertFalse(destination.exists())
        manifest = artifacts["manifest"]
        self.assertEqual(
            [item["candidate_key"] for item in manifest["candidates"]],
            ["vidya-a", "vidya-b", "chenxing-a", "chenxing-b"],
        )
        self.assertTrue(all(state is False for state in manifest["scope_limits"].values()))
        self.assertFalse(manifest["provider_intent"]["credentials_read"])
        self.assertFalse(manifest["provider_intent"]["network_calls_performed"])
        self.assertTrue(
            all(item["isolation"]["not_concatenated_with_other_slots"] for item in manifest["candidates"])
        )
        plan = artifacts["plan"]
        self.assertEqual(plan["status"], "planned_not_rendered")
        self.assertEqual(
            plan["paralinguistic_event_lane"]["current_action"],
            "do_not_submit_as_enrollment_text_or_reference_audio",
        )
        self.assertEqual(
            [len(item["cases"]) for item in plan["lexical_test_prompts"]],
            [6, 6],
        )

    def test_execute_is_atomic_idempotent_and_reconstructable(self) -> None:
        with self.fixture.validators():
            artifacts, destination = self._build()
            first_status, _ = preflight.write_preflight(self.root, artifacts, destination)
            second_status, _ = preflight.write_preflight(self.root, artifacts, destination)
            validation = preflight.validate_preflight(self.root, artifacts["manifest"]["preflight_id"])
        self.assertEqual(first_status, "created")
        self.assertEqual(second_status, "existing_valid")
        self.assertEqual(validation["status"], "valid")
        self.assertFalse(validation["provider_interactions_performed"])
        self.assertEqual(
            {item.name for item in destination.iterdir()},
            {"manifest.json", "blind_test_plan.json", "README.md"},
        )

    def test_reference_audio_tamper_is_detected(self) -> None:
        with self.fixture.validators():
            artifacts, destination = self._build()
            preflight.write_preflight(self.root, artifacts, destination)
            audio = self.root.joinpath(
                *artifacts["manifest"]["candidates"][0]["reference_audio"]["relative_path"].split("/")
            )
            audio.write_bytes(audio.read_bytes() + b"tamper")
            with self.assertRaisesRegex(preflight.VoiceProviderPreflightError, "audio SHA-256"):
                preflight.validate_preflight(self.root, artifacts["manifest"]["preflight_id"])

    def test_blind_plan_tamper_is_detected(self) -> None:
        with self.fixture.validators():
            artifacts, destination = self._build()
            preflight.write_preflight(self.root, artifacts, destination)
            plan_path = destination / "blind_test_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["status"] = "tampered"
            plan_path.write_bytes(_pretty_bytes(plan))
            with self.assertRaisesRegex(
                preflight.VoiceProviderPreflightError,
                "blind test plan semantic SHA-256",
            ):
                preflight.validate_preflight(self.root, artifacts["manifest"]["preflight_id"])

    def test_open_source_gate_fails_closed(self) -> None:
        alternate_root = Path(self.temporary.name) / "OpenGateVoice"
        fixture = PreflightFixture(alternate_root, open_review_gate=True)
        with (
            fixture.validators(),
            self.assertRaisesRegex(
                preflight.base.VoiceParalinguisticError,
                "provider_enrollment_allowed must remain false",
            ),
        ):
            preflight.build_preflight(
                alternate_root,
                prepared_at=self.PREPARED_AT,
            )

    def test_modified_in_memory_plan_is_rejected_before_output_creation(self) -> None:
        with self.fixture.validators():
            artifacts, destination = self._build()
            artifacts["plan"]["status"] = "modified"
            with self.assertRaisesRegex(
                preflight.VoiceProviderPreflightError,
                "in-memory blind test plan semantic SHA-256",
            ):
                preflight.write_preflight(self.root, artifacts, destination)
        self.assertFalse((self.root / preflight.OUTPUT_DIRECTORY).exists())

    def test_execute_requires_explicit_offline_confirmation(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = preflight.main(
                [
                    "prepare",
                    "--voice-root",
                    str(self.root),
                    "--prepared-at",
                    self.PREPARED_AT,
                    "--execute",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("--execute requires --confirm-offline-only", stderr.getvalue())
        self.assertFalse((self.root / preflight.OUTPUT_DIRECTORY).exists())
