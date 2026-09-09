from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_public_frontend_e2e as frontend


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

    def _subscription_routes(self, page, *, status="waiting", model="gpt-5.6-luna"):
        session = {"status": status, "model": model, "effort": "max", "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat()}
        calls = []

        def config(route):
            payload = route.fetch().json()
            payload["subscription"] = {"enabled": True, "model": "gpt-5.6-luna", "effort": "max", "protocol_version": 1}
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
            for text in ("codex login", "Node.js", "0.153.4", "0.151", "auth.json", "自己的订阅", "max 可能等待更久"):
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
            page.locator("#save-model").click()
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
            page.locator("#subscription-status", has_text="不支持 Luna Max").wait_for()
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
            page.locator("#save-model").click()
            page.locator("#message-input").fill("合成订阅断线测试")
            page.locator("#send-message").click()
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
            page.locator("#save-model").click()
            page.locator("#message-input").fill("合成结果未确认测试")
            page.locator("#send-message").click()
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
            page.locator("#save-model").click()
            for index in range(1, 13):
                page.locator("#message-input").fill(f"合成订阅消息 {index}")
                page.locator("#send-message").click()
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
            page.locator("#save-model").click()
            page.locator("#message-input").fill("切回 API 后继续")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("合成回复 13", exact=True).wait_for()
            page.locator("#open-settings").click()
            page.locator('[data-settings-tab="history"]').click()
            page.locator("#summary-last-updated", has_text="最近更新").wait_for()
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["provider"], "openai")
            self.assertTrue(page.locator("#auto-summary-enabled").is_checked())
            self.assertTrue(page.locator("#subscription-summary-hint").is_hidden())
            browser.close()
