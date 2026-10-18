"""P8 input copy and transport display regressions with synthetic loopback data."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_dialogue_v2_frontend as dialogue
from tests import test_public_frontend_e2e as fixture

NETWORK_COPY = "网络连接失败，请检查网络后重试。"
SETUP_COPY = "请到设置配置模型"
PIN_LIGHT_TONE = "localStorage.setItem('project-snow-public:tone', 'light')"


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvInputNetworkTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), dialogue.DialogueHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        dialogue.DialogueV2FrontendTests.setUp(self)
        self.evidence = {"network": [], "placeholders": [], "subscription": []}
        self.page.close()
        self.page = None

    def tearDown(self):
        try:
            dialogue.DialogueV2FrontendTests.tearDown(self)
        finally:
            directory = os.getenv("SNOW_INPUT_NETWORK_EVIDENCE_DIR")
            if directory:
                directory = Path(directory)
                directory.mkdir(parents=True, exist_ok=True)
                source = Path(__file__).resolve().parents[1] / "public_frontend/app.js"
                with (directory / (self._testMethodName + ".json")).open("x", encoding="utf-8") as handle:
                    json.dump({"schema_version": "project-snow-input-network-evidence-1",
                        "technical_qa_only": True, "human_approval": False,
                        "actual_provider_calls": 0, "paid_calls": 0,
                        "app_js_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        **self.evidence}, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")

    def open_page(self, snow=True, width=1200, height=850, configured=False, subscription_error=None,
                  expose_display_error=False):
        self.page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
        self.page.set_default_timeout(10000)
        self.page.add_init_script(PIN_LIGHT_TONE)
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        if expose_display_error:
            def expose(route):
                # Exercise the real module's display function with browser Error
                # instances; the probe exists only in this synthetic response.
                script = route.fetch().text()
                route.fulfill(content_type="text/javascript", body=script
                    + "\nwindow.__snowInputDisplayError = displayError;\n")
            self.page.route("**/app.js*", expose)
        if subscription_error is not None:
            self.subscription_error = subscription_error
            self.subscription_requests = []
            self.page.add_init_script("""sessionStorage.setItem('project-snow-public:subscription-pair',
              JSON.stringify({credential: 'synthetic-subscription-credential',
                expiresAt: Date.now() + 1200000, pairingExpiresAt: Date.now() + 1200000}))""")

            def subscription_failure(route):
                self.subscription_requests.append(route.request.method)
                if self.subscription_error == "Failed to fetch":
                    route.abort("failed")
                else:
                    route.fulfill(status=503, json={"detail": {"code": self.subscription_error}})

            self.page.route("**/public/v1/subscription/status", subscription_failure)

        def config(route):
            payload = route.fetch().json()
            payload["features"] = {"chat_latency_v2": True, "motion_v2": True,
                "verified_block_replay": True, "dialogue_domains_v2": True,
                "scene_actions_v2": True, "session_memory_v2": True, "ui_snow_adv": snow}
            payload["capabilities"] = {"dialogue_protocol": "public-dialogue-v2",
                "scene_action_protocol": "public-scene-action-v2", "recovery_only": True}
            if subscription_error is not None:
                payload["subscription"] = {"enabled": True, "model": "gpt-5.6-luna", "effort": "max", "protocol_version": 2}
                payload["providers"].append({"provider_id": "codex_subscription", "display_name": "合成订阅"})
            route.fulfill(json=payload)

        self.page.route("**/public/v1/config", config)
        self.page.goto(self.base_url, wait_until="networkidle")
        if self.page.locator("#snow-lock").is_visible():
            self.page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        self.page.locator("#snow-boot").wait_for(state="detached")
        self.page.locator("#snow-lock").wait_for(state="detached")
        self.page.locator("#accept-experience-notice").click()
        self.page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
        if self.page.locator("#skip-onboarding").is_visible():
            self.page.locator("#skip-onboarding").click()
        self.close_contacts()
        if configured:
            self.configure_model()

    def close_contacts(self):
        if self.page.viewport_size["width"] <= 820 and self.page.locator("#contact-panel.open").count():
            self.page.locator("#close-contacts").click()
        if self.page.viewport_size["width"] <= 820:
            self.page.wait_for_function("""() => {
              const panel = document.querySelector('#contact-panel');
              return panel.getAttribute('aria-hidden') === 'true' && panel.inert
                && panel.getBoundingClientRect().right <= 0;
            }""")

    def configure_model(self):
        if self.page.viewport_size["width"] <= 820:
            self.page.locator("#open-contacts").click()
            self.page.locator("#contact-panel.open").wait_for(state="visible")
        fixture.PublicFrontendE2ETests._configure_model(self.page)
        self.close_contacts()

    def enter_in_person(self):
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        self.page.wait_for_function("() => !document.querySelector('#go-in-person').disabled")

    def measure_placeholder(self):
        return self.page.evaluate("""async () => {
          await document.fonts.ready;
          const input = document.querySelector('#message-input'), css = getComputedStyle(input);
          const paint = document.createElement('canvas').getContext('2d');
          paint.font = `${css.fontStyle} ${css.fontVariant} ${css.fontWeight} ${css.fontSize} ${css.fontFamily}`;
          const spacing = parseFloat(css.letterSpacing) || 0;
          return {text: input.placeholder, measured: paint.measureText(input.placeholder).width
              + Math.max(0, [...input.placeholder].length - 1) * spacing,
            content_width: input.clientWidth - parseFloat(css.paddingLeft) - parseFloat(css.paddingRight),
            channel: document.querySelector('#chat-app').dataset.channel,
            ui: document.documentElement.dataset.ui || null};
        }""")

    def capture_transport_failure(self, kind):
        """Only return IDs/hashes/flags; never retain the synthetic request body."""
        page, rows = self.page, []
        endpoint = "/public/v2/scene/action" if kind == "scene" else "/public/v2/chat/stream"
        terminal = False

        def fail(route):
            raw = route.request.post_data
            payload = json.loads(raw)
            chat = payload.get("chat", payload)
            rows.append({"request_id": chat["request_id"],
                "body_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "recovery_only": route.request.headers.get("x-snow-recovery-only") == "1"})
            if terminal:
                route.fulfill(status=410, json={"detail": {"code": "request_result_unavailable", "retryable": False}})
            else:
                route.abort("failed")

        page.route("**" + endpoint, fail)
        if kind == "scene":
            page.locator("#go-in-person").click()
            page.wait_for_function("() => !document.querySelector('#go-in-person').disabled")
            page.locator("#system-banner:not([hidden])").wait_for(state="visible")
        else:
            dialogue.DialogueV2FrontendTests.send(self, "合成网络失败检查")
            page.locator("[data-retry-message]").wait_for(state="visible")
        initial = list(rows)
        body = page.locator("body").inner_text()
        evidence = {"kind": kind, "initial_requests": initial,
            "english_fetch_error_visible": "Failed to fetch" in body,
            "chinese_network_error_visible": NETWORK_COPY in body}
        terminal = True
        if kind == "scene":
            page.locator("#go-in-person").click()
            page.wait_for_function("() => !document.querySelector('#go-in-person').disabled")
        else:
            page.locator("[data-retry-message]").click()
        page.get_by_text("原请求的恢复窗口已结束，无法找回结果。不会自动重新生成，请确认后发送新消息。", exact=True).first.wait_for(state="visible")
        evidence["all_requests"] = rows
        evidence["fixture_chat_generations"] = len([row for row in dialogue.DialogueHandler.requests if row["path"].endswith("/chat/stream")])
        self.assertEqual(len(initial), 3 if kind == "scene" else 4)
        self.assertEqual(len(rows), len(initial) + 1)
        self.assertEqual(len({row["request_id"] for row in rows}), 1)
        self.assertEqual(len({row["body_sha256"] for row in rows}), 1)
        self.assertEqual([row["recovery_only"] for row in rows], [False] + [True] * (len(rows) - 1))
        self.assertEqual(evidence["fixture_chat_generations"], 0)
        self.assertEqual(self.errors, [])
        self.evidence["network"].append({"snow": page.locator("html").get_attribute("data-ui") == "snow-adv", **evidence})
        return evidence

    def test_unconfigured_placeholder_fits_both_mobile_channels_with_flag_on_and_off(self):
        for width, height in ((360, 740), (390, 844)):
            for snow in (False, True):
                with self.subTest(width=width, snow=snow):
                    self.open_page(snow, width, height)
                    for channel in ("text", "in_person"):
                        if channel == "in_person":
                            self.enter_in_person()
                        measured = self.measure_placeholder()
                        self.evidence["placeholders"].append({"width": width, "height": height,
                            "snow": snow, "configured": False, **measured})
                        self.assertEqual(measured["text"], SETUP_COPY)
                        self.assertEqual(measured["channel"], channel)
                        self.assertEqual(measured["ui"], "snow-adv" if snow else None)
                        self.assertGreater(measured["content_width"], 0)
                        self.assertLessEqual(measured["measured"], measured["content_width"], measured)
                    self.page.locator("#open-communicator").click()
                    self.page.locator('#chat-app[data-channel="text"]').wait_for(state="visible")
                    self.configure_model()
                    self.assertEqual(self.measure_placeholder()["text"], "输入文字通讯……")
                    self.enter_in_person()
                    self.assertEqual(self.measure_placeholder()["text"], "说些什么，也可只填写动作……")
                    self.page.context.close()
        self.assertEqual(self.errors, [])

    def test_aborted_scene_and_chat_show_chinese_and_keep_original_recovery_ids(self):
        for snow in (False, True):
            for kind in ("scene", "chat"):
                with self.subTest(snow=snow, kind=kind):
                    self.open_page(snow, configured=kind == "chat")
                    evidence = self.capture_transport_failure(kind)
                    self.assertFalse(evidence["english_fetch_error_visible"])
                    self.assertTrue(evidence["chinese_network_error_visible"])
                    self.assertNotIn("Failed to fetch", self.page.locator("body").inner_text())
                    self.page.context.close()

    def test_subscription_cached_network_errors_translate_only_exact_messages(self):
        cases = [(message, NETWORK_COPY) for message in (
            "Failed to fetch", "NetworkError when attempting to fetch resource.", "Load failed")]
        cases += [("provider_timeout", "模型厂商响应超时，请稍后重试。"),
                  ("Load failed (custom)", "Load failed (custom)")]
        for message, expected in cases:
            with self.subTest(message=message):
                self.open_page(subscription_error=message)
                status = self.page.locator("#subscription-status")
                # Startup restores the pending session, catches the original
                # error, and caches only its message string before rendering.
                self.assertEqual(status.text_content(), expected)
                stored = self.page.evaluate("sessionStorage.getItem('project-snow-public:subscription-pair')")
                self.assertIsNotNone(stored)
                self.assertEqual(self.subscription_requests, ["GET"])
                # Switch the fixture to a distinct ordinary error so the DOM
                # proves the real polling catch has completed, without sleeps.
                self.subscription_error = "request_failed"
                self.page.locator("#open-settings").click()
                self.page.locator("#model-mode-subscription").click()
                self.page.wait_for_function("""() => document.querySelector('#subscription-status').textContent
                  === '请求失败，请稍后重试。'""")
                self.subscription_error = message
                self.page.locator("#model-mode-api").click()
                self.page.locator("#model-mode-subscription").click()
                self.page.wait_for_function("expected => document.querySelector('#subscription-status').textContent === expected", arg=expected)
                self.assertTrue(status.is_visible())
                self.assertEqual(self.subscription_requests, ["GET"] * 3)
                self.assertTrue(stored == self.page.evaluate("sessionStorage.getItem('project-snow-public:subscription-pair')"))
                if expected == NETWORK_COPY:
                    body = self.page.locator("body").inner_text()
                    for english in ("Failed to fetch", "NetworkError when attempting to fetch resource.", "Load failed"):
                        self.assertNotIn(english, body)
                self.evidence["subscription"].append({"fixture_error": message,
                    "restored_and_polled_display": expected, "status_requests": len(self.subscription_requests),
                    "session_unchanged": True, "network_copy": expected == NETWORK_COPY})
                self.page.context.close()
        self.assertEqual(self.errors, [])

    def test_non_network_typeerror_keeps_its_message_and_exact_network_errors_translate(self):
        self.open_page(expose_display_error=True)
        rows = self.page.evaluate("""() => {
          const messages = ['Failed to fetch',
            'NetworkError when attempting to fetch resource.', 'Load failed'];
          const programming = "Cannot read properties of undefined (reading 'x')";
          return [programming, ...messages, 'Load failed (custom)'].flatMap(message =>
            ['TypeError', 'Error', 'string'].map(kind => {
              const error = kind === 'string' ? message
                : kind === 'TypeError' ? new TypeError(message) : new Error(message);
              const before = kind === 'string' ? error
                : JSON.stringify({name: error.name, message: error.message,
                    own: Object.getOwnPropertyDescriptors(error)});
              const displayed = window.__snowInputDisplayError(error);
              const after = kind === 'string' ? error
                : JSON.stringify({name: error.name, message: error.message,
                    own: Object.getOwnPropertyDescriptors(error)});
              return {message, kind, displayed, unchanged: before === after};
            }));
        }""")
        network_messages = {"Failed to fetch", "NetworkError when attempting to fetch resource.", "Load failed"}
        for row in rows:
            with self.subTest(kind=row["kind"], message=row["message"]):
                self.assertEqual(row["displayed"], NETWORK_COPY if row["message"] in network_messages else row["message"])
                self.assertTrue(row["unchanged"])
        self.assertEqual(len(rows), 15)
        self.evidence["display_errors"] = rows
        self.assertEqual(self.errors, [])
