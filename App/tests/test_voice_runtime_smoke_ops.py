from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from backend.snow_app import local_voice
from scripts import voice_paralinguistic_ops as base
from scripts import voice_runtime_smoke_ops as smoke


class _FakeRuntime:
    def __init__(self, provider=None) -> None:
        definitions = (
            ("5157b8972632", "vidya", "薇蒂雅", "vidya-neutral-short", "neutral"),
            ("5157b8972632", "vidya", "薇蒂雅", "vidya-heightened", "heightened"),
            ("98322bd505f4", "chenxing", "辰星", "chenxing-neutral-short", "neutral"),
            (
                "98322bd505f4",
                "chenxing",
                "辰星",
                "chenxing-breathy-lexical",
                "breathy",
            ),
            ("98322bd505f4", "chenxing", "辰星", "chenxing-heightened", "heightened"),
        )
        self.profile_id = smoke.DEFAULT_PROFILE_ID
        self.routes = {
            (character_id, style): local_voice.VoiceRoute(
                character_id=character_id,
                character_slug=slug,
                character_name=name,
                case_id=case_id,
                style=style,
                status="locked",
                provider_voice_id=f"fixture_{slug}",
            )
            for character_id, slug, name, case_id, style in definitions
        }
        self.provider = provider

    def route(self, character_id, _text, *, style=None):
        return self.routes[(character_id, style)]

    def synthesize(self, character_id, text, *, style=None):
        route = self.route(character_id, text, style=style)
        pcm, metadata = self.provider(
            api_key="fixture",
            workspace_id="ws-fixture",
            voice_id=route.provider_voice_id,
            text=text,
        )
        return {
            "case_id": route.case_id,
            "style": route.style,
            "audio_bytes": local_voice.pcm_to_wav(pcm),
            "provider_usage_characters": metadata.get("provider_usage_characters"),
        }


def _profile_source(root: Path):
    profile = {
        "profile_id": smoke.DEFAULT_PROFILE_ID,
        "manifest_sha256": "a" * 64,
    }
    payload = b'{"fixture":"profile"}\n'
    path = root / smoke.profile_ops.OUTPUT_DIRECTORY / smoke.DEFAULT_PROFILE_ID / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    return path, profile, payload, _FakeRuntime()


