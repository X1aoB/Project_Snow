from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as frontend

SUBSCRIPTION_MODELS = [
    {"id": "gpt-5.6-luna", "display_name": "GPT-5.6 Luna", "reasoning_efforts": ["low", "medium", "high", "max"], "default_reasoning_effort": "medium"},
    {"id": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"], "default_reasoning_effort": "medium"},
    {"id": "fixture-limited", "display_name": "合成受限模型", "reasoning_efforts": ["low", "medium"], "default_reasoning_effort": "medium"},
]

@skipUnless(frontend.RUN_PUBLIC_E2E, "requires the public browser E2E tier")
class SubscriptionFrontendE2ETests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend.PublicFrontendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        frontend.PublicFrontendE2ETests().setUp()

    def _subscription_routes(self, page, *, status="waiting", model="gpt-5.6-luna", models=None, protocol_version=1):
        session = {"status": status, "model": model, "effort": "max", "protocol_version": protocol_version, "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat()}
        if models is not None:
            session["models"] = json.loads(json.dumps(models))
        calls = []

        def config(route):
            payload = route.fetch().json()
            payload["subscription"] = {"enabled": True, "model": "gpt-5.6-luna", "effort": "max", "protocol_version": 2}
            payload["providers"].append({"provider_id": "codex_subscription", "display_name": "Codex 订阅"})
            route.fulfill(json=payload)

        def subscription(route):
            path = route.request.url.rsplit("/", 1)[-1]
            calls.append(path)
            if path == "pair":
                self.assertEqual(route.request.post_data_json, {"accepted_transit_notice": True, "accepted_cost_notice": True, "accepted_local_history_notice": True})
                route.fulfill(json={"pairing_code": "TEST-PAIR-3412", "credential": "synthetic-subscription-credential", "provider": "codex_subscription", "expires_at": session["expires_at"], "pairing_expires_at": session.get("pairing_expires_at", session["expires_at"])})
            elif path == "status":
                route.fulfill(json=session)
            elif path == "disconnect":
                self.assertEqual(route.request.post_data_json, {})
                session["status"] = "disconnected"
                route.fulfill(json={"status": "disconnected"})
            else:
                route.fulfill(status=404, json={"detail": {"code": "invalid_request"}})

        page.route("**/public/v1/config", config)
        page.route("**/public/v1/subscription/**", subscription)
        return session, calls

    def _open(self, page):
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        page.locator("#open-settings").click()

    def _screenshot(self, page, name):
        if directory := os.environ.get("SNOW_SUBSCRIPTION_SCREENSHOTS"):
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / f"{name}.png"), animations="disabled")

    def _pair(self, page):
        self._open(page)
        page.locator("#model-mode-subscription").click()
        page.locator("#pair-subscription").click()
        page.locator("#subscription-status", has_text="已连接个人连接器").wait_for()

    @staticmethod
    def _wait_model_saved(page):
        # fill() can succeed against the inert composer behind an open dialog
        # without entering any text. A click completing is not a saved session.
        page.locator("#settings-dialog").wait_for(state="hidden")
        page.wait_for_function("document.querySelector('.provider-form').getAttribute('aria-busy') === 'false'")

    def _save_model(self, page):
        page.locator("#save-model").click()
        self._wait_model_saved(page)

    def _send_chat(self, page, text):
        page.locator("#message-input").fill(text)
        self.assertEqual(page.locator("#message-input").input_value(), text)
        with page.expect_request(lambda request: request.url.endswith("/public/v1/chat/stream")) as sent:
            page.locator("#send-message").click()
        return sent.value

    @staticmethod
    def _reply(route, text="合成模型选择回复"):
        result = {"communication_channel": "text", "content_blocks": [{"type": "message", "text": text}], "usage": {"provider_calls": 1}}
        route.fulfill(status=200, content_type="text/event-stream", body="event: done\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n")

    def test_catalogue_selection_sends_sol_medium_and_restores_after_reload(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS, protocol_version=2)
            calls = []

            def chat(route):
                calls.append(route.request.post_data_json)
                self._reply(route)

            page.route("**/public/v1/chat/stream", chat)
            self._pair(page)
            self.assertEqual(page.locator("#subscription-model-select").input_value(), "gpt-5.6-luna")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "max")
            page.locator("#subscription-model-select").select_option("gpt-5.6-sol")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "max")
            page.locator("#subscription-effort-select").select_option("medium")
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"))
            self._save_model(page)
            self._send_chat(page, "使用所选的 Sol 和 medium")
            page.locator("#timeline").get_by_text("合成模型选择回复", exact=True).wait_for()
            self.assertEqual(calls[0]["model"], "gpt-5.6-sol")
            self.assertEqual(calls[0]["reasoning_effort"], "medium")
            page.reload(wait_until="networkidle")
            page.locator("#open-settings").click()
            self.assertEqual(page.locator("#subscription-model-select").input_value(), "gpt-5.6-sol")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "medium")
            self.assertIn("gpt-5.6-sol · medium", page.locator("#subscription-model-detail").inner_text())
            self._screenshot(page, "model-choice-desktop")
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                page.locator("#save-model").scroll_into_view_if_needed()
                metrics = page.locator("#save-model").evaluate("""el => { const r=el.getBoundingClientRect(); const modal=document.querySelector('#settings-dialog'); return {top:r.top,bottom:r.bottom,width:modal.clientWidth,scrollWidth:modal.scrollWidth}; }""")
                self.assertGreaterEqual(metrics["top"], 0)
                self.assertLessEqual(metrics["bottom"], 844)
                self.assertLessEqual(metrics["scrollWidth"], metrics["width"])
                self._screenshot(page, f"model-choice-mobile-{width}")
            browser.close()

    def test_model_change_retains_supported_effort_or_explicitly_shows_default(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS, protocol_version=2)
            self._pair(page)
            page.locator("#subscription-model-select").select_option("gpt-5.6-sol")
            page.locator("#subscription-effort-select").select_option("ultra")
            page.locator("#subscription-model-select").select_option("fixture-limited")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "medium")
            self.assertIn("默认推理强度", page.locator("#subscription-selection-hint").inner_text())
            self.assertEqual(page.locator("#subscription-effort-select option").evaluate_all("els => els.map(el => el.value)"), ["", "low", "medium"])
            page.locator("#subscription-effort-select").select_option("low")
            page.locator("#subscription-model-select").select_option("gpt-5.6-luna")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "low")
            browser.close()

    def test_account_without_luna_requires_explicit_model_selection(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS[1:2], protocol_version=2)
            self._pair(page)
            self.assertEqual(page.locator("#subscription-model-select").input_value(), "")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "")
            self.assertIn("明确选择", page.locator("#subscription-selection-hint").inner_text())
            self.assertTrue(page.locator("#save-model").is_disabled())
            page.locator("#subscription-model-select").select_option("gpt-5.6-sol")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "medium")
            self.assertFalse(page.locator("#save-model").is_disabled())
            browser.close()

    def test_catalogue_change_requires_reselection_without_mutating_saved_configuration(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            session, calls = self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS, protocol_version=2)
            chat_requests = []
            page.on("request", lambda request: chat_requests.append(request.url) if request.url.endswith("/chat/stream") else None)
            self._pair(page)
            page.locator("#subscription-model-select").select_option("gpt-5.6-sol")
            page.locator("#subscription-effort-select").select_option("medium")
            self._save_model(page)
            page.locator("#open-settings").click()
            session["models"] = [{**SUBSCRIPTION_MODELS[1], "reasoning_efforts": ["high"], "default_reasoning_effort": "high"}]
            page.locator("#subscription-selection-hint", has_text="已改变").wait_for()
            self.assertEqual(page.locator("#subscription-model-select").input_value(), "gpt-5.6-sol")
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "")
            self.assertTrue(page.locator("#save-model").is_disabled())
            page.wait_for_timeout(3200)
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "")
            stored = page.evaluate("JSON.parse(sessionStorage.getItem('project-snow-public:byok'))")
            self.assertEqual((stored["model"], stored["reasoningEffort"]), ("gpt-5.6-sol", "medium"))
            page.locator('[data-close-dialog="settings-dialog"]').click()
            self.assertIn("配置模型", page.locator("#message-input").get_attribute("placeholder"))
            page.locator("#message-input").fill("权限失效后先重新配置")
            page.locator("#send-message").click()
            page.locator("#settings-dialog").wait_for(state="visible")
            self.assertEqual(chat_requests, [])
            page.locator("#subscription-effort-select").select_option("high")
            self._save_model(page)
            self.assertFalse(page.locator("#send-message").is_disabled())
            browser.close()

    def test_changed_effort_is_saved_explicitly_and_uses_a_new_retry_snapshot(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS, protocol_version=2)
            calls = []

            def chat(route):
                calls.append(route.request.post_data_json)
                if len(calls) == 1:
                    route.fulfill(status=503, json={"detail": {"code": "provider_timeout"}})
                else:
                    self._reply(route)

            page.route("**/public/v1/chat/stream", chat)
            self._pair(page)
            page.locator("#subscription-model-select").select_option("gpt-5.6-sol")
            page.locator("#subscription-effort-select").select_option("medium")
            self._save_model(page)
            self._send_chat(page, "合成强度重试测试")
            page.locator("[data-retry-message]").wait_for()
            page.locator("#open-settings").click()
            page.locator("#subscription-effort-select").select_option("high")
            page.wait_for_timeout(3200)
            self.assertEqual(page.locator("#subscription-effort-select").input_value(), "high")
            self.assertEqual(page.evaluate("JSON.parse(sessionStorage.getItem('project-snow-public:byok')).reasoningEffort"), "medium")
            self._save_model(page)
            page.locator("[data-retry-message]").click()
            page.locator("#timeline").get_by_text("合成模型选择回复", exact=True).wait_for()
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0]["request_id"], calls[1]["request_id"])
            self.assertEqual(calls[0]["reasoning_effort"], "medium")
            self.assertEqual(calls[1]["reasoning_effort"], "high")
            self.assertEqual(calls[0]["model"], calls[1]["model"])
            browser.close()

    def test_disabled_subscription_keeps_tutorial_available_without_pairing(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._open(page)
            self.assertEqual(page.locator("#provider-select").input_value(), "openai")
            page.locator("#model-mode-subscription").click()
            self.assertIn("本站尚未启用", page.locator("#subscription-status").inner_text())
            self.assertTrue(page.locator("#pair-subscription").is_hidden())
            self.assertTrue(page.locator("#save-model").is_disabled())
            self.assertTrue(page.locator("#api-key").is_hidden())
            page.locator("#subscription-tutorial summary").click()
            tutorial = page.locator("#subscription-tutorial").inner_text()
            for text in ("codex login", "Node.js", "0.153.4", "0.151", "auth.json", "自己的订阅", "较高强度可能等待更久"):
                self.assertIn(text, tutorial if text != "Node.js" else page.locator("#subscription-settings").inner_text())
            self.assertEqual(page.locator("#subscription-connect-command").inner_text(), f"node snow-codex-connector.mjs --site {self.base_url}")
            for width in (1280, 390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                self.assertTrue(page.locator("#settings-dialog").evaluate("el => el.scrollWidth <= el.clientWidth"))
                self._screenshot(page, f"tutorial-{width}")
            browser.close()

    def test_waiting_pair_is_not_configured_and_polling_stops_when_closed(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            session, calls = self._subscription_routes(page)
            self._open(page)
            page.locator("#model-mode-subscription").click()
            page.locator("#pair-subscription").click()
            page.locator("#subscription-pairing-code", has_text="TEST-PAIR-3412").wait_for()
            self.assertTrue(page.locator("#save-model").is_disabled())
            self.assertFalse(page.locator("#subscription-status").inner_text().startswith("已连接"))
            stored = page.evaluate("({local: JSON.stringify(localStorage), session: JSON.stringify(sessionStorage)})")
            self.assertNotIn("TEST-PAIR-3412", stored["local"] + stored["session"])
            self.assertNotIn("synthetic-subscription-credential", stored["local"])
            self.assertNotIn("project-snow-public:byok", stored["session"])
            self._screenshot(page, "pairing-desktop")
            page.locator('[data-close-dialog="settings-dialog"]').click()
            page.wait_for_timeout(150)
            count = calls.count("status")
            page.wait_for_timeout(3300)
            self.assertEqual(calls.count("status"), count)
            session["status"] = "connected"
            page.locator("#open-settings").click()
            page.locator("#subscription-status", has_text="已连接个人连接器").wait_for()
            self.assertTrue(page.locator("#subscription-pairing").is_hidden())
            cta = page.locator("#save-model").evaluate("""button => {
                const rect = button.getBoundingClientRect();
                const modal = document.querySelector('#settings-dialog').getBoundingClientRect();
                const tutorial = document.querySelector('#subscription-tutorial').getBoundingClientRect();
                const center = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
                return {top: rect.top, bottom: rect.bottom, height: rect.height, modalBottom: modal.bottom, viewportBottom: innerHeight, beforeTutorial: rect.bottom <= tutorial.top, unobscured: button === center || button.contains(center)};
            }""")
            self.assertGreaterEqual(cta["height"], 44)
            self.assertGreaterEqual(cta["top"], 0)
            self.assertLessEqual(cta["bottom"], min(cta["modalBottom"], cta["viewportBottom"]))
            self.assertTrue(cta["beforeTutorial"])
            self.assertTrue(cta["unobscured"])
            self._screenshot(page, "connected-desktop")
            self._save_model(page)
            page.locator("#settings-dialog").wait_for(state="hidden")
            saved = page.evaluate("JSON.parse(sessionStorage.getItem('project-snow-public:byok'))")
            self.assertEqual(saved["provider"], "codex_subscription")
            self.assertEqual(saved["model"], "gpt-5.6-luna")
            page.reload(wait_until="networkidle")
            page.locator("#open-settings").click()
            page.locator("#subscription-status", has_text="已连接个人连接器").wait_for()
            self.assertEqual(page.locator("#model-mode-subscription").get_attribute("aria-pressed"), "true")
            self.assertTrue(page.locator("#model-id").is_hidden())
            page.locator("#disconnect-subscription").click()
            page.locator("#subscription-status", has_text="尚未连接个人连接器").wait_for()
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"))
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:subscription-pair')"))
            self.assertEqual(calls.count("disconnect"), 1)
            browser.close()

    def test_connected_model_mismatch_never_saves_or_substitutes(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._subscription_routes(page, status="connected", model="gpt-other")
            self._open(page)
            page.locator("#model-mode-subscription").click()
            page.locator("#pair-subscription").click()
            page.locator("#subscription-status", has_text="不支持所选模型").wait_for()
            self.assertTrue(page.locator("#save-model").is_disabled())
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"))
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:subscription-pair')"))
            browser.close()

    def test_expired_pair_cannot_be_saved_and_can_be_regenerated(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            session, calls = self._subscription_routes(page)
            self._open(page)
            page.locator("#model-mode-subscription").click()
            session["pairing_expires_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
            page.locator("#pair-subscription").click()
            page.locator("#subscription-pairing-code", has_text="TEST-PAIR-3412").wait_for()
            page.locator("#subscription-status", has_text="配对码已过期").wait_for()
            self.assertTrue(page.locator("#subscription-pairing").is_hidden())
            self.assertTrue(page.locator("#save-model").is_disabled())
            self.assertFalse(page.locator("#pair-subscription").is_disabled())
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:subscription-pair')"))
            session["pairing_expires_at"] = session["expires_at"]
            page.locator("#pair-subscription").click()
            page.locator("#subscription-pairing-code", has_text="TEST-PAIR-3412").wait_for()
            self.assertEqual(calls.count("pair"), 2)
            browser.close()

    def test_existing_api_session_survives_browsing_subscription_tutorial(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._subscription_routes(page)
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            frontend.PublicFrontendE2ETests._configure_model(page)
            before = page.evaluate("sessionStorage.getItem('project-snow-public:byok')")
            page.locator("#open-settings").click()
            page.locator("#model-mode-subscription").click()
            page.locator("#model-mode-api").click()
            self.assertEqual(page.locator("#provider-select").input_value(), "openai")
            self.assertTrue(page.locator("#model-session-actions").evaluate("el => el.previousElementSibling.id === 'setup-error'"))
            self.assertEqual(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"), before)
            browser.close()

    def test_delayed_subscription_save_waits_for_dialog_close_before_first_chat(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(timezone_id="UTC", reduced_motion="reduce")
            session, _ = self._subscription_routes(page, status="connected")
            requests = []
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            def chat(route):
                requests.append(route.request.post_data_json)
                self._reply(route)

            page.route("**/public/v1/chat/stream", chat)
            self._pair(page)
            pending_status = []
            page.route("**/public/v1/subscription/status", lambda route: pending_status.append(route))
            with page.expect_request(lambda request: request.url.endswith("/subscription/status")):
                page.locator("#save-model").click()
            self.assertTrue(page.locator("#settings-dialog").is_visible())
            self.assertEqual(page.locator(".provider-form").get_attribute("aria-busy"), "true")
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"))
            self.assertEqual(requests, [])
            self.assertEqual(len(pending_status), 1)
            pending_status[0].fulfill(json=session)
            self._wait_model_saved(page)
            self._send_chat(page, "保存完成后发送合成消息")
            page.locator("#timeline").get_by_text("合成模型选择回复", exact=True).wait_for()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["provider"], "codex_subscription")
            self.assertEqual(page_errors, [])
            browser.close()

    def test_disconnected_subscription_chat_is_not_automatically_resent(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._subscription_routes(page, status="connected")
            attempts = []

            def chat(route):
                attempts.append(route.request.post_data_json)
                route.fulfill(status=503, json={"detail": {"code": "subscription_not_connected"}})

            page.route("**/public/v1/chat/stream", chat)
            self._open(page)
            page.locator("#model-mode-subscription").click()
            page.locator("#pair-subscription").click()
            page.locator("#subscription-status", has_text="已连接个人连接器").wait_for()
            self._save_model(page)
            self._send_chat(page, "合成订阅断线测试")
            page.locator("#request-status", has_text="重新配对").wait_for()
            page.wait_for_timeout(1300)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["provider"], "codex_subscription")
            self.assertEqual(attempts[0]["model"], "gpt-5.6-luna")
            self.assertIsNone(page.evaluate("sessionStorage.getItem('project-snow-public:byok')"))
            browser.close()

    def test_unknown_subscription_result_keeps_reconciliation_id_without_retry_button(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page()
            self._subscription_routes(page, status="connected")
            attempts = []

            def chat(route):
                attempts.append(route.request.post_data_json)
                route.fulfill(status=503, json={"detail": {"code": "subscription_result_unknown"}})

            page.route("**/public/v1/chat/stream", chat)
            self._open(page)
            page.locator("#model-mode-subscription").click()
            page.locator("#pair-subscription").click()
            page.locator("#subscription-status", has_text="已连接个人连接器").wait_for()
            self._save_model(page)
            self._send_chat(page, "合成结果未确认测试")
            page.locator("#request-status", has_text="结果尚未确认").wait_for()
            self.assertEqual(page.locator("[data-retry-message]").count(), 0)
            self.assertIn("避免重复发送", page.locator(".message-recovery-note").inner_text())
            page.wait_for_timeout(1300)
            self.assertEqual(len(attempts), 1)
            browser.close()

    def test_subscription_pauses_background_summaries_without_changing_api_preference(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            self._subscription_routes(page, status="connected")
            replies = []
            summaries = []

            def chat(route):
                replies.append(route.request.post_data_json)
                result = {"communication_channel": "text", "content_blocks": [{"type": "message", "text": f"合成回复 {len(replies)}"}], "usage": {"provider_calls": 1}}
                route.fulfill(status=200, content_type="text/event-stream", body="event: done\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n")

            def summary(route):
                summaries.append(route.request.post_data_json)
                route.fulfill(json={"summary": "合成 API 摘要", "pending_topics": []})

            page.route("**/public/v1/chat/stream", chat)
            page.route("**/public/v1/chat/summarize", summary)
            self._open(page)
            page.locator("#model-mode-subscription").click()
            page.locator("#pair-subscription").click()
            page.locator("#subscription-status", has_text="已连接个人连接器").wait_for()
            self._save_model(page)
            for index in range(1, 13):
                self._send_chat(page, f"合成订阅消息 {index}")
                page.locator("#timeline").get_by_text(f"合成回复 {index}", exact=True).wait_for()
                page.wait_for_function("!document.querySelector('#send-message').disabled")
            self.assertEqual(len(replies), 12)
            self.assertEqual(summaries, [])
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            self.assertTrue(page.locator("#auto-summary-enabled").is_checked())
            self.assertTrue(page.locator("#subscription-summary-hint").is_visible())
            page.locator('[data-settings-tab="models"]').click()
            page.locator("#model-mode-api").click()
            page.locator("#api-key").fill("sk-synthetic-api")
            page.locator("#toggle-advanced-model").click()
            page.locator("#model-id").fill("gpt-e2e")
            self._save_model(page)
            self._send_chat(page, "切回 API 后继续")
            page.locator("#timeline").get_by_text("合成回复 13", exact=True).wait_for()
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            page.locator("#summary-last-updated", has_text="最近更新").wait_for()
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["provider"], "openai")
            self.assertNotIn("reasoning_effort", summaries[0])
            self.assertNotIn("reasoning_effort", replies[-1])
            self.assertTrue(page.locator("#auto-summary-enabled").is_checked())
            self.assertTrue(page.locator("#subscription-summary-hint").is_hidden())
            browser.close()

    def test_subscription_arrival_uses_saved_model_and_effort(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS, protocol_version=2)
            arrivals = []

            def arrival(route):
                arrivals.append(route.request.post_data_json)
                route.continue_()

            page.route("**/public/v1/presence/arrival", arrival)
            self._pair(page)
            page.locator("#subscription-model-select").select_option("gpt-5.6-sol")
            page.locator("#subscription-effort-select").select_option("medium")
            self._save_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#stage-speech").get_by_text("你来了。", exact=True).wait_for()
            self.assertEqual(len(arrivals), 1)
            self.assertEqual(arrivals[0]["provider"], "codex_subscription")
            self.assertEqual(arrivals[0]["model"], "gpt-5.6-sol")
            self.assertEqual(arrivals[0]["reasoning_effort"], "medium")
            browser.close()

    def test_legacy_luna_snapshot_replays_without_adding_reasoning_effort(self):
        with frontend.sync_playwright() as playwright:
            browser = frontend._launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            self._subscription_routes(page, status="connected", models=SUBSCRIPTION_MODELS, protocol_version=2)
            calls = []

            def chat(route):
                calls.append(route.request.post_data_json)
                if len(calls) == 1:
                    route.fulfill(status=503, json={"detail": {"code": "provider_timeout"}})
                else:
                    self._reply(route)

            page.route("**/public/v1/chat/stream", chat)
            self._pair(page)
            self._save_model(page)
            self._send_chat(page, "合成旧版 Luna 请求")
            page.locator("[data-retry-message]").wait_for()
            legacy_snapshot = page.evaluate("""async () => {
                const saved = JSON.parse(sessionStorage.getItem('project-snow-public:byok'));
                delete saved.reasoningEffort;
                sessionStorage.setItem('project-snow-public:byok', JSON.stringify(saved));
                const db = await new Promise(resolve => { const req=indexedDB.open('project-snow-public',4); req.onsuccess=()=>resolve(req.result); });
                let legacy;
                await new Promise((resolve,reject) => {
                    const tx=db.transaction('messages','readwrite');
                    const store=tx.objectStore('messages');
                    const req=store.openCursor();
                    req.onsuccess=()=>{ const c=req.result; if(c){ const message=c.value;
                        if(message.role==='user' && message.requestSnapshot){ delete message.requestSnapshot.reasoning_effort; legacy=structuredClone(message.requestSnapshot); c.update(message); }
                        c.continue(); } };
                    tx.oncomplete=resolve; tx.onerror=()=>reject(tx.error);
                });
                db.close(); return legacy;
            }""")
            self.assertNotIn("reasoning_effort", legacy_snapshot)
            page.reload(wait_until="networkidle")
            page.locator("[data-retry-message]").click()
            page.locator("#timeline").get_by_text("合成模型选择回复", exact=True).wait_for()
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["request_id"], legacy_snapshot["request_id"])
            self.assertNotIn("reasoning_effort", calls[1])
            self.assertEqual({key: value for key, value in calls[1].items() if key != "credential"}, legacy_snapshot)
            browser.close()
