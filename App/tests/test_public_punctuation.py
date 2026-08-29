from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.snow_app.public_service import (
    _PUBLIC_JSON_CANDIDATE_LIMIT,
    PublicChatService,
    _normalize_public_immersive_punctuation,
)


class PublicImmersivePunctuationTests(unittest.TestCase):
    def test_normalizes_ascii_marks_in_chinese_conversation(self) -> None:
        self.assertEqual(
            _normalize_public_immersive_punctuation("还没睡呢,郎君...吗?"),
            "还没睡呢，郎君...吗？",
        )
        self.assertEqual(
            _normalize_public_immersive_punctuation(
                "还没睡呢,郎君...吗?我想说:今晚有空;一起走吧!"
            ),
            "还没睡呢，郎君...吗？我想说：今晚有空；一起走吧！",
        )
        self.assertEqual(
            _normalize_public_immersive_punctuation("“你好”,她说:“晚安!”"),
            "“你好”，她说：“晚安！”",
        )
        self.assertEqual(
            _normalize_public_immersive_punctuation("你好吗?🙂 “你好吗?”,她问"),
            "你好吗？🙂 “你好吗？”，她问",
        )

    def test_preserves_urls_numbers_code_json_identifiers_and_emoticons(self) -> None:
        source = "\n".join(
            (
                "链接：https://example.com/search?q=你好,世界&lang=zh",
                "裸链接：example.com/你好,世界?q=晚安",
                "相对链接：/chat/你好,世界?mode=raw",
                "数字 3.14, 1,234; 时间 12:30",
                "版本 v1.2.3-beta, build 2026.08.28",
                "ASCII ids: foo_bar:baz, user?name!",
                "`print(\"你好,世界?\")`",
                "print(\"你好,世界?\")",
                'JSON: {"text":"你好,世界?","ok":true}',
                "```python\nprint(\"你好,世界?\")\n```",
                "开心:) 调皮;) 惊讶:D ^_^",
            )
        )
        self.assertEqual(_normalize_public_immersive_punctuation(source), source)

    def test_public_content_blocks_and_answer_share_normalized_text(self) -> None:
        for channel, block_type in (("text", "message"), ("in_person", "speech")):
            with self.subTest(channel=channel):
                adjustments: list[str] = []
                blocks, answer, truncated, safety_category = (
                    PublicChatService._public_generation_content(
                        object(),
                        {
                            "answer": "还没睡呢,郎君...吗?",
                            "content_blocks": [
                                {
                                    "type": block_type,
                                    "text": "还没睡呢,郎君...吗?",
                                }
                            ],
                        },
                        channel,
                        response_adjustments=adjustments,
                    )
                )

                self.assertEqual(
                    [(block["type"], block["text"]) for block in blocks],
                    [(block_type, "还没睡呢，郎君...吗？")],
                )
                self.assertEqual(answer, "还没睡呢，郎君...吗？")
                self.assertFalse(truncated)
                self.assertIsNone(safety_category)
                self.assertEqual(adjustments, ["public_punctuation_normalized"])

    def test_deeply_nested_json_like_output_is_bounded_and_does_not_crash(self) -> None:
        reproducer = "[" * 1000 + "]" * 1000 + "中,文"
        self.assertEqual(_normalize_public_immersive_punctuation(reproducer), reproducer)

        deeply_nested = "[" * 1100 + "还没睡呢,郎君吗?" + "]" * 1100

        blocks, answer, truncated, safety_category = (
            PublicChatService._public_generation_content(
                object(),
                {
                    "answer": deeply_nested,
                    "content_blocks": [{"type": "message", "text": deeply_nested}],
                },
                "text",
            )
        )

        self.assertTrue(truncated)
        self.assertLessEqual(len(answer), 1200)
        self.assertEqual(blocks[0]["text"], answer)
        self.assertIn("还没睡呢，郎君吗？", answer)
        self.assertIsNone(safety_category)

    def test_overlong_malformed_json_has_a_fixed_decode_attempt_budget(self) -> None:
        overlong = "中,文 " + "{} [" * 20_000
        with patch(
            "backend.snow_app.public_service.json.JSONDecoder.raw_decode",
            side_effect=ValueError("malformed"),
        ) as raw_decode:
            normalized = _normalize_public_immersive_punctuation(overlong)

        self.assertEqual(len(normalized), len(overlong))
        self.assertTrue(normalized.startswith("中，文 "))
        self.assertLessEqual(raw_decode.call_count, _PUBLIC_JSON_CANDIDATE_LIMIT)


if __name__ == "__main__":
    unittest.main()
