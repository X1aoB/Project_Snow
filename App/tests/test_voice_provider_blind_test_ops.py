from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from scripts import voice_provider_blind_test_ops as blind
from scripts import voice_provider_preflight_ops as preflight


def _plan() -> dict:
    return {
        "operator_only_candidate_map": [
            {"candidate_key": "vidya-a", "character_slug": "vidya"},
            {"candidate_key": "vidya-b", "character_slug": "vidya"},
            {"candidate_key": "chenxing-a", "character_slug": "chenxing"},
            {"candidate_key": "chenxing-b", "character_slug": "chenxing"},
        ],
        "lexical_test_prompts": preflight._test_prompts(),
    }


class FakeConnection:
    def __init__(self, events: list[dict]) -> None:
        self.events = [json.dumps(item, ensure_ascii=False) for item in events]
        self.sent: list[dict] = []

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def recv(self, timeout: float | None = None) -> str:
        if timeout is None or timeout <= 0:
            raise AssertionError("the client must use a positive receive timeout")
        if not self.events:
            raise AssertionError("fake provider ran out of events")
        return self.events.pop(0)

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))


class FakeFactory:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.url = ""
        self.options: dict = {}

    def __call__(self, url: str, **options) -> FakeConnection:
        self.url = url
        self.options = options
        return self.connection


