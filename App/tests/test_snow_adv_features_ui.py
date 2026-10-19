"""Snow ADV first-release feature UI: memory archive, backlog search/favorites, image card, round usage.

The logic lives in modules/snow-feature-logic.js and has its own tests; these drive the real page through
the dialogue fixture (no network, no model) and check what the user sees and what reaches the server.
"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from tests import test_dialogue_v2_frontend as dialogue
from tests import test_public_frontend_e2e as fixture

FEATURES = {"ui_snow_adv": True, "chat_latency_v2": True, "motion_v2": True, "dialogue_domains_v2": True,
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
  const [a,b]=[lum(fg),lum(bg)].sort((x,y)=>y-x);return [node.className||node.tagName,(a+0.05)/(b+0.05)];})"""


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvFeaturesUITests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), dialogue.DialogueHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        handler = dialogue.DialogueHandler
        handler.requests = []; handler.results = {}; handler.counter = 0
        handler.scope_rejected = False; handler.memory_enabled = True; handler.scenario = "success"
        handler.join_started = threading.Event(); handler.join_release = threading.Event()
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors, self.requested = [], []

    def tearDown(self):
        dialogue.DialogueHandler.join_release.set()
        self.browser.close(); self.playwright.stop()

    # ---------- helpers ----------
    def page(self, width=1280, height=820, tone="light", enabled=True, context=None, locale=None):
        target = context or self.browser
        options = {"locale": locale} if locale else {}
        page = target.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce", timezone_id="Asia/Hong_Kong",
                               accept_downloads=True, **options) if context is None else context.new_page()
        page.set_default_timeout(10000)
        stored = {"light": "light", "dusk": "auto", "dark": "dark"}[tone]
        page.add_init_script(f"localStorage.setItem('project-snow-public:tone',{json.dumps(stored)});"
                             "localStorage.setItem('project-snow-public:motion','off');" + """(() => {
          const set = () => { if (!document.documentElement) return false; document.documentElement.dataset.snowTime='dusk'; return true; };
          if (!set()) { const o=new MutationObserver(() => { if (set()) o.disconnect(); }); o.observe(document,{childList:true}); } })();""")
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("request", lambda request: self.requested.append(urlsplit(request.url).path))

        def config(route):
            payload = route.fetch().json()
            payload["features"] = {**FEATURES, "ui_snow_adv": enabled}
            payload["capabilities"] = {"dialogue_protocol": "public-dialogue-v2", "scene_action_protocol": "public-scene-action-v2", "recovery_only": True}
            route.fulfill(json=payload)
        page.route("**/public/v1/config", config)
        return page

    def open(self, page, enabled=True):
        page.goto(self.base_url, wait_until="networkidle")
        page.wait_for_function("() => Boolean(document.querySelector('#snow-lock') || document.querySelector('#experience-notice-dialog')?.open)")
        if page.locator("#snow-lock").count():
            page.locator("#snow-lock").focus()
            page.keyboard.press("Enter")
        page.locator("#snow-boot").wait_for(state="detached")
        page.locator("#accept-experience-notice").click()
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        if page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
            page.locator("#open-contacts").click()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        if page.viewport_size["width"] <= 820 and page.locator("#contact-panel.open").count():
            page.locator("#close-contacts").click()
        if enabled:
            page.locator("#snow-backlog-tools").wait_for(state="attached")
        return page

    def send(self, page, text="合成日常聊天"):
        before = len([r for r in dialogue.DialogueHandler.requests if r["path"].endswith("/chat/stream")])
        page.locator("#message-input").fill(text)
        page.locator("#send-message").click()
        for _ in range(100):
            if len([r for r in dialogue.DialogueHandler.requests if r["path"].endswith("/chat/stream")]) > before:
                break
            page.wait_for_timeout(50)
        page.wait_for_function("() => !document.querySelector('#send-message').disabled || document.querySelector('#message-input').value === ''")
        page.wait_for_function("""() => !document.querySelector('.timeline-typing-row') && !document.querySelector('#stage-speech.is-typing')
          && (document.querySelector('#stage-speech')?.textContent.includes('合成的有效回复')
              || [...document.querySelectorAll('#timeline article.message.assistant')].length > 0)""")
        page.wait_for_timeout(250)

    def enter_stage(self, page):
        page.locator("#go-in-person-label", has_text="观景区").wait_for(state="attached")
        page.wait_for_function("() => !document.querySelector('#go-in-person').disabled")
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.locator("#presence-arrival-loading").wait_for(state="hidden")

    def open_transcript(self, page):
        # The visible entry differs by channel and width; both call the same handler.
        page.evaluate("() => document.getElementById('open-transcript').click()")
        page.locator("#transcript-panel.open").wait_for(state="attached")
        page.wait_for_timeout(300)

    def candidates(self):
        last = [r for r in dialogue.DialogueHandler.requests if r["path"].endswith("/chat/stream")][-1]["payload"]
        return [item["memory"]["memory_id"] for item in last.get("memory_candidates", [])]

    # ---------- tests ----------
    def test_memory_archive_confirms_deletes_undoes_and_stops_offering_the_memory(self):
        page = self.open(self.page())
        self.send(page, "合成第一句")
        self.send(page, "合成第二句")
        self.enter_stage(page)
        page.locator("#stage-menu > summary").click()
        page.locator("#stage-open-memory").click()
        dialog = page.locator("#snow-memory-dialog")
        dialog.wait_for(state="visible")
        page.locator(".snow-memory-item").first.wait_for()
        self.assertEqual(page.locator("#snow-memory-title").inner_text(), "凯茜娅记得的事")
        self.assertEqual(page.locator(".snow-memory-item").count(), 3)
        chips = page.locator(".snow-memory-filters .snow-chip").all_inner_texts()
        self.assertEqual(chips[0], "全部 3")
        self.assertIn("你说过的 2", chips)
        self.assertIn("一起经历 1", chips)
        texts = page.locator(".snow-memory-text").all_inner_texts()
        # Trash asks first; cancel leaves everything in place.
        page.locator(".snow-memory-item").nth(0).locator("button.snow-round").click()
        page.locator(".snow-memory-confirm").wait_for()
        self.assertTrue(page.evaluate("() => document.activeElement.hasAttribute('data-snow-confirm-cancel')"))
        page.locator("[data-snow-confirm-cancel]").click()
        self.assertEqual(page.locator(".snow-memory-confirm").count(), 0)
        # Delete one, undo it; delete another and keep it deleted.
        page.locator(".snow-memory-item").nth(0).locator("button.snow-round").click()
        page.locator(".snow-memory-confirm .danger-button").click()
        page.locator(".snow-memory-removed").wait_for()
        page.locator(".snow-memory-removed button").click()
        page.wait_for_function("() => document.querySelectorAll('.snow-memory-item').length === 3")
        page.locator(".snow-memory-item").nth(1).locator("button.snow-round").click()
        page.locator(".snow-memory-confirm .danger-button").click()
        page.locator(".snow-memory-removed").wait_for()
        deleted = texts[1]
        page.locator("#snow-memory-dialog .snow-modal-foot .secondary-button").click()
        dialog.wait_for(state="hidden")
        page.locator("#stage-menu > summary").click()
        page.locator("#stage-open-memory").click()
        page.locator(".snow-memory-item").first.wait_for()
        self.assertEqual(page.locator(".snow-memory-item").count(), 2)
        self.assertNotIn(deleted, page.locator(".snow-memory-text").all_inner_texts())
        self.assertEqual(page.locator(".snow-memory-removed").count(), 0, "undo is only offered while the dialog stays open")
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        # The next turn does not offer the deleted memory to the server.
        record = page.evaluate("""async () => new Promise(resolve => { const open = indexedDB.open('project-snow-public');
          open.onsuccess = () => { const request = open.result.transaction('app_state').objectStore('app_state').get('ui_preferences');
            request.onsuccess = () => resolve(request.result?.deletedMemoryIds || []); }; })""")
        self.assertEqual(len(record), 1)
        self.send(page, "合成第三句")
        self.assertNotIn(record[0], self.candidates())
        self.assertEqual(self.errors, [])

    def test_backlog_search_favorites_and_image_card(self):
        page = self.open(self.page())
        self.send(page, "今天有南瓜汤吗")
        self.send(page, "合成日常聊天")
        self.open_transcript(page)
        # Every character reply in the full list gets a star; favouriting updates the tab count.
        page.locator("#transcript-content .snow-entry-fav").first.wait_for()
        replies = page.locator("#transcript-content .transcript-entry .snow-entry-fav").count()
        self.assertEqual(replies, 2)
        star = page.locator("#transcript-content .snow-entry-fav").first
        star.click()
        page.wait_for_function("() => document.querySelector('#transcript-content .snow-entry-fav').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(page.locator('[data-snow-backlog-tab="favorites"]').inner_text(), "收藏 1")
        # Search covers stored history and highlights exactly the query.
        page.locator("#snow-backlog-search").fill("南瓜汤")
        page.locator(".snow-search-hit").first.wait_for()
        self.assertTrue(page.locator("#transcript-content").evaluate("node => node.hidden"))
        self.assertEqual(page.locator(".snow-search-hit mark.snow-mark").all_inner_texts(), ["南瓜汤"])
        self.assertIn("1 处", page.locator(".snow-search-summary").inner_text())
        page.locator("#snow-backlog-search").fill("有效回复")
        page.wait_for_function("() => document.querySelectorAll('.snow-search-hit').length === 2")
        page.locator("#snow-backlog-search").fill("不存在的词")
        page.wait_for_function("() => document.querySelector('.snow-search-summary').textContent.startsWith('没有找到')")
        page.locator("#snow-backlog-search").fill("")
        page.wait_for_function("() => !document.querySelector('#transcript-content').hidden")
        # Favourites tab -> image card -> PNG download.
        page.locator('[data-snow-backlog-tab="favorites"]').click()
        page.locator(".snow-fav-card").wait_for()
        self.assertEqual(page.locator(".snow-fav-card blockquote").inner_text(), "合成的有效回复。")
        page.locator(".snow-fav-card button", has_text="生成图片卡").click()
        page.locator("#snow-card-dialog").wait_for(state="visible")
        page.wait_for_function("() => document.querySelector('#snow-card-dialog img')?.src.startsWith('data:image/png')")
        size = page.locator("#snow-card-dialog img").evaluate("img => img.decode().then(() => [img.naturalWidth, img.naturalHeight])")
        self.assertEqual(size, [1080, 1350])
        before = page.locator("#snow-card-dialog img").get_attribute("src")
        page.locator('#snow-card-dialog .snow-seg button[data-style="夜色"]').click()
        page.wait_for_function("src => document.querySelector('#snow-card-dialog img').src !== src", arg=before)
        with page.expect_download() as download:
            page.locator("#snow-card-dialog .primary-button").click()
        self.assertTrue(download.value.suggested_filename.startswith("小吉终端-凯茜娅-"))
        self.assertTrue(download.value.suggested_filename.endswith(".png"))
        page.keyboard.press("Escape")
        page.locator("#snow-card-dialog").wait_for(state="detached")
        # Unstar from the favourites tab.
        page.locator(".snow-fav-card .snow-fav").click()
        page.wait_for_function("() => document.querySelector('[data-snow-backlog-tab=\"favorites\"]').textContent === '收藏'")
        self.assertEqual(self.errors, [])
        self.assertEqual([path for path in self.requested if path.startswith("/public/") and "favorite" in path], [])

    def test_stage_star_and_round_usage_with_unknown_tokens_and_setting(self):
        page = self.open(self.page())
        self.enter_stage(page)
        self.send(page, "合成面对面")
        chip = page.locator("#snow-stage-usage")
        chip.wait_for(state="visible")
        self.assertEqual(chip.locator(".snow-usage-long").inner_text(), "本轮 1 次调用 · 10 tokens")
        chip.click()
        pop = page.locator(".snow-pop")
        pop.wait_for()
        rows = pop.locator(".snow-usage-row").all_inner_texts()
        self.assertTrue(rows[0].startswith("回复"))
        self.assertIn("10 tokens", rows[-1])
        page.keyboard.press("Escape")
        pop.wait_for(state="detached")
        self.assertEqual(page.evaluate("() => document.activeElement.id"), "snow-stage-usage")
        star = page.locator("#snow-stage-favorite")
        star.click()
        page.wait_for_function("() => document.querySelector('#snow-stage-favorite').getAttribute('aria-pressed') === 'true'")
        # A provider that reports calls but no tokens is never shown as zero tokens.
        def no_tokens(route):
            response = route.fetch()
            body = response.text().replace('"usage": {"provider_calls": 1, "total_tokens": 10}', '"usage": {"provider_calls": 1}')
            route.fulfill(status=response.status, headers=response.headers, body=body)
        page.route("**/public/v2/chat/stream", no_tokens)
        self.send(page, "合成第二句")
        page.wait_for_function("() => document.querySelector('#snow-stage-usage .snow-usage-long').textContent.includes('厂商未返回')")
        self.assertEqual(chip.locator(".snow-usage-long").inner_text(), "本轮 1 次调用 · 厂商未返回 token 数")
        page.unroute("**/public/v2/chat/stream")
        # Turning the setting off hides every usage line and survives a reload.
        page.evaluate("() => document.getElementById('stage-open-settings').click()")
        page.locator('[data-settings-tab="history"]').click()
        toggle = page.locator("#snow-usage-enabled")
        self.assertTrue(toggle.is_checked())
        self.assertIn("今天（本机统计）", page.locator("#snow-usage-today").inner_text())
        toggle.uncheck()
        page.wait_for_function("() => document.querySelector('#snow-stage-usage').hidden")
        page.keyboard.press("Escape")
        page.reload(wait_until="networkidle")
        page.locator("#snow-backlog-tools").wait_for(state="attached")
        page.wait_for_timeout(400)
        self.assertEqual(page.locator("#snow-stage-usage:not([hidden]), .snow-message-usage:not([hidden])").count(), 0)
        self.assertEqual(self.errors, [])

    def test_new_surfaces_are_readable_in_every_tone(self):
        for tone in ("light", "dusk", "dark"):
            with self.subTest(tone=tone):
                page = self.open(self.page(tone=tone))
                self.enter_stage(page)
                self.send(page, "合成对比度")
                ratios = page.locator("#snow-stage-usage .snow-usage-long").evaluate_all(CONTRAST)
                page.locator("#stage-menu > summary").click()
                page.locator("#stage-open-memory").click()
                page.locator(".snow-memory-item").first.wait_for()
                ratios += page.locator("#snow-memory-dialog :is(.snow-memory-text, .snow-kind, .snow-memory-meta > span:not(.snow-kind), .snow-memory-day, h2, header p, .snow-modal-foot > span, .snow-chip)").evaluate_all(CONTRAST)
                page.keyboard.press("Escape")
                self.open_transcript(page)
                page.locator("#snow-backlog-search").fill("合成")
                page.locator(".snow-search-hit").first.wait_for()
                ratios += page.locator("#transcript-panel :is(.snow-search input, .snow-search-summary, .snow-mark, .snow-seg button)").evaluate_all(CONTRAST)
                low = [item for item in ratios if item[1] < 4.5]
                self.assertTrue(ratios)
                self.assertEqual(low, [], low)
                page.close()
        self.assertEqual(self.errors, [])

    def test_feature_rows_do_not_crowd_existing_layout_on_mobile(self):
        page = self.open(self.page(width=390, height=844, locale="zh-CN"))
        self.send(page, "合成日常聊天")
        # The round usage replaces the old per-reply usage line instead of repeating it.
        page.locator("#timeline .snow-message-usage:not([hidden])").first.wait_for()
        self.assertEqual(page.locator("#timeline article.message:has(.snow-message-usage) .message-usage").evaluate_all(
            "nodes => nodes.filter(node => node.getClientRects().length).length"), 0)
        # Backlog stars sit beside the body text, below the header row, so the header keeps its full width;
        # no star covers a glyph.
        self.open_transcript(page)
        page.locator("#transcript-content .snow-entry-fav").first.wait_for()
        entries = page.locator("#transcript-content .transcript-entry").evaluate_all("""entries => entries.map(entry => {
          const star = entry.querySelector(':scope > .snow-entry-fav')?.getBoundingClientRect();
          const header = entry.querySelector(':scope > strong').getBoundingClientRect();
          const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
          const covered = Boolean(star) && [...entry.querySelectorAll(':scope > :not(.snow-entry-fav)')].some(node => {
            const range = document.createRange(); range.selectNodeContents(node);
            return [...range.getClientRects()].some(rect => hit(rect, star));
          });
          return {starred: Boolean(star), covered, besideHeader: Boolean(star) && star.top < header.bottom};
        })""")
        self.assertTrue(any(entry["starred"] for entry in entries), entries)
        self.assertEqual([entry for entry in entries if entry["besideHeader"] or entry["covered"]], [], entries)
        page.keyboard.press("Escape")
        # The star and usage chip do not make the in-person name row taller, so the dialogue keeps its text lines.
        self.enter_stage(page)
        self.send(page, "合成面对面")
        page.locator("#snow-stage-usage").wait_for(state="visible")
        measure = "() => document.querySelector('#stage-dialogue .stage-speaker-line').getBoundingClientRect().height"
        with_tools = page.evaluate(measure)
        page.evaluate("() => { document.querySelector('.snow-line-tools').style.display = 'none'; }")
        self.assertAlmostEqual(with_tools, page.evaluate(measure), delta=0.5)
        self.assertEqual(self.errors, [])

    def test_flag_off_never_loads_the_feature_modules(self):
        page = self.open(self.page(enabled=False), enabled=False)
        page.wait_for_timeout(800)
        self.assertEqual([path for path in self.requested if path.startswith("/modules/snow-feature") or path == "/modules/snow-features.js"], [])
        self.assertEqual(page.locator("#snow-backlog-tools, #snow-memory-dialog, .snow-line-tools, #snow-usage-setting").count(), 0)
        self.assertEqual(self.errors, [])
