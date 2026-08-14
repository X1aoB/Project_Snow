from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import tempfile
import unittest

from backend.snow_app.chat_store import ConversationStore
from backend.snow_app.mvp_policy import (
    MVP_CHARACTERS,
    MVP_CHARACTER_BY_ID,
    MVP_CHARACTER_BY_NAME,
    question_bank,
)
from backend.snow_app.config import Settings
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.repository import RuntimeRepository
from pipelines.build_mvp_views import _document_mentions_character, _shared_document_for_character


APP_ROOT = Path(__file__).resolve().parents[1]


class Mvp22ProductTests(unittest.TestCase):
    def test_registry_has_exactly_22_selectable_characters_and_merged_aliases(self) -> None:
        self.assertEqual(len(MVP_CHARACTERS), 22)
        self.assertEqual(len({item.character_id for item in MVP_CHARACTERS}), 22)
        self.assertTrue(all(item.selector_enabled for item in MVP_CHARACTERS))
        self.assertNotIn("米拉·吉诺拉", MVP_CHARACTER_BY_NAME)
        self.assertEqual(MVP_CHARACTER_BY_NAME["芬妮·戈尔登"].display_name, "芬妮")
        self.assertEqual(MVP_CHARACTER_BY_NAME["苔丝·科特金"].display_name, "苔丝")
        self.assertEqual(MVP_CHARACTER_BY_NAME["茉莉安·安德烈奥蒂"].display_name, "茉莉安")
        self.assertEqual(MVP_CHARACTER_BY_NAME["姬辰星"].display_name, "辰星")
        self.assertEqual(MVP_CHARACTER_BY_NAME["鸣濑晴"].display_name, "晴")

    def test_question_bank_has_eight_questions_per_character(self) -> None:
        questions = question_bank()
        counts = Counter(item["character_id"] for item in questions)
        self.assertEqual(len(questions), 176)
        self.assertEqual(set(counts), {item.character_id for item in MVP_CHARACTERS})
        self.assertTrue(all(value == 8 for value in counts.values()))

    def test_generated_views_cover_all_registry_entries(self) -> None:
        path = APP_ROOT / "runtime" / "mvp" / "character_views.jsonl"
        self.assertTrue(path.exists(), "Run python -m pipelines.build_mvp_views first")
        views = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(views), 22)
        self.assertEqual({item["character_id"] for item in views}, {item.character_id for item in MVP_CHARACTERS})
        for view in views:
            coverage = view["coverage"]
            self.assertIn(coverage["level"], {"limited", "standard", "full"})
            self.assertIn("direct_document_count", coverage)
            self.assertIn("linked_document_count", coverage)
            self.assertIn("global_context_document_count", coverage)
            self.assertEqual(view["selector_enabled"], True)

    def test_product_bootstrap_returns_all_22_characters(self) -> None:
        settings = Settings.from_environment()
        service = MVPService(settings, RuntimeRepository(settings))
        result = service.bootstrap()

        self.assertEqual(len(result["characters"]), 22)
        self.assertEqual(
            {item["character_id"] for item in result["characters"]},
            {item.character_id for item in MVP_CHARACTERS},
        )
        self.assertEqual(len(result["feedback_categories"]), 5)

    def test_product_bootstrap_returns_independent_mode_summaries(self) -> None:
        settings = Settings.from_environment()
        service = MVPService(settings, RuntimeRepository(settings))
        with tempfile.TemporaryDirectory() as temporary_directory:
            service.conversation_store = ConversationStore(
                Path(temporary_directory) / "conversations.sqlite3"
            )
            for mode, message_id, answer in (
                ("immersive", "immersive_message", "沉浸式回复"),
                ("assistant", "assistant_message", "助手回复"),
            ):
                service.conversation_store.save_exchange(
                    character_id="ca0144ccd81b",
                    session_id="bootstrap_session",
                    world_session_id="bootstrap_world",
                    client_message_id=f"client_{mode}",
                    user_text=f"{mode} question",
                    response={
                        "message_id": message_id,
                        "character_id": "ca0144ccd81b",
                        "character_name": "里芙",
                        "session_id": "bootstrap_session",
                        "world_session_id": "bootstrap_world",
                        "mode": mode,
                        "communication_channel": "text",
                        "answer": answer,
                        "content_blocks": [{"type": "message", "text": answer}],
                    },
                    session_state={
                        "character_id": "ca0144ccd81b",
                        "communication_channel": "text",
                        "mode": mode,
                        "turns": [],
                        "mode_turns": {"immersive": [], "assistant": []},
                    },
                    world_state={"world_session_id": "bootstrap_world"},
                )
            result = service.bootstrap()

        liv = next(
            item for item in result["characters"] if item["character_id"] == "ca0144ccd81b"
        )
        self.assertEqual(liv["conversations"]["immersive"]["last_message"], "沉浸式回复")
        self.assertEqual(liv["conversations"]["assistant"]["last_message"], "助手回复")
        self.assertEqual(liv["generated_portrait"], None)
        self.assertEqual(result["client_version"], "v0.5.0")

    def test_shared_inheritance_requires_scope_or_explicit_mention(self) -> None:
        character = MVP_CHARACTER_BY_ID["ca0144ccd81b"]
        self.assertTrue(
            _shared_document_for_character(
                {"source_type": "main_story", "title": "global", "text": "world"}, character
            )
        )
        self.assertTrue(
            _shared_document_for_character(
                {"source_type": "event_lore", "title": "event", "text": "里芙在码头等待分析员"},
                character,
            )
        )
        self.assertFalse(
            _shared_document_for_character(
                {"source_type": "event_lore", "title": "event", "text": "其他人完成了任务"},
                character,
            )
        )
        self.assertFalse(
            _shared_document_for_character(
                {"source_type": "weapon_lore", "title": "weapon", "text": "里芙推荐"}, character
            )
        )
        self.assertFalse(
            _document_mentions_character(
                {"title": "event", "text": "今天晴朗，风很大", "metadata": {}},
                MVP_CHARACTER_BY_ID["cf0569ac6de9"],
            )
        )


if __name__ == "__main__":
    unittest.main()
