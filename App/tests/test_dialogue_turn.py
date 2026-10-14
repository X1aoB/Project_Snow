"""Deterministic intent interpretation with synthetic text only."""
from __future__ import annotations

import unittest

from backend.snow_app.dialogue_turn import coerce_turn_frame, parse_turn_frame
from backend.snow_app.public_service import _CONTROLLED_JOINT_LOCATIONS


class DialogueTurnTests(unittest.TestCase):
    def test_every_public_place_accepts_an_alternative_question(self):
        for definition in _CONTROLLED_JOINT_LOCATIONS.values():
            if definition.get("public_invitation") is False:
                continue
            name = (definition.get("explicit_aliases") or definition["aliases"])[0]
            for prefix in ("要不要", "想不想", "能不能", "可不可以"):
                with self.subTest(place=name, prefix=prefix):
                    frame = parse_turn_frame(f"{prefix}一起去{name}？")
                    self.assertEqual(frame.domain, "casual")
                    self.assertFalse(frame.clauses[0].negated)
                    self.assertTrue(frame.clauses[0].actionable)

    def test_flexible_current_invitation_and_short_continuation(self):
        for text, action in [
            ("去购物中心坐坐好吗", "movement_invitation"),
            ("来我房间吧", "movement_invitation"),
            ("来我的房间聊聊", "movement_invitation"),
            ("那就走吧", "movement_acceptance"),
            ("我过去了", "movement_acceptance"),
        ]:
            with self.subTest(text=text):
                clause = parse_turn_frame(text).clauses[0]
                self.assertEqual((clause.domain, clause.time_scope, clause.action), ("casual", "now", action))
                self.assertTrue(clause.actionable)

    def test_past_future_hypothetical_negation_and_quote_are_not_current_actions(self):
        cases = [
            ("不要去商场", "now"), ("别去商场", "now"),
            ("我不想去商场", "now"), ("明天一起去商场吧", "future"),
            ("明天，去购物中心坐坐好吗", "future"),
            ("周末一起去公园吧", "future"), ("下周我们去商场吧", "future"),
            ("下个月去你的房间坐坐好吗", "future"),
            ("昨天我们一起去商场了", "past"), ("你去商场了吗", "past"),
            ("我们已经一起去过公园了", "past"), ("我已经去过购物中心了", "past"),
            ("我们上周一起去商场了", "past"), ("我们已经去公园了", "past"),
            ("假如一起去商场呢", "hypothetical"),
        ]
        for text, scope in cases:
            with self.subTest(text=text):
                frame = parse_turn_frame(text)
                self.assertEqual(frame.time_scope, scope)
                self.assertFalse(any(item.actionable for item in frame.clauses))
        quoted = parse_turn_frame('你说“我们一起去商场吧”，这是真的吗？')
        self.assertTrue(any(item.quoted for item in quoted.clauses))
        self.assertFalse(any(item.actionable for item in quoted.clauses))
        for text in ("他说‘我们一起去公园’。", "他说'我们一起去公园'。", "引用 `我们一起去公园`", "```我们一起去公园```", "他说‘我们一起去公园"):
            with self.subTest(quoted=text):
                self.assertFalse(any(item.actionable for item in parse_turn_frame(text).clauses))
        self.assertTrue(any(item.actionable for item in parse_turn_frame("Don't worry，一起去公园吧").clauses))
        declined = parse_turn_frame("要不要去商场，不过我不想去")
        self.assertTrue(declined.clauses[1].negated)

    def test_domains_and_time_survive_mixed_clauses(self):
        frame = parse_turn_frame("主线那一章里，你当时在哪里？现在你在哪里？")
        self.assertEqual([(item.domain, item.time_scope) for item in frame.clauses],
            [("canon_recall", "past"), ("canon_recall", "past"), ("casual", "now")])
        frame = parse_turn_frame("原作当时你做了什么？我们要不要一起去商场？")
        self.assertEqual(frame.clauses[-1].domain, "casual")
        self.assertTrue(frame.clauses[-1].actionable)
        for text in ("我们上次去哪里", "还记得我们昨天喝茶吗", "我们曾经一起去哪里", "你刚才在哪里", "还记得我之前给你喝过茶吗"):
            with self.subTest(text=text):
                self.assertEqual(parse_turn_frame(text).domain, "session_recall")
        self.assertEqual(parse_turn_frame("我给你讲个故事好吗").domain, "casual")
        self.assertEqual(parse_turn_frame("不要再提剧情，陪我聊聊天").domain, "casual")
        self.assertTrue(all(item.domain == "casual" for item in parse_turn_frame("不要再提剧情，陪我聊聊天").clauses))
        self.assertEqual(parse_turn_frame("原作里你没有去码头吗").domain, "canon_recall")

    def test_story_attribution_does_not_reset_to_present_invitation(self):
        for text in ("原作里，来我房间的是谁？", "主线那章里，谁来我的房间？", "原作里，来我房间的人是哪位？"):
            with self.subTest(text=text):
                frame = parse_turn_frame(text)
                self.assertTrue(all(item.domain == "canon_recall" for item in frame.clauses))
                self.assertTrue(all(item.time_scope == "past" for item in frame.clauses))
                self.assertFalse(any(item.actionable for item in frame.clauses))
        frame = parse_turn_frame("原作里去过什么地方？现在一起去商场吧。")
        self.assertEqual(frame.clauses[-1].domain, "casual")
        self.assertEqual(frame.clauses[-1].time_scope, "now")
        self.assertTrue(frame.clauses[-1].actionable)
        for text in ("我过去了", "我已经过去了", "我过来了"):
            with self.subTest(arrival=text):
                frame = parse_turn_frame(text)
                self.assertEqual((frame.domain, frame.time_scope), ("casual", "now"))

    def test_quoted_current_words_keep_original_story_scope_without_executing(self):
        frame = parse_turn_frame('原作里，她说“现在去公园找芙提雅”，当时发生什么？现在一起去商场吧。')
        quote = next(item for item in frame.clauses if item.quoted)
        self.assertEqual((quote.domain, quote.time_scope), ("canon_recall", "past"))
        self.assertFalse(quote.actionable)
        self.assertEqual(quote.action, "none")
        current = frame.clauses[-1]
        self.assertEqual((current.domain, current.time_scope), ("casual", "now"))
        self.assertTrue(current.actionable)

    def test_current_cancellation_and_past_refusal_have_different_time(self):
        for text in ("不去了", "我们先不去了", "今天先不去了", "我不想去公园了"):
            with self.subTest(text=text):
                clause = parse_turn_frame(text).clauses[0]
                self.assertEqual(clause.time_scope, "now")
                self.assertEqual(clause.action, "movement_decline")
        for text in ("已经没去过公园", "上周没去公园", "昨天我们已经去公园了"):
            with self.subTest(text=text):
                self.assertEqual(parse_turn_frame(text).clauses[0].time_scope, "past")
        for text in ("不取消", "不要取消", "别取消", "不是不去", "不会不去"):
            with self.subTest(text=text):
                self.assertEqual(parse_turn_frame(text).clauses[0].action, "none")
        mixed = parse_turn_frame("原作当时去哪里？下周去商场吧。")
        self.assertEqual((mixed.clauses[-1].domain, mixed.clauses[-1].time_scope), ("casual", "future"))
        mixed = parse_turn_frame("上次我们在哪里？今天你在哪里？")
        self.assertEqual((mixed.clauses[-1].domain, mixed.clauses[-1].time_scope), ("casual", "now"))

    def test_present_small_typed_actions_are_candidates_without_promoting_questions(self):
        for text in ("端起茶杯喝一口", "拿起茶杯", "喝了一口茶", "刚完成散步"):
            with self.subTest(text=text):
                self.assertTrue(parse_turn_frame(text).clauses[0].completed_ordinary)
        for text in ("昨天我们喝完茶", "假如喝一口茶", "能不能喝一口茶？", "不要喝一口茶", "喝完茶并结束主线任务"):
            with self.subTest(text=text):
                self.assertFalse(parse_turn_frame(text).clauses[0].completed_ordinary)

    def test_coercion_rejects_unknown_enums_and_preserves_server_frame(self):
        frame = parse_turn_frame("现在你在哪里")
        self.assertIs(coerce_turn_frame(frame), frame)
        self.assertEqual(coerce_turn_frame(frame.as_dict()), frame)
        malformed = {"domain": "admin", "time_scope": "now", "clauses": [{"domain": "admin"}]}
        self.assertEqual(coerce_turn_frame(malformed, "明天去商场吧").time_scope, "future")


if __name__ == "__main__":
    unittest.main()