class VoiceRuntimeSmokeTests(TestCase):
    def _prepared(self, root: Path):
        source = _profile_source(root)
        with patch.object(smoke, "_profile", return_value=source):
            document, destination = smoke.build_run(
                root,
                profile_id=smoke.DEFAULT_PROFILE_ID,
                prepared_at="2026-09-04T17:00:00+08:00",
            )
            result = smoke.write_run(root, document, destination)
        return document, destination, result, source

    def test_plan_has_exactly_five_locked_slots_under_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, _destination, result, _source = self._prepared(root)

            self.assertEqual(result["planned_output_count"], 5)
            self.assertEqual(result["planned_billable_characters"], 179)
            self.assertLessEqual(smoke.Decimal(result["estimated_cost_usd"]), smoke.COST_CEILING_USD)
            self.assertNotIn(
                "vidya-breathy-lexical",
                {item["case_id"] for item in document["planned_outputs"]},
            )
            encoded = json.dumps(document, ensure_ascii=False)
            self.assertNotIn('"provider_voice_id":', encoded)
            self.assertNotIn('"workspace_id":', encoded)
            self.assertFalse(result["network_calls_performed"])

    def test_fake_live_run_writes_five_audited_wavs_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, destination, _result, source = self._prepared(root)
            profile_validation = {"manifest_sha256": source[1]["manifest_sha256"]}
            counter = iter(range(1, 10))

            def provider(**_kwargs):
                value = next(counter)
                pcm = value.to_bytes(2, "little", signed=True) * local_voice.SAMPLE_RATE_HZ
                return pcm, {"provider_usage_characters": 10}

            def runtime_factory(_path, *, api_key, provider):
                self.assertEqual(api_key, "fixture-secret")
                return _FakeRuntime(provider)

            byte_hash = base._sha256_bytes(smoke._pretty_json_bytes(document))
            with (
                patch.object(smoke, "_profile", return_value=source),
                patch.object(
                    smoke.profile_ops,
                    "validate_profile",
                    return_value=profile_validation,
                ),
                patch.object(smoke.enrollment, "_read_secret", return_value="fixture-secret"),
                patch.object(smoke.local_voice, "LocalVoiceRuntime", side_effect=runtime_factory),
            ):
                rendered = smoke.render_all(
                    root,
                    document["run_id"],
                    expected_manifest_byte_sha256=byte_hash,
                    confirm_run_id=document["run_id"],
                    confirm_model=local_voice.MODEL,
                    confirm_region=local_voice.REGION,
                    confirm_cost_ceiling_usd="0.005",
                    confirm_five_locked_slot_smoke_authorized=True,
                    confirm_paused_slot_excluded=True,
                    provider=provider,
                )
                review = smoke.finalize_review(
                    root,
                    document["run_id"],
                    expected_manifest_byte_sha256=byte_hash,
                )

            self.assertEqual(rendered["successful_output_count"], 5)
            self.assertEqual(rendered["pending_attempt_count"], 0)
            self.assertEqual(len(list((destination / "audio").rglob("*.wav"))), 5)
            self.assertEqual(len(list((destination / "audits").glob("*-attempt.json"))), 5)
            self.assertEqual(len(list((destination / "audits").glob("*-result.json"))), 5)
            self.assertEqual(review["sample_count"], 5)
            page = Path(review["review_html_path"]).read_text(encoding="utf-8")
            self.assertIn("5/5 已生成", page)
            self.assertNotIn("fixture_vidya", page)
            self.assertFalse(review["new_preference_round_created"])

    def test_uncertain_attempt_blocks_automatic_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document, _destination, _result, source = self._prepared(root)
            profile_validation = {"manifest_sha256": source[1]["manifest_sha256"]}

            def failing_provider(**_kwargs):
                raise local_voice.LocalVoiceError("fixture provider failure")

            def runtime_factory(_path, *, api_key, provider):
                return _FakeRuntime(provider)

            byte_hash = base._sha256_bytes(smoke._pretty_json_bytes(document))
            patches = (
                patch.object(smoke, "_profile", return_value=source),
                patch.object(
                    smoke.profile_ops,
                    "validate_profile",
                    return_value=profile_validation,
                ),
                patch.object(smoke.enrollment, "_read_secret", return_value="fixture-secret"),
                patch.object(smoke.local_voice, "LocalVoiceRuntime", side_effect=runtime_factory),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                with self.assertRaises(local_voice.LocalVoiceError):
                    smoke.render_all(
                        root,
                        document["run_id"],
                        expected_manifest_byte_sha256=byte_hash,
                        confirm_run_id=document["run_id"],
                        confirm_model=local_voice.MODEL,
                        confirm_region=local_voice.REGION,
                        confirm_cost_ceiling_usd="0.005",
                        confirm_five_locked_slot_smoke_authorized=True,
                        confirm_paused_slot_excluded=True,
                        provider=failing_provider,
                    )
                with self.assertRaisesRegex(smoke.VoiceRuntimeSmokeError, "uncertain live attempt"):
                    smoke.render_all(
                        root,
                        document["run_id"],
                        expected_manifest_byte_sha256=byte_hash,
                        confirm_run_id=document["run_id"],
                        confirm_model=local_voice.MODEL,
                        confirm_region=local_voice.REGION,
                        confirm_cost_ceiling_usd="0.005",
                        confirm_five_locked_slot_smoke_authorized=True,
                        confirm_paused_slot_excluded=True,
                        provider=failing_provider,
                    )
