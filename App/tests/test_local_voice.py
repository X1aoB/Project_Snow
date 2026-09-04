from __future__ import annotations

import json
import tempfile
import wave
from io import BytesIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.snow_app import local_voice


def _profile_document() -> dict:
    workspace_id = "ws-fixture-runtime"
    character_definitions = (
        (
            "5157b8972632",
            "vidya",
            "薇蒂雅",
            (
                ("vidya-neutral-short", "neutral", "voice_vidya_a", "locked"),
                ("vidya-breathy-lexical", "breathy", None, "paused"),
                ("vidya-heightened", "heightened", "voice_vidya_b", "locked"),
            ),
        ),
        (
            "98322bd505f4",
            "chenxing",
            "辰星",
            (
                ("chenxing-neutral-short", "neutral", "voice_chenxing_a", "locked"),
                ("chenxing-breathy-lexical", "breathy", "voice_chenxing_b", "locked"),
                ("chenxing-heightened", "heightened", "voice_chenxing_a", "locked"),
            ),
        ),
    )
    characters = []
    for character_id, slug, name, definitions in character_definitions:
        routes = []
        for case_id, style, voice_id, status in definitions:
            route = {
                "case_id": case_id,
                "style": style,
                "source_style_anchor": style,
                "status": status,
            }
            if voice_id is not None:
                route["provider_voice_id"] = voice_id
                route["provider_voice_id_sha256"] = local_voice._sha256_text(voice_id)
            else:
                route["unavailable_reason"] = "terminal_preference_slot_not_qualified"
            routes.append(route)
        characters.append(
            {
                "character_slug": slug,
                "runtime_character_id": character_id,
                "runtime_character_name": name,
                "routes": routes,
            }
        )
    document = {
        "schema_version": local_voice.PROFILE_SCHEMA,
        "policy_version": "project-snow-style-specific-qwen-vc-runtime-routing-1",
        "profile_id": "voice-runtime-profile-" + "1" * 20,
        "prepared_at": "2026-09-04T16:00:00+08:00",
        "status": local_voice.PROFILE_STATUS,
        "source": {},
        "provider_contract": {
            "provider_family": "Alibaba Cloud Model Studio",
            "region": local_voice.REGION,
            "target_model": local_voice.MODEL,
            "websocket_endpoint": local_voice.WEBSOCKET_ENDPOINT,
            "mode": local_voice.MODE,
            "language_type": local_voice.LANGUAGE_TYPE,
            "response_format": local_voice.RESPONSE_FORMAT,
            "sample_rate_hz": local_voice.SAMPLE_RATE_HZ,
            "channels": local_voice.CHANNELS,
            "sample_width_bytes": local_voice.SAMPLE_WIDTH_BYTES,
            "workspace_id": workspace_id,
            "workspace_id_sha256": local_voice._sha256_text(workspace_id),
            "instruction_control_supported_by_target_model": False,
            "instructions_sent": False,
        },
        "routing_contract": {
            "style_selector": "explicit_internal_style_or_deterministic_lexical_classifier",
            "supported_styles": list(local_voice.EXPECTED_STYLES),
            "paused_slot_fallback_allowed": False,
            "cross_character_fallback_allowed": False,
            "cross_style_fallback_allowed": False,
            "provider_voice_id_exposed_to_client": False,
            "user_supplied_voice_id_allowed": False,
            "user_supplied_model_allowed": False,
            "user_supplied_websocket_endpoint_allowed": False,
            "paralinguistic_ordinals_2_and_3_included": False,
        },
        "authorization_contract": {},
        "characters": characters,
        "summary": {
            "character_count": 2,
            "style_slot_count": 6,
            "locked_slot_count": 5,
            "paused_slot_count": 1,
            "paused_case_ids": ["vidya-breathy-lexical"],
        },
        "stable_identity_sha256": "2" * 64,
    }
    document["manifest_sha256"] = local_voice._canonical_sha256(document)
    return document


def _write_profile(root: Path, document: dict | None = None) -> Path:
    value = document or _profile_document()
    destination = root / "Voice" / local_voice.PROFILE_DIRECTORY / str(value["profile_id"]) / "manifest.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


