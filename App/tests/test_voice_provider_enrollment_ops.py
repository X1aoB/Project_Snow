from __future__ import annotations

import hashlib
import io
import json
import tempfile
import wave
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from scripts import voice_provider_enrollment_ops as enrollment


def _wav_bytes(seed: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24_000)
        frame = int(seed).to_bytes(2, "little", signed=True)
        writer.writeframes(frame * (24_000 * 12))
    return output.getvalue()


class EnrollmentFixture:
    PREPARED_AT = "2026-09-02T18:00:00+08:00"
    PREFLIGHT_ID = "voice-provider-preflight-277b384f4a1451063562"
    PREFLIGHT_BYTE_SHA256 = "d" * 64
    PREFLIGHT_MANIFEST_SHA256 = "a" * 64

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        candidates = []
        definitions = (
            ("vidya-a", "5157b8972632", "薇蒂雅", "vidya", 1),
            ("vidya-b", "5157b8972632", "薇蒂雅", "vidya", 2),
            ("chenxing-a", "98322bd505f4", "辰星", "chenxing", 3),
            ("chenxing-b", "98322bd505f4", "辰星", "chenxing", 4),
        )
        for key, character_id, character_name, slug, seed in definitions:
            audio = _wav_bytes(seed)
            transcript = f"{character_name} {key} 的精确登记台词。\n".encode()
            audio_relative = f"fixtures/{key}/compacted.wav"
            transcript_relative = f"fixtures/{key}/displayed_text.txt"
            self._write(root / audio_relative, audio)
            self._write(root / transcript_relative, transcript)
            candidates.append(
                {
                    "candidate_id": f"project-snow-{key}-{hashlib.sha256(audio).hexdigest()[:8]}",
                    "candidate_key": key,
                    "character": {
                        "runtime_character_id": character_id,
                        "runtime_character_name": character_name,
                        "character_slug": slug,
                    },
                    "enrollment_text": {
                        "relative_path": transcript_relative,
                        "utf8_sha256": hashlib.sha256(transcript).hexdigest(),
                        "byte_count": len(transcript),
                    },
                    "reference_audio": {
                        "relative_path": audio_relative,
                        "wav_sha256": hashlib.sha256(audio).hexdigest(),
                        "byte_count": len(audio),
                        "audio_format": {
                            "encoding": "pcm_s16le",
                            "sample_rate_hz": 24_000,
                            "channels": 1,
                            "sample_width_bytes": 2,
                            "frame_count": 24_000 * 12,
                            "duration_seconds": 12.0,
                        },
                    },
                }
            )
        self.preflight_manifest = {
            "manifest_sha256": self.PREFLIGHT_MANIFEST_SHA256,
            "candidates": candidates,
        }
        self.preflight_validation = {
            "manifest_sha256": self.PREFLIGHT_MANIFEST_SHA256,
            "manifest_byte_sha256": self.PREFLIGHT_BYTE_SHA256,
            "candidate_count": 4,
            "provider_interactions_performed": False,
        }

    @staticmethod
    def _write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    @contextmanager
    def preflight(self):
        with patch.object(
            enrollment,
            "_load_preflight",
            return_value=(self.preflight_manifest, self.preflight_validation),
        ):
            yield

    def build_and_write(self, *, region: str = enrollment.REGION) -> tuple[dict, Path]:
        with self.preflight():
            artifacts, destination = enrollment.build_readiness(
                self.root,
                preflight_id=self.PREFLIGHT_ID,
                expected_preflight_manifest_byte_sha256=self.PREFLIGHT_BYTE_SHA256,
                prepared_at=self.PREPARED_AT,
                region=region,
            )
            status, _ = enrollment.write_readiness(self.root, artifacts, destination)
        if status != "created":
            raise AssertionError(f"unexpected write status: {status}")
        return artifacts, destination


class VoiceProviderEnrollmentOpsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Voice"
        self.fixture = EnrollmentFixture(self.root)

    def _confirmations(self, run_id: str, candidate_key: str) -> dict:
        return {
            "workspace_id": "project-snow-sg",
            "api_key_file": None,
            "expected_run_manifest_byte_sha256": None,
            "confirm_run_id": run_id,
            "confirm_candidate_key": candidate_key,
            "confirm_model": enrollment.MODEL,
            "confirm_region": enrollment.REGION,
            "confirm_cost_ceiling_usd": "0.04",
            "confirm_external_upload_to_singapore": True,
            "confirm_unverified_fanwork_source_risk": True,
            "confirm_provider_terms_and_voice_cloning_consent": True,
            "confirm_undocumented_source_audio_retention": True,
        }

    def test_prepare_pins_four_bounded_names_and_remains_offline(self) -> None:
        with self.fixture.preflight():
            artifacts, destination = enrollment.build_readiness(
                self.root,
                preflight_id=self.fixture.PREFLIGHT_ID,
                expected_preflight_manifest_byte_sha256=self.fixture.PREFLIGHT_BYTE_SHA256,
                prepared_at=self.fixture.PREPARED_AT,
            )
        manifest = artifacts["manifest"]
        self.assertFalse(destination.exists())
        self.assertEqual(
            [item["candidate_key"] for item in manifest["candidates"]],
            ["vidya-a", "vidya-b", "chenxing-a", "chenxing-b"],
        )
        self.assertTrue(all(len(item["preferred_name"]) <= 16 for item in manifest["candidates"]))
        self.assertTrue(all(value is False for value in manifest["scope_limits"].values()))
        self.assertFalse(manifest["credentials"]["credentials_read"])
        self.assertFalse(manifest["provider_interactions_performed"])
        self.assertEqual(
            manifest["pricing_and_quota_contract"]["direct_creation_cost_ceiling_usd"],
            "0.04",
        )

    def test_write_is_idempotent_and_fully_reconstructable(self) -> None:
        artifacts, destination = self.fixture.build_and_write()
        with self.fixture.preflight():
            second_status, _ = enrollment.write_readiness(self.root, artifacts, destination)
            validation = enrollment.validate_readiness(
                self.root,
                artifacts["manifest"]["run_id"],
            )
        self.assertEqual(second_status, "existing_valid")
        self.assertEqual(validation["status"], "valid")
        self.assertEqual({item.name for item in destination.iterdir()}, {"manifest.json", "README.md"})

    def test_beijing_successor_contract_is_distinct_and_reconstructable(self) -> None:
        artifacts, destination = self.fixture.build_and_write(region=enrollment.CHINA_REGION)
        manifest = artifacts["manifest"]
        provider = manifest["provider_contract"]
        self.assertEqual(provider["region"], "cn-beijing")
        self.assertEqual(provider["region_name"], "China (Beijing)")
        self.assertEqual(
            provider["workspace_specific_endpoint_template"],
            "https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization",
        )
        self.assertFalse(manifest["pricing_and_quota_contract"]["free_quota_may_reduce_actual_charge"])
        self.assertIn("beijing_workspace_id_not_bound", manifest["live_execution_blockers"])
        self.assertIn("China (Beijing)", artifacts["readme"])
        with self.fixture.preflight():
            validation = enrollment.validate_readiness(self.root, manifest["run_id"])
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(destination.exists())

    def test_manifest_tamper_is_detected(self) -> None:
        artifacts, destination = self.fixture.build_and_write()
        path = destination / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["provider_contract"]["region"] = "tampered"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with (
            self.fixture.preflight(),
            self.assertRaisesRegex(
                enrollment.base.VoiceParalinguisticError,
                "semantic SHA-256",
            ),
        ):
            enrollment.validate_readiness(self.root, artifacts["manifest"]["run_id"])

    def test_inspect_redacts_audio_and_transcript_without_reading_secret(self) -> None:
        artifacts, _ = self.fixture.build_and_write()
        run_id = artifacts["manifest"]["run_id"]
        with (
            self.fixture.preflight(),
            patch.object(
                enrollment,
                "_read_secret",
                side_effect=AssertionError("inspect must not read credentials"),
            ),
        ):
            result = enrollment.inspect_candidate(self.root, run_id, "vidya-a")
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn("<redacted source-audio-sha256=", encoded)
        self.assertIn("<redacted submitted-transcript-sha256=", encoded)
        self.assertNotIn("精确登记台词", encoded)
        self.assertFalse(result["credentials_read"])
        self.assertFalse(result["provider_interactions_performed"])

    def test_live_create_fails_confirmation_before_secret_or_network(self) -> None:
        artifacts, _ = self.fixture.build_and_write()
        run_id = artifacts["manifest"]["run_id"]
        confirmations = self._confirmations(run_id, "vidya-a")
        confirmations["confirm_undocumented_source_audio_retention"] = False
        with (
            self.fixture.preflight(),
            patch.object(enrollment, "_read_secret", side_effect=AssertionError("secret read")),
            patch.object(enrollment, "provider_request", side_effect=AssertionError("network call")),
            self.assertRaisesRegex(
                enrollment.VoiceProviderEnrollmentError,
                "undocumented source-audio retention",
            ),
        ):
            enrollment.create_one(self.root, run_id, "vidya-a", **confirmations)

    def test_beijing_create_rejects_legacy_singapore_upload_confirmation(self) -> None:
        artifacts, _ = self.fixture.build_and_write(region=enrollment.CHINA_REGION)
        run_id = artifacts["manifest"]["run_id"]
        confirmations = self._confirmations(run_id, "vidya-a")
        confirmations["workspace_id"] = "project-snow-cn"
        confirmations["confirm_region"] = enrollment.CHINA_REGION
        with (
            self.fixture.preflight(),
            patch.object(enrollment, "_read_secret", side_effect=AssertionError("secret read")),
            patch.object(enrollment, "provider_request", side_effect=AssertionError("network call")),
            self.assertRaisesRegex(
                enrollment.VoiceProviderEnrollmentError,
                r"external upload to China \(Beijing\)",
            ),
        ):
            enrollment.create_one(self.root, run_id, "vidya-a", **confirmations)

    def test_success_is_audited_and_duplicate_candidate_is_blocked(self) -> None:
        artifacts, _ = self.fixture.build_and_write()
        run_id = artifacts["manifest"]["run_id"]
        confirmations = self._confirmations(run_id, "vidya-a")
        response = {
            "output": {
                "voice": "psvda_provider_1",
                "target_model": enrollment.MODEL,
                "fallback_mode": False,
            },
            "request_id": "request-1",
        }
        with (
            self.fixture.preflight(),
            patch.object(enrollment, "_read_secret", return_value="private-test-key"),
            patch.object(enrollment, "provider_request", return_value=response) as provider,
        ):
            result = enrollment.create_one(self.root, run_id, "vidya-a", **confirmations)
            with self.assertRaisesRegex(
                enrollment.VoiceProviderEnrollmentError,
                "already has a successful",
            ):
                enrollment.create_one(self.root, run_id, "vidya-a", **confirmations)
        self.assertEqual(result["status"], "voice_created")
        self.assertEqual(provider.call_count, 1)
        audit = self.root / enrollment.AUDIT_DIRECTORY / run_id
        self.assertEqual(len(list(audit.glob("*.json"))), 2)

    def test_uncertain_attempt_blocks_retry_without_second_network_call(self) -> None:
        artifacts, _ = self.fixture.build_and_write()
        run_id = artifacts["manifest"]["run_id"]
        confirmations = self._confirmations(run_id, "chenxing-a")
        with (
            self.fixture.preflight(),
            patch.object(enrollment, "_read_secret", return_value="private-test-key"),
            patch.object(
                enrollment,
                "provider_request",
                side_effect=enrollment.VoiceProviderEnrollmentError(
                    "provider request failed; reconcile before retry"
                ),
            ) as provider,
        ):
            with self.assertRaisesRegex(
                enrollment.VoiceProviderEnrollmentError,
                "reconcile before retry",
            ):
                enrollment.create_one(self.root, run_id, "chenxing-a", **confirmations)
            with self.assertRaisesRegex(
                enrollment.VoiceProviderEnrollmentError,
                "uncertain provider attempt",
            ):
                enrollment.create_one(self.root, run_id, "chenxing-a", **confirmations)
        self.assertEqual(provider.call_count, 1)
        audit = self.root / enrollment.AUDIT_DIRECTORY / run_id
        self.assertEqual(len(list(audit.glob("*.json"))), 1)

    def test_delete_is_audited_and_duplicate_delete_is_blocked(self) -> None:
        artifacts, _ = self.fixture.build_and_write()
        run_id = artifacts["manifest"]["run_id"]
        response = {"output": {"voice": "psvda_provider_1"}, "request_id": "delete-request-1"}
        with (
            self.fixture.preflight(),
            patch.object(enrollment, "_read_secret", return_value="private-test-key"),
            patch.object(enrollment, "provider_request", return_value=response) as provider,
        ):
            result = enrollment.delete_one(
                self.root,
                run_id,
                "psvda_provider_1",
                workspace_id="project-snow-sg",
                api_key_file=None,
                confirm_voice="psvda_provider_1",
                reason="paired blind test loser cleanup",
                confirm_delete_does_not_restore_free_quota=True,
            )
            with self.assertRaisesRegex(
                enrollment.VoiceProviderEnrollmentError,
                "already has a successful provider delete receipt",
            ):
                enrollment.delete_one(
                    self.root,
                    run_id,
                    "psvda_provider_1",
                    workspace_id="project-snow-sg",
                    api_key_file=None,
                    confirm_voice="psvda_provider_1",
                    reason="paired blind test loser cleanup",
                    confirm_delete_does_not_restore_free_quota=True,
                )
        self.assertEqual(result["status"], "voice_deleted")
        self.assertFalse(result["free_quota_restored"])
        self.assertEqual(provider.call_count, 1)

    def test_create_one_without_execute_can_dry_run_without_confirmations(self) -> None:
        artifacts, _ = self.fixture.build_and_write()
        run_id = artifacts["manifest"]["run_id"]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.fixture.preflight(), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = enrollment.main(
                [
                    "--voice-root",
                    str(self.root),
                    "create-one",
                    "--run-id",
                    run_id,
                    "--candidate-key",
                    "vidya-b",
                ]
            )
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["credentials_read"])
        self.assertEqual(stderr.getvalue(), "")

    def test_endpoint_and_payload_are_pinned_and_injection_safe(self) -> None:
        self.assertEqual(
            enrollment.enrollment_endpoint("Project-Snow-SG"),
            "https://project-snow-sg.ap-southeast-1.maas.aliyuncs.com"
            "/api/v1/services/audio/tts/customization",
        )
        with self.assertRaises(enrollment.VoiceProviderEnrollmentError):
            enrollment.enrollment_endpoint("evil.example.com")
        self.assertEqual(
            enrollment.enrollment_endpoint("Project-Snow-CN", region=enrollment.CHINA_REGION),
            "https://project-snow-cn.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization",
        )
        payload = enrollment.build_create_payload(
            preferred_name="psvda12345678",
            audio=b"wav",
            transcript="精确台词",
        )
        self.assertEqual(payload["model"], enrollment.ENROLLMENT_MODEL)
        self.assertEqual(payload["input"]["target_model"], enrollment.MODEL)
        self.assertEqual(payload["input"]["language"], "zh")

    def test_beijing_dotenv_alias_is_discovered_without_exposing_or_relabeling_it(self) -> None:
        project = Path(self.temporary.name) / "DotenvProject"
        voice_root = project / "Data" / "Voice"
        dotenv = project / "App" / ".env"
        voice_root.mkdir(parents=True)
        dotenv.parent.mkdir(parents=True)
        dotenv.write_text(
            "DASHSCOPE_API_KEY=\n"
            "DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "EVIDENCE_REVIEW_API_KEY=\n"
            "EVIDENCE_REVIEW_API_KEY=private-beijing-test-key\n"
            "EVIDENCE_REVIEW_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "EVIDENCE_REVIEW_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "EVIDENCE_REVIEW_PROVIDER=first-unrelated-value\n"
            "EVIDENCE_REVIEW_PROVIDER=second-unrelated-value\n",
            encoding="utf-8",
        )
        self.assertEqual(
            enrollment._read_secret(
                None,
                voice_root=voice_root,
                region=enrollment.CHINA_REGION,
            ),
            "private-beijing-test-key",
        )
        with self.assertRaisesRegex(
            enrollment.VoiceProviderEnrollmentError,
            "Singapore",
        ):
            enrollment._read_secret(None, voice_root=voice_root, region=enrollment.REGION)

        dotenv.write_text(
            "DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "EVIDENCE_REVIEW_API_KEY=first-test-key\n"
            "EVIDENCE_REVIEW_API_KEY=second-test-key\n"
            "EVIDENCE_REVIEW_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            enrollment.VoiceProviderEnrollmentError,
            "duplicate key 'EVIDENCE_REVIEW_API_KEY'",
        ):
            enrollment._read_secret(None, voice_root=voice_root, region=enrollment.CHINA_REGION)

    def test_prepare_execute_requires_offline_confirmation(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = enrollment.main(
                [
                    "--voice-root",
                    str(self.root),
                    "prepare",
                    "--preflight-id",
                    self.fixture.PREFLIGHT_ID,
                    "--expect-preflight-manifest-byte-sha256",
                    self.fixture.PREFLIGHT_BYTE_SHA256,
                    "--prepared-at",
                    self.fixture.PREPARED_AT,
                    "--execute",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("--execute requires --confirm-offline-only", stderr.getvalue())
        self.assertFalse((self.root / enrollment.OUTPUT_DIRECTORY).exists())