class VoiceProviderBlindTestTests(TestCase):
    def test_randomized_outputs_are_complete_opaque_and_bounded(self) -> None:
        mappings, outputs = blind._build_randomized_outputs(_plan())

        self.assertEqual(len(mappings), 2)
        self.assertEqual(len(outputs), 24)
        self.assertEqual(sum(item["input_character_count"] for item in outputs), 594)
        self.assertEqual(sum(item["billing_character_count"] for item in outputs), 1052)
        self.assertEqual(len({item["output_id"] for item in outputs}), 24)
        labels = {
            label["opaque_label_id"]
            for mapping in mappings
            for label in mapping["labels"]
        }
        self.assertEqual(len(labels), 4)
        self.assertTrue(all(blind.OPAQUE_ID_PATTERN.fullmatch(item) for item in labels))
        for case_id in {item["case_id"] for item in outputs}:
            pair = [item for item in outputs if item["case_id"] == case_id]
            self.assertEqual({item["display_order"] for item in pair}, {1, 2})
            self.assertEqual(len({item["text"] for item in pair}), 1)

    def test_official_cjk_billing_rule_matches_provider_usage_example(self) -> None:
        text = "今天的状态很稳定，我们可以按计划继续。"

        self.assertEqual(len(text), 19)
        self.assertEqual(blind._billing_character_count(text), 36)

    def test_pcm_is_wrapped_as_valid_pinned_wav(self) -> None:
        pcm = (1200).to_bytes(2, "little", signed=True) * blind.SAMPLE_RATE_HZ

        wav, metrics = blind._pcm_to_wav(pcm)
        verified = blind._validate_wav_bytes(wav, metrics)

        self.assertEqual(verified["duration_seconds"], 1.0)
        self.assertEqual(verified["sample_rate_hz"], 24_000)
        self.assertEqual(verified["channels"], 1)
        self.assertEqual(verified["full_scale_sample_count"], 0)

    def test_realtime_protocol_commits_one_text_and_waits_for_finish(self) -> None:
        voice = "qwen3_tts_vc_voice_123"
        workspace = "ws-test123"
        pcm = (900).to_bytes(2, "little", signed=True) * blind.SAMPLE_RATE_HZ
        events = [
            {
                "type": "session.created",
                "session": {"id": "sess-1", "model": blind.MODEL},
            },
            {
                "type": "session.updated",
                "session": {
                    "id": "sess-1",
                    "model": blind.MODEL,
                    "voice": voice,
                    "mode": "commit",
                    "language_type": "chinese",
                    "response_format": "pcm",
                    "sample_rate": 24_000,
                },
            },
            {"type": "input_text_buffer.committed", "item_id": "item-1"},
            {"type": "response.created", "response": {"id": "resp-1"}},
            {
                "type": "response.audio.delta",
                "response_id": "resp-1",
                "delta": base64.b64encode(pcm).decode("ascii"),
            },
            {"type": "response.audio.done", "response_id": "resp-1"},
            {
                "type": "response.done",
                "response_id": "resp-1",
                "response": {
                    "id": "resp-1",
                    "status": "completed",
                    "voice": voice,
                    "usage": {"characters": 8},
                },
            },
            {"type": "session.finished"},
        ]
        connection = FakeConnection(events)
        factory = FakeFactory(connection)

        actual_pcm, metadata = blind.provider_synthesize_pcm(
            api_key="secret-api-key",
            workspace_id=workspace,
            voice_id=voice,
            text="测试一条语音。",
            websocket_factory=factory,
        )

        self.assertEqual(actual_pcm, pcm)
        self.assertEqual(metadata["session_id"], "sess-1")
        self.assertEqual(metadata["response_id"], "resp-1")
        self.assertEqual(metadata["provider_usage_characters"], 8)
        self.assertEqual(
            [item["type"] for item in connection.sent],
            [
                "session.update",
                "input_text_buffer.append",
                "input_text_buffer.commit",
                "session.finish",
            ],
        )
        self.assertEqual(connection.sent[1]["text"], "测试一条语音。")
        self.assertEqual(connection.sent[0]["session"]["voice"], voice)
        self.assertEqual(factory.options["additional_headers"]["X-DashScope-WorkSpace"], workspace)
        self.assertNotIn("secret-api-key", json.dumps(connection.sent))

    def test_provider_error_does_not_echo_sensitive_message(self) -> None:
        secret_voice = "private_voice_identifier"
        connection = FakeConnection(
            [
                {
                    "type": "session.created",
                    "session": {"id": "sess-1", "model": blind.MODEL},
                },
                {
                    "type": "error",
                    "error": {
                        "code": "invalid_value",
                        "message": f"voice {secret_voice} is invalid",
                    },
                },
            ]
        )

        with self.assertRaises(blind.VoiceProviderBlindTestError) as caught:
            blind.provider_synthesize_pcm(
                api_key="secret-api-key",
                workspace_id="ws-test123",
                voice_id=secret_voice,
                text="测试。",
                websocket_factory=FakeFactory(connection),
            )

        self.assertIn("invalid_value", str(caught.exception))
        self.assertNotIn(secret_voice, str(caught.exception))

    def test_pending_attempt_blocks_until_a_result_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            audits = root / "audits"
            audits.mkdir()
            run_id = "voice-provider-blind-test-run-" + "a" * 20
            attempt_id = "synthesis-attempt-" + "b" * 32
            output_id = "blind-output-" + "c" * 16
            blind._write_audit(
                audits,
                {
                    "schema_version": blind.AUDIT_SCHEMA,
                    "attempt_id": attempt_id,
                    "stage": "attempt_started",
                    "run_id": run_id,
                    "output_id": output_id,
                },
                f"{attempt_id}-attempt.json",
            )
            state = blind._audit_state(root, audits, run_id)
            self.assertEqual(len(state["pending"]), 1)

            blind._write_audit(
                audits,
                {
                    "schema_version": blind.AUDIT_SCHEMA,
                    "attempt_id": attempt_id,
                    "stage": "result_committed",
                    "run_id": run_id,
                    "output_id": output_id,
                    "outcome": "audio_rendered",
                },
                f"{attempt_id}-result.json",
            )
            state = blind._audit_state(root, audits, run_id)
            self.assertEqual(state["pending"], [])
            self.assertIn(output_id, state["by_output"])

    def test_public_privacy_guard_rejects_candidate_or_voice_ids(self) -> None:
        private = {
            "operator_only_candidate_mapping": [
                {
                    "labels": [
                        {"opaque_label_id": "sample-abcd"},
                        {"opaque_label_id": "sample-efgh"},
                    ]
                }
            ]
        }
        clean = b'{"samples":["sample-abcd","sample-efgh"]}'
        blind._assert_public_privacy([clean], private, {"voice-private-123"})

        with self.assertRaises(blind.VoiceProviderBlindTestError):
            blind._assert_public_privacy(
                [clean + b' "candidate":"vidya-a"'],
                private,
                {"voice-private-123"},
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