class LocalVoiceTests(TestCase):
    def test_style_classifier_is_conservative_and_lexical(self) -> None:
        self.assertEqual(local_voice.classify_style("今天按计划继续。"), "neutral")
        self.assertEqual(local_voice.classify_style("靠近一点，我只想轻声告诉你。"), "breathy")
        self.assertEqual(local_voice.classify_style("看着我！现在不许闭眼！"), "heightened")
        self.assertEqual(local_voice.classify_style("啊……"), "neutral")

    def test_voice_offer_policy_is_idempotent_and_channel_aware(self) -> None:
        arguments = {
            "enabled_by_user": True,
            "communication_channel": "text",
            "character_id": "98322bd505f4",
            "message_id": "message-fixture",
            "text": "今天按计划继续。",
            "text_probability": 0.25,
            "emotion_probability": 0.45,
        }
        first = local_voice.decide_voice_reply(**arguments)
        second = local_voice.decide_voice_reply(**arguments)
        self.assertEqual(first, second)
        self.assertFalse(first.auto_play)
        self.assertEqual(first.reason, "text_daily_probability")

        in_person = local_voice.decide_voice_reply(**{**arguments, "communication_channel": "in_person"})
        self.assertTrue(in_person.should_synthesize)
        self.assertTrue(in_person.auto_play)
        self.assertEqual(in_person.probability, 1.0)

        disabled = local_voice.decide_voice_reply(**{**arguments, "enabled_by_user": False})
        self.assertFalse(disabled.should_synthesize)

    def test_text_offer_uses_higher_probability_for_strong_emotion(self) -> None:
        common = {
            "enabled_by_user": True,
            "communication_channel": "text",
            "character_id": "98322bd505f4",
            "message_id": "message-fixture",
            "text_probability": 0.25,
            "emotion_probability": 0.45,
        }
        neutral = local_voice.decide_voice_reply(**common, text="今天按计划继续。")
        heightened = local_voice.decide_voice_reply(**common, text="看着我！现在不许闭眼！")
        self.assertEqual(neutral.probability, 0.25)
        self.assertEqual(heightened.probability, 0.45)
        self.assertEqual(heightened.reason, "text_strong_emotion_probability")

    def test_paused_style_never_calls_provider_or_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_profile(root)
            provider = Mock()
            runtime = local_voice.LocalVoiceRuntime(path, api_key="fixture-key", provider=provider)

            with self.assertRaises(local_voice.LocalVoiceSlotPaused) as captured:
                runtime.synthesize("5157b8972632", "靠近一点，轻声告诉你。")

            self.assertEqual(captured.exception.style, "breathy")
            provider.assert_not_called()

    def test_locked_route_returns_wav_without_private_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_profile(root)
            provider = Mock(
                return_value=(
                    (100).to_bytes(2, "little", signed=True) * local_voice.SAMPLE_RATE_HZ,
                    {"provider_usage_characters": 8},
                )
            )
            runtime = local_voice.LocalVoiceRuntime(path, api_key="fixture-key", provider=provider)

            result = runtime.synthesize("98322bd505f4", "设备运行正常。")

            with wave.open(BytesIO(result.pop("audio_bytes")), "rb") as audio:
                self.assertEqual(audio.getframerate(), local_voice.SAMPLE_RATE_HZ)
                self.assertEqual(audio.getnchannels(), 1)
            safe_result = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("voice_chenxing_a", safe_result)
            self.assertNotIn("ws-fixture-runtime", safe_result)
            self.assertEqual(result["style"], "neutral")
            self.assertEqual(result["provider_usage_characters"], 8)
            provider.assert_called_once()

    def test_discovery_ignores_legacy_profile_without_workspace_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = _write_profile(root)
            legacy = _profile_document()
            legacy["profile_id"] = "voice-runtime-profile-" + "3" * 20
            legacy["provider_contract"].pop("workspace_id")
            legacy["provider_contract"].pop("workspace_id_sha256")
            legacy["manifest_sha256"] = local_voice._canonical_sha256(
                {key: value for key, value in legacy.items() if key != "manifest_sha256"}
            )
            _write_profile(root, legacy)

            self.assertEqual(local_voice.discover_profile_path(root), expected.resolve())

    def test_tampered_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = _profile_document()
            document["routing_contract"]["paused_slot_fallback_allowed"] = True
            path = _write_profile(root, document)

            with self.assertRaises(local_voice.LocalVoiceError):
                local_voice.LocalVoiceRuntime(path, api_key="fixture-key")

    def test_application_adapter_saves_audio_without_exposing_private_voice(self) -> None:
        from backend.snow_app import main

        runtime = Mock()
        runtime.supports.return_value = True
        runtime.synthesize.return_value = {
            "status": "completed",
            "audio_bytes": b"RIFF-fixture",
            "content_type": "audio/wav",
            "filename": "chenxing-neutral-reply.wav",
            "style": "neutral",
            "case_id": "chenxing-neutral-short",
            "profile_id": "voice-runtime-profile-" + "1" * 20,
            "model": {
                "provider": "qwen-vc-realtime",
                "model": local_voice.MODEL,
                "region": local_voice.REGION,
            },
            "provider_usage_characters": 8,
        }
        manager = Mock()
        manager.save_bytes.return_value = {"attachment_id": "attachment-fixture"}
        with (
            patch.object(main, "local_voice_runtime", runtime),
            patch.object(main, "attachment_manager", manager),
            patch.object(main.provider_registry, "route") as generic_route,
        ):
            result = main._synthesize_voice("98322bd505f4", "设备运行正常。")

        generic_route.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["content_url"],
            "/api/v1/attachments/attachment-fixture/content",
        )
        self.assertNotIn("voice", result)
        self.assertNotIn("workspace", json.dumps(result, ensure_ascii=False).casefold())

    def test_application_adapter_reports_pause_without_generic_fallback(self) -> None:
        from backend.snow_app import main

        runtime = Mock()
        runtime.supports.return_value = True
        runtime.synthesize.side_effect = local_voice.LocalVoiceSlotPaused(
            style="breathy", case_id="vidya-breathy-lexical"
        )
        with (
            patch.object(main, "local_voice_runtime", runtime),
            patch.object(main.provider_registry, "route") as generic_route,
        ):
            result = main._synthesize_voice("5157b8972632", "靠近一点，轻声说。")

        generic_route.assert_not_called()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "style_slot_paused")
        self.assertFalse(result["fallback_used"])
