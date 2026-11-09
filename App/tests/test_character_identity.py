"""Every selectable character always knows her own facts and what day it is.

Regression for 2026-11-08: asked what day it was on her own birthday, 凯茜娅 did
not know.  Her corpus has no profile page and no prompt carried the date.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_knowledge import CharacterIdentityCatalog, PublicKnowledgeError
from pipelines.build_character_identity import OUTPUT_PATH, build, page_facts, render

APP_ROOT = Path(__file__).resolve().parents[1]
KATHYA = "25b23cb64398"
TESS = "673ba6851b05"
TZ = ZoneInfo("Asia/Hong_Kong")


def service():
    instance = object.__new__(MVPService)
    instance.repository = Mock()
    instance.repository.documents_by_id.return_value = {}
    return instance


def user_prompt(character, now):
    context = {"hits": [], "provisional_relations": [], "local_now": now}
    return json.loads(service()._prompt(character, "今天是什么日子", context))


def character(character_id):
    return next(item for item in MVP_CHARACTERS if item.character_id == character_id)


class CatalogTests(TestCase):
    def test_every_selectable_character_has_a_card_matching_the_public_birthdays(self):
        catalog = CharacterIdentityCatalog()
        feed = json.loads((APP_ROOT / "public_frontend/announcements.json").read_text(encoding="utf-8"))
        birthdays = {item["character_id"]: item for item in feed["birthdays"]}
        self.assertEqual(len(MVP_CHARACTERS), 22)
        for item in MVP_CHARACTERS:
            with self.subTest(character=item.display_name):
                card = catalog.card(item.character_id)
                self.assertIsNotNone(card)
                self.assertEqual(card["display_name"], item.display_name)
                public = birthdays[item.character_id]
                self.assertEqual(card["birthday"], {"month": public["month"], "day": public["day"]})
                self.assertTrue(card["armors"])
                for field in ("full_name", "birthday", "height"):
                    if field in card:
                        self.assertTrue(card["evidence"][field], field)

    def test_loader_refuses_a_damaged_card(self):
        valid = {"character_id": "a", "display_name": "甲", "birthday": {"month": 2, "day": 28}}
        for name, characters in (
            ("duplicate", [valid, valid]),
            ("impossible date", [{**valid, "birthday": {"month": 2, "day": 30}}]),
            ("missing birthday", [{"character_id": "a", "display_name": "甲"}]),
            ("missing name", [{**valid, "display_name": ""}]),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                path = Path(folder, "card.json")
                path.write_text(json.dumps({"schema_version": 1, "knowledge_version": "t", "characters": characters}), encoding="utf-8")
                with self.assertRaises(PublicKnowledgeError):
                    CharacterIdentityCatalog(path)

    def test_info_box_parsing_handles_the_three_page_layouts(self):
        styled = '<th colspan="2"><font style="color:#000">全名</font></th>\n<td colspan="2">米娅·丝凯依\n</td>'
        self.assertEqual(page_facts(styled), {"full_name": "米娅·丝凯依"})
        self.assertEqual(page_facts("<th>全名/本名\n</th>\n<td>鸣濑晴\n</td>"), {"full_name": "鸣濑晴"})
        self.assertEqual(page_facts("<th>生日\n</th>\n<td>11月08日\n</td>"), {"birthday": (11, 8)})
        self.assertEqual(page_facts("<th>身高\n</th>\n<td>170厘米\n</td>"), {"height": "170cm"})
        self.assertEqual(page_facts("<th>身高</th><td>158cm 自称160cm</td>"), {"height": "158cm（自称160cm）"})
        self.assertEqual(page_facts("<th>身高</th><td></td>"), {})

    def test_committed_card_matches_the_read_only_sources_when_available(self):
        data_root = Path(os.getenv("SNOW_DATA_ROOT") or APP_ROOT.parent / "Data")
        if not (data_root / "Source/character_armors").is_dir():
            self.skipTest("Original local data package is not present in portable CI")
        payload, problems = build(data_root)
        self.assertEqual(problems, [])
        self.assertEqual(render(payload), OUTPUT_PATH.read_text(encoding="utf-8"))


class SelfKnowledgePromptTests(TestCase):
    def test_every_character_is_told_her_own_facts_without_retrieval(self):
        catalog = CharacterIdentityCatalog()
        for item in MVP_CHARACTERS:
            with self.subTest(character=item.display_name):
                prompt = service()._system_prompt(item, None, communication_channel="text", dialogue_v2=True)
                birthday = catalog.card(item.character_id)["birthday"]
                self.assertIn(f"生日：{birthday['month']}月{birthday['day']}日", prompt)
                self.assertIn("【今天的日期】", prompt)
                self.assertLess(prompt.index("【你对自己的基本认知"), prompt.index("身份与证据规则"))

    def test_the_stable_prefix_does_not_change_with_the_date(self):
        # Only the per-turn user message carries the date.
        prompt = service()._system_prompt(character(KATHYA), None)
        self.assertNotIn("2026年", prompt.split("【今天的日期】", 1)[1].split("身份与证据规则", 1)[0])

    def test_rule_about_dated_stories_does_not_silence_today(self):
        prompt = service()._system_prompt(character(KATHYA), None)
        self.assertIn("today_context 和你的基本认知不是检索证据，不受这一条限制", prompt)

    def test_today_context_marks_her_birthday_on_that_beijing_date_only(self):
        on_the_day = user_prompt(character(KATHYA), datetime(2026, 11, 8, 0, 0, tzinfo=TZ))["today_context"]
        self.assertEqual(on_the_day, {"date": "2026年11月8日", "weekday": "星期日", "timezone": "北京时间",
                                      "own_birthday_today": True, "birthdays_today": []})
        before = user_prompt(character(KATHYA), datetime(2026, 11, 7, 23, 59, tzinfo=TZ))["today_context"]
        self.assertFalse(before["own_birthday_today"])
        self.assertEqual(before["date"], "2026年11月7日")

    def test_another_character_can_see_who_is_celebrating(self):
        today = user_prompt(character(TESS), datetime(2026, 11, 8, 12, 0, tzinfo=TZ))["today_context"]
        self.assertFalse(today["own_birthday_today"])
        self.assertEqual(today["birthdays_today"], ["凯茜娅"])

    def test_date_and_birthday_questions_get_their_own_answer_contract(self):
        for message in ("今天是什么日子", "今天是什么日子呀？", "早安，今天几号", "今天星期几", "今天的日期是多少",
                        "今天是你的生日吗", "今天是谁的生日", "你的生日是什么时候", "你生日几号", "你哪天过生日",
                        "生日快乐，凯茜娅！"):
            with self.subTest(message):
                self.assertEqual(MVPService._question_focus(message), "date_or_occasion")
                self.assertIn("today_context", MVPService._response_contract(message, ()))

    def test_nearby_small_talk_keeps_its_existing_route(self):
        for message, focus in (("今天吃什么", "food_or_drink"), ("今天过得怎么样", "casual_check_in"),
                               ("今天在做什么", "current_activity"), ("早安", "casual_check_in")):
            with self.subTest(message):
                self.assertEqual(MVPService._question_focus(message), focus)

    def test_admitting_the_birthday_is_not_rejected_as_an_invented_state(self):
        context = {"question_focus": "date_or_occasion", "hits": [], "mode": "immersive", "scene_state": {},
                   "local_now": datetime(2026, 11, 8, 12, 0, tzinfo=TZ)}
        answer = "今天是11月8日，星期日——也是我的生日哦。"
        violations = MVPService._answer_guardrail_violations("今天是什么日子", answer, context, "immersive")
        self.assertEqual(violations, [])
