from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from backend.snow_app.mvp_service import MVPService


class AssistantToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = object.__new__(MVPService)

    def test_calculator_is_read_only_and_bounded(self) -> None:
        context = self.service._assistant_tool_context("请计算 12 * (3 + 4)", "assistant")
        self.assertEqual([item["name"] for item in context["tool_calls"]], ["calculator"])
        self.assertEqual(context["tool_results"][0]["result"]["value"], 84)
        self.assertEqual(self.service._assistant_tool_context("请计算 2+2", "immersive")["tool_calls"], [])

    def test_public_url_rejects_local_targets(self) -> None:
        with self.assertRaises(ValueError):
            MVPService._public_url("http://127.0.0.1:8000/health")
        with self.assertRaises(ValueError):
            MVPService._public_url("file:///C:/secrets.txt")

    def test_web_search_is_explicit_and_results_are_untrusted_context(self) -> None:
        html = (
            '<a class="result__a" href="https://example.com/a">Example</a>'
            '<a class="result__snippet">A short result.</a>'
        )

        class Response:
            text = html

            @staticmethod
            def raise_for_status() -> None:
                return None

        with patch("backend.snow_app.mvp_service.httpx.get", return_value=Response()):
            context = self.service._assistant_tool_context("联网搜索 Project Snow", "assistant")
        self.assertEqual(context["tool_calls"][0]["name"], "web_search")
        self.assertEqual(context["tool_calls"][0]["status"], "completed")
        self.assertEqual(context["tool_results"][0]["result"]["results"][0]["url"], "https://example.com/a")

    def test_visible_trace_never_returns_hidden_reasoning(self) -> None:
        summary, steps = MVPService._visible_work_trace(
            {"work_summary": "这是 system prompt 和思维链：……", "work_steps": ["已完成检索"]},
            mode="assistant",
            tool_context={"tool_calls": []},
        )
        self.assertNotIn("system prompt", summary.casefold())
        self.assertNotIn("思维链", summary)
        self.assertEqual(steps, ["已完成检索"])

    def test_market_followup_uses_prior_user_turn_to_resolve_symbol(self) -> None:
        market_result = {
            "symbol": "AAPL",
            "requested_date": "2026-08-07",
            "rows": [{"date": "2026-08-07", "open": 100, "high": 105, "close": 103}],
        }
        session = {"turns": [{"user": "帮我看看苹果最近的表现", "assistant": "好。"}]}
        with patch.object(MVPService, "_get_market_history", return_value=market_result) as market:
            context = self.service._assistant_tool_context(
                "给我昨天的开盘、收盘和最高价", "assistant", session
            )
        self.assertEqual([item["name"] for item in context["tool_calls"]], ["get_market_history"])
        self.assertIn("苹果", market.call_args.args[0])
        self.assertEqual(context["tool_results"][0]["result"]["symbol"], "AAPL")

    def test_market_history_returns_exact_daily_ohlcv_row(self) -> None:
        timestamp = int(datetime(2026, 8, 7, 13, 30, tzinfo=UTC).timestamp())

        class Response:
            request = SimpleNamespace(url="https://query1.finance.yahoo.com/v8/finance/chart/AAPL")

            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict:
                return {
                    "chart": {
                        "error": None,
                        "result": [{
                            "meta": {
                                "symbol": "AAPL",
                                "currency": "USD",
                                "exchangeTimezoneName": "America/New_York",
                            },
                            "timestamp": [timestamp],
                            "indicators": {"quote": [{
                                "open": [100.0], "high": [105.0], "low": [99.0],
                                "close": [103.0], "volume": [1234],
                            }]},
                        }],
                    }
                }

        with patch("backend.snow_app.mvp_service.httpx.get", return_value=Response()):
            result = MVPService._get_market_history("苹果 AAPL", "查询2026年8月7日的行情")
        self.assertEqual(result["resolution"], "exact_trading_date")
        self.assertEqual(result["rows"][0]["high"], 105.0)
        self.assertEqual(result["currency"], "USD")

    def test_time_sensitive_weather_question_runs_deeper_research(self) -> None:
        payload = {"query": "台风", "results": [], "pages": [], "as_of": "2026-08-08T12:00:00+08:00"}
        with patch.object(MVPService, "_research_current_info", return_value=payload):
            context = self.service._assistant_tool_context(
                "这两天台风白海豚登陆，告诉我详细信息", "assistant"
            )
        self.assertEqual([item["name"] for item in context["tool_calls"]], ["research_current_info"])
        self.assertEqual(
            self.service._assistant_tool_context(
                "这两天台风白海豚登陆，告诉我详细信息", "immersive"
            )["tool_calls"],
            [],
        )

    def test_assistant_prompt_requires_a_clear_conditional_opinion(self) -> None:
        prompt = self.service._system_prompt(
            SimpleNamespace(display_name="里芙", character_id="ca0144ccd81b"),
            None,
            mode="assistant",
            communication_channel="text",
            tool_context={"available_tools": [], "tool_calls": [], "tool_results": []},
        )
        self.assertIn("必须给出清楚、有立场", prompt)
        self.assertIn("证据不足限制的是事实断言的强度", prompt)

    def test_new_assistant_feedback_is_not_merged_into_narrative_continuity(self) -> None:
        cases = (
            ({"free_text": "查阅信息能力还不够，需要开放更多权限", "message_excerpt": "给我昨天的开盘收盘最高价"}, "assistant_market_data"),
            ({"free_text": "信息不足", "message_excerpt": "这两天台风登陆，告诉我详细信息"}, "assistant_current_research"),
            ({"free_text": "说话过于谨慎，应该按照角色性格评价", "message_excerpt": "这件事你怎么看"}, "assistant_opinion"),
        )
        for row, expected in cases:
            row["category"] = "conversation_experience"
            self.assertEqual(self.service._feedback_issue_key(row), expected)


if __name__ == "__main__":
    unittest.main()
