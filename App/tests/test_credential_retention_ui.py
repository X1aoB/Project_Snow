"""Browser checks for the opt-in credential retention ("凭证保存") in model settings.

The backend contract (handoff section 13.3) is simulated with routes: /public/v1/config advertises
credential_policy.retention, /public/v1/byok/session echoes the chosen retention, and
/public/v1/byok/revoke records what the page asks the server to invalidate. No model is called.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from tests import test_public_frontend_e2e as fixture

KEY = "project-snow-public:byok"
PENDING = "project-snow-public:byok-revoke"
NOTICE = "credential-retention-1"
HOURS = {"tab": 12, "2h": 2, "24h": 24, "3d": 72, "7d": 168}
SNOW = {"ui_snow_adv": True, "chat_latency_v2": True, "motion_v2": True, "dialogue_domains_v2": True,
        "scene_actions_v2": True, "session_memory_v2": True, "verified_block_replay": True}
CONTRAST = """nodes => nodes.filter(node => node.getClientRects().length).map(node => {
  const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
  const paint=canvas.getContext('2d',{willReadFrequently:true});
  const rgba=c=>{paint.clearRect(0,0,1,1);paint.fillStyle=c;paint.fillRect(0,0,1,1);const a=[...paint.getImageData(0,0,1,1).data];return [a[0],a[1],a[2],a[3]/255];};
  const over=(f,b)=>[0,1,2].map(i=>f[i]*f[3]+b[i]*(1-f[3])).concat(1);
  const layers=[];for(let n=node;n;n=n.parentElement){const c=rgba(getComputedStyle(n).backgroundColor);if(c[3]>0)layers.push(c);if(c[3]>=0.999)break;}
  let bg=[255,255,255,1];for(const c of layers.reverse())bg=over(c,bg);
  const fg=over(rgba(getComputedStyle(node).color),bg);
  const lum=c=>{const v=c.slice(0,3).map(x=>{x/=255;return x<=0.03928?x/12.92:((x+0.055)/1.055)**2.4;});return 0.2126*v[0]+0.7152*v[1]+0.0722*v[2];};
  const [a,b]=[lum(fg),lum(bg)].sort((x,y)=>y-x);return [node.textContent.trim().slice(0,12),(a+0.05)/(b+0.05)];})"""


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class CredentialRetentionUITests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendHandler.request_paths = []
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.sessions, self.revokes, self.errors = [], [], []
        self.events, self.issued = [], {}
        self.revoke_status = [200]
        self.counter = 0

    def tearDown(self):
        try:
            self.browser.close()
        finally:
            self.playwright.stop()

    def alive(self, credential):
        """Mirror public_security.open_byok_credential: a successful revoke invalidates every retained (v2)
        credential issued before it; tab-scoped (v1) credentials carry no issue time and are never revoked."""
        if credential.startswith("encrypted-tab-"):
            return True
        return not any(kind == "revoke" and status == 200 for kind, status in self.events[self.issued[credential] + 1:])

    # ---------- helpers ----------
    def context(self, retention=True, snow=False, tone="light", width=1280, height=820):
        context = self.browser.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce",
                                           timezone_id="Asia/Hong_Kong", locale="zh-CN")
        if snow:
            stored = {"light": "light", "dusk": "auto", "dark": "dark"}[tone]
            context.add_init_script(f"localStorage.setItem('project-snow-public:tone',{json.dumps(stored)});"
                                    "localStorage.setItem('project-snow-public:motion','off');" + """(() => {
              const set = () => { if (!document.documentElement) return false; document.documentElement.dataset.snowTime='dusk'; return true; };
              if (!set()) { const o=new MutationObserver(() => { if (set()) o.disconnect(); }); o.observe(document,{childList:true}); } })();""")

        def config(route):
            payload = route.fetch().json()
            if snow:
                payload["features"] = {**payload.get("features", {}), **SNOW}
            if retention:
                payload["credential_policy"] = {**payload.get("credential_policy", {}), "retention": {
                    "enabled": True, "notice_version": NOTICE, "options": list(retention) if isinstance(retention, tuple) else ["2h", "24h", "3d", "7d"],
                    "storage": "localStorage"}}
            route.fulfill(json=payload)

        def session(route):
            body = route.request.post_data_json
            self.sessions.append(body)
            self.counter += 1
            choice = body.get("retention", "tab")
            credential = f"encrypted-{choice}-{self.counter}"
            self.issued[credential] = len(self.events)
            self.events.append(("session", choice))
            # Same shape as App/backend public_main.byok_session: no issued_at in the response.
            route.fulfill(json={"provider": body["provider"], "credential": credential,
                                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=HOURS[choice])).isoformat(),
                                "stored": False, **({"retention": choice} if "retention" in body else {})})

        def revoke(route):
            self.revokes.append(route.request.post_data_json)
            status = self.revoke_status.pop(0) if len(self.revoke_status) > 1 else self.revoke_status[0]
            self.events.append(("revoke", status))
            route.fulfill(status=status, json={"revoked": True} if status == 200 else {"detail": {"code": "unavailable"}})

        context.route("**/public/v1/config", config)
        context.route("**/public/v1/byok/session", session)
        context.route("**/public/v1/byok/revoke", revoke)
        return context

    def open_page(self, context, snow=False):
        page = context.new_page()
        page.set_default_timeout(10000)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.goto(self.base_url, wait_until="networkidle")
        if snow:
            page.wait_for_function("() => Boolean(document.querySelector('#snow-lock') || document.querySelector('#experience-notice-dialog')?.open)")
            if page.locator("#snow-lock").count():
                page.locator("#snow-lock").focus(); page.keyboard.press("Enter")
            page.locator("#snow-boot").wait_for(state="detached")
        if page.locator("#experience-notice-dialog[open]").count():
            page.locator("#accept-experience-notice").click()
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        return page

    @staticmethod
    def open_models(page):
        if page.viewport_size["width"] < 820:
            if page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
                page.locator("#open-contacts").click()
            page.locator("#open-settings").click()
        else:
            page.locator("#header-config-model").click()
        page.locator("#settings-dialog").wait_for(state="visible")

    @staticmethod
    def fill_model(page):
        page.locator("#api-key").fill("sk-e2e-only-not-real")
        if not page.locator("#model-id").is_visible():
            page.locator("#toggle-advanced-model").click()
        page.locator("#model-id").fill("gpt-e2e")

    @staticmethod
    def choose(page, value, confirm=True):
        page.locator("#credential-retention").select_option(value)
        if value == "tab":
            return
        dialog = page.locator(".credential-retention-modal[open]")
        dialog.wait_for(state="visible")
        if confirm:
            dialog.locator('input[type="checkbox"]').check()
            dialog.locator("[data-retention-confirm]").click()
            page.locator(".credential-retention-modal").wait_for(state="detached")

    @staticmethod
    def stored(page):
        return page.evaluate(f"() => [sessionStorage.getItem({json.dumps(KEY)}), localStorage.getItem({json.dumps(KEY)}), localStorage.getItem({json.dumps(PENDING)})]")

    def save(self, page):
        page.locator("#save-model").click()
        page.locator("#settings-dialog").wait_for(state="hidden")

    # ---------- tests ----------
    def test_field_hidden_and_request_unchanged_without_server_retention(self):
        context = self.context(retention=False)
        page = self.open_page(context)
        self.open_models(page)
        self.assertFalse(page.locator("#credential-retention-field").is_visible())
        self.fill_model(page)
        self.save(page)
        self.assertEqual(len(self.sessions), 1)
        self.assertFalse({"retention", "retention_notice_version", "accepted_retention_notice"} & set(self.sessions[0]))
        tab, local, _ = self.stored(page)
        self.assertIsNotNone(tab)
        self.assertIsNone(local)
        self.assertEqual(page.locator("#toast").inner_text(), "模型会话已保存到当前标签页")
        self.assertEqual(self.errors, [])

    def test_saving_on_this_device_needs_the_notice_and_cancel_restores_choice(self):
        context = self.context(retention=("24h", "7d"))
        page = self.open_page(context)
        self.open_models(page)
        options = page.locator("#credential-retention option").evaluate_all("nodes => nodes.map(node => node.value)")
        self.assertEqual(options, ["tab", "24h", "7d"])
        page.locator("#credential-retention").select_option("7d")
        dialog = page.locator(".credential-retention-modal[open]")
        dialog.wait_for(state="visible")
        self.assertEqual(dialog.locator("h2").inner_text(), "在本机保存 7 天")
        self.assertIn("使用这个浏览器的人都能用你的模型服务对话", dialog.inner_text())
        self.assertIn("服务器不保存你的 API Key", dialog.inner_text())
        self.assertFalse(dialog.locator("[data-retention-confirm]").is_enabled())
        self.assertEqual(dialog.locator("[data-retention-confirm]").evaluate("node => getComputedStyle(node).opacity"), "0.5")
        page.keyboard.press("Escape")
        page.locator(".credential-retention-modal").wait_for(state="detached")
        self.assertEqual(page.locator("#credential-retention").input_value(), "tab")
        self.assertTrue(page.locator("#settings-dialog").is_visible())
        self.choose(page, "24h")
        self.assertEqual(page.locator("#credential-retention").input_value(), "24h")
        # A later cancel goes back to the previous accepted choice, not to the default.
        self.choose(page, "7d", confirm=False)
        dialog.locator("[data-retention-cancel]").last.click()
        page.locator(".credential-retention-modal").wait_for(state="detached")
        self.assertEqual(page.locator("#credential-retention").input_value(), "24h")
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.errors, [])

    def test_kept_credential_survives_a_new_tab_and_clearing_revokes_it(self):
        context = self.context()
        page = self.open_page(context)
        self.open_models(page)
        self.fill_model(page)
        self.choose(page, "7d")
        self.save(page)
        body = self.sessions[-1]
        self.assertEqual((body["retention"], body["retention_notice_version"], body["accepted_retention_notice"]), ("7d", NOTICE, True))
        self.assertEqual(page.locator("#toast").inner_text(), "模型会话已保存，凭证保存在本机")
        tab, local, _ = self.stored(page)
        self.assertIsNone(tab)
        saved = json.loads(local)
        self.assertEqual((saved["credential"], saved["retention"], saved["model"]), ("encrypted-7d-1", "7d", "gpt-e2e"))
        self.assertNotIn("issuedAt", saved)

        second = self.open_page(context)
        self.open_models(second)
        self.assertIn("加密凭证已保存在本机", second.locator("#credential-status").inner_text())
        self.assertEqual(second.locator("#credential-retention").input_value(), "7d")
        self.assertFalse(second.locator("#credential-retention-hint").is_visible())
        second.locator("#clear-credential").click()
        second.locator("#toast", has_text="已清除并作废").wait_for(state="visible")
        self.assertEqual(self.revokes, [{}])
        self.assertFalse(self.alive("encrypted-7d-1"))
        self.assertEqual(self.stored(second), [None, None, None])
        # The first tab drops the same credential through the storage event.
        self.open_models(page)
        self.assertFalse(page.locator("#credential-status").is_visible())
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.errors, [])

    def test_failed_revoke_is_retried_on_next_open_and_client_errors_give_up(self):
        context = self.context()
        page = self.open_page(context)
        self.open_models(page)
        self.fill_model(page)
        self.choose(page, "3d")
        self.save(page)
        self.revoke_status = [503, 200]
        self.open_models(page)
        page.locator("#clear-credential").click()
        page.locator("#toast", has_text="下次打开页面会重试").wait_for(state="visible")
        self.assertEqual(self.stored(page), [None, None, "1"])
        page.reload(wait_until="networkidle")
        page.wait_for_function(f"() => localStorage.getItem({json.dumps(PENDING)}) === null")
        self.assertEqual(self.revokes, [{}, {}])
        self.assertFalse(self.alive("encrypted-3d-1"))

        self.revoke_status = [404]
        self.open_models(page)
        self.fill_model(page)
        self.choose(page, "2h")
        self.save(page)
        self.open_models(page)
        page.locator("#clear-credential").click()
        page.locator("#toast", has_text="它会在到期时失效").wait_for(state="visible")
        self.assertEqual(self.stored(page), [None, None, None])
        self.assertEqual(self.errors, [])

    def test_unfinished_revoke_blocks_a_new_kept_credential_but_not_a_tab_one(self):
        # The server revokes every retained credential issued before the call, so a retry that lands after a
        # new retained credential would kill it too. The page must finish the revoke first.
        context = self.context()
        page = self.open_page(context)
        self.open_models(page)
        self.fill_model(page)
        self.choose(page, "24h")
        self.save(page)
        self.revoke_status = [503]
        self.open_models(page)
        page.locator("#clear-credential").click()
        page.locator("#toast", has_text="下次打开页面会重试").wait_for(state="visible")
        self.fill_model(page)
        self.assertEqual(page.locator("#credential-retention").input_value(), "24h")
        page.locator("#save-model").click()
        page.locator("#setup-error", has_text="还没有作废成功").wait_for(state="visible")
        self.assertEqual([kind for kind, _ in self.events], ["session", "revoke", "revoke"])
        self.assertEqual(self.stored(page), [None, None, "1"])

        page.locator("#credential-retention").select_option("tab")
        self.fill_model(page)
        self.save(page)
        tab, local, pending = self.stored(page)
        self.assertEqual(json.loads(tab)["credential"], "encrypted-tab-2")
        self.assertEqual((local, pending), (None, "1"))

        self.revoke_status = [200]
        page.reload(wait_until="networkidle")
        page.wait_for_function(f"() => localStorage.getItem({json.dumps(PENDING)}) === null")
        self.assertFalse(self.alive("encrypted-24h-1"))
        self.assertTrue(self.alive("encrypted-tab-2"))
        self.open_models(page)
        self.assertIn("加密凭证有效至", page.locator("#credential-status").inner_text())
        self.assertEqual(self.errors, [])

    def test_changing_retention_reissues_with_key_and_revokes_the_replaced_credential_first(self):
        context = self.context()
        page = self.open_page(context)
        self.open_models(page)
        self.fill_model(page)
        self.choose(page, "24h")
        self.save(page)
        # 24h -> 7d: the revoke must land before the new retained credential is issued, or it would kill it.
        self.open_models(page)
        self.choose(page, "7d")
        page.locator("#credential-retention-hint").wait_for(state="visible")
        page.locator("#save-model").click()
        page.locator("#setup-error", has_text="请先输入 API Key").wait_for(state="visible")
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.revokes, [])
        self.fill_model(page)
        self.save(page)
        self.assertEqual(self.events, [("session", "24h"), ("revoke", 200), ("session", "7d")])
        self.assertEqual(json.loads(self.stored(page)[1])["credential"], "encrypted-7d-2")
        self.assertFalse(self.alive("encrypted-24h-1"))
        self.assertTrue(self.alive("encrypted-7d-2"))
        # 7d -> tab.
        self.open_models(page)
        page.locator("#credential-retention").select_option("tab")
        self.fill_model(page)
        self.save(page)
        self.assertEqual(self.sessions[-1]["retention"], "tab")
        self.assertNotIn("accepted_retention_notice", self.sessions[-1])
        tab, local, pending = self.stored(page)
        self.assertEqual(json.loads(tab)["credential"], "encrypted-tab-3")
        self.assertEqual((local, pending), (None, None))
        self.assertEqual(self.events[3:], [("revoke", 200), ("session", "tab")])
        self.assertFalse(self.alive("encrypted-7d-2"))
        self.assertEqual(self.errors, [])

    def test_expired_kept_credential_is_removed_on_start(self):
        context = self.context()
        expired = json.dumps({"credential": "encrypted-old", "provider": "openai", "model": "gpt-e2e", "expiresAt": 1_000,
                              "retention": "7d"})
        context.add_init_script(f"if (!sessionStorage.getItem('seeded')) {{ sessionStorage.setItem('seeded','1'); localStorage.setItem({json.dumps(KEY)}, {json.dumps(expired)}); }}")
        page = self.open_page(context)
        self.assertEqual(self.stored(page)[:2], [None, None])
        self.open_models(page)
        self.assertFalse(page.locator("#credential-status").is_visible())
        self.assertEqual(self.revokes, [])
        self.assertEqual(self.errors, [])

    def test_notice_is_readable_in_every_snow_tone_and_size(self):
        for tone in ("light", "dusk", "dark"):
            for width, height in ((1440, 900), (390, 844)):
                with self.subTest(tone=tone, width=width):
                    context = self.context(snow=True, tone=tone, width=width, height=height)
                    page = self.open_page(context, snow=True)
                    self.open_models(page)
                    if width < 820:
                        options = page.locator("#provider-options-mobile button").evaluate_all(
                            "nodes => nodes.map(node => [node.getAttribute('aria-checked'), getComputedStyle(node).backgroundColor])")
                        self.assertGreaterEqual(len(options), 2)
                        ratios = page.locator("#provider-options-mobile button").evaluate_all(CONTRAST)
                        self.assertEqual([item for item in ratios if item[1] < 4.5], [], ratios)
                        if tone != "light":
                            # 未选中的厂商按钮原来在深色和暮色下仍是 app.css 写死的白底。
                            self.assertNotIn("rgb(255, 255, 255)", [bg for checked, bg in options if checked != "true"])
                    field = page.locator("#credential-retention-field")
                    field.scroll_into_view_if_needed()
                    self.assertTrue(field.is_visible())
                    page.locator("#credential-retention").select_option("7d")
                    dialog = page.locator(".credential-retention-modal[open]")
                    dialog.wait_for(state="visible")
                    box = dialog.bounding_box()
                    self.assertGreaterEqual(box["x"], 0)
                    self.assertLessEqual(box["x"] + box["width"], width + 0.5)
                    self.assertLessEqual(dialog.evaluate("node => node.scrollWidth - node.clientWidth"), 1)
                    ratios = dialog.locator("h2, .notice-list p, .notice-choice, [data-retention-cancel].secondary-button").evaluate_all(CONTRAST)
                    low = [item for item in ratios if item[1] < 4.5]
                    self.assertEqual(low, [], ratios)
                    dialog.locator('input[type="checkbox"]').check()
                    confirm = dialog.locator("[data-retention-confirm]")
                    self.assertTrue(confirm.is_enabled())
                    confirm.click()
                    page.locator(".credential-retention-modal").wait_for(state="detached")
                    self.assertEqual(page.locator("#credential-retention").input_value(), "7d")
                    self.assertEqual(self.errors, [])
                    context.close()
