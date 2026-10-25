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
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from tests import test_dialogue_v2_frontend as dialogue
from tests import test_public_frontend_e2e as fixture

FEATURES = {"ui_snow_adv": True, "chat_latency_v2": True, "motion_v2": True, "dialogue_domains_v2": True,
            "scene_actions_v2": True, "session_memory_v2": True, "verified_block_replay": True}
# 第二批分页用的长台词：句号、逗号都有，足够在电脑上分 2 页以上、手机上分更多页。
LONG_SPEECH = ("今天的雪下得很安静，基地外面的灯一盏一盏亮起来。我刚从资料室出来，顺路给你带了一杯热可可，还是你喜欢的那种不太甜的。"
               "你看，观景区那边的玻璃上结了一层薄薄的霜，用手指划一下就能写字。上次你说想看极光，我查了一下，今晚的云层好像会散开一点。"
               "如果你不累的话，我们晚点一起去看看吧？就算看不到也没关系，坐在那里聊聊天也很好。对了，食堂今天有南瓜汤，我先帮你留了一份。"
               "其实我一直想跟你说，这些天有你在身边，感觉连值班的夜晚都没那么长了。"
               "明天早上训练区要检修，我们可以晚一点起床，先去食堂吃早饭，再慢慢走去资料室整理那批旧档案。"
               "要是路上又下雪，我就把伞撑过去一点，你别又把自己那一半让给我，上次你肩膀都湿透了。")
LONG_ACTION = "她把围巾往上拉了拉，侧过头看你。"
# 动作描写多到会把台词挤出对白框（小B 10-24 的截图）：三句动作 + 两句台词。
MANY_ACTIONS = ("伊切尔仰起脸，眼睛弯成两道小月牙，认真地把这个回礼准备好。伊切尔踮起脚，凑近，在分析员的额头上轻轻碰了一下。"
                "然后她像是做了什么了不起的大事一样，背着手往后退了两步，耳朵尖一点点红了起来，还假装若无其事地看向窗外的雪。")
SHORT_SPEECH = ("嘿嘿，分析员真的低头啦——那伊切尔就不客气咯。这可是回礼哦，不许说不算数！下次你要是再帮我把训练器材搬回仓库，我还会这样谢你的。"
                "明天早上训练区要检修，我们可以晚一点起床，先去食堂吃早饭。")
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
    def page(self, width=1280, height=820, tone="light", enabled=True, context=None, locale=None, proactive=False, retention=False):
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
            if proactive:
                payload["proactive_messages"] = {"enabled": True, "notice_version": "proactive-cost-1"}
            if retention:
                payload["credential_policy"] = {**payload.get("credential_policy", {}), "retention": {
                    "enabled": True, "notice_version": "credential-retention-1", "options": ["2h", "24h", "3d", "7d"], "storage": "localStorage"}}
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

    def long_reply(self, page, speech=LONG_SPEECH, action=LONG_ACTION):
        """Rewrite the fixture's one-line reply into an action + long speech (content is not signed)."""
        old = json.dumps({"type": "speech", "text": "合成的有效回复。"}, ensure_ascii=False)
        blocks = ([{"type": "action", "text": action}] if action else []) + [{"type": "speech", "text": speech}]
        new = json.dumps(blocks, ensure_ascii=False)[1:-1]

        def handler(route):
            response = route.fetch()
            headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
            route.fulfill(status=response.status, headers=headers, body=response.text().replace(old, new))
        page.route("**/public/v2/chat/stream", handler)

    def send_long(self, page, text="合成长台词"):
        page.locator("#message-input").fill(text)
        page.locator("#send-message").click()
        page.locator(".snow-page-meter:not(.is-single)").wait_for(state="attached")
        page.wait_for_function(self.READY)

    PAGE_STATE = """() => {
      const speech = document.querySelector('#stage-speech'), page = speech.querySelector(':scope > .snow-page');
      const text = page?.lastChild, box = speech.getBoundingClientRect();
      const range = document.createRange(); if (text) range.selectNodeContents(text);
      const lines = text ? [...range.getClientRects()].filter(rect => rect.width > 0.5) : [];
      const blockers = ['#stage-quick-menu', '#stage-advance', '.snow-page-meter:not(.is-single)']
        .map(selector => document.querySelector(selector)).filter(node => node && node.getClientRects().length)
        .map(node => node.getBoundingClientRect());
      const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
      const narration = document.querySelector('#stage-narration');
      const outside = lines.filter(line => line.bottom > box.bottom + 0.5 || line.top < box.top - 0.5 || line.right > box.right + 0.5);
      return { text: text?.data || '', meter: document.querySelector('.snow-page-meter').textContent,
        ready: !speech.classList.contains('is-revealing'), lines: lines.length,
        covered: lines.filter(line => blockers.some(rect => hit(line, rect))).length, outside: outside.length,
        where: outside.map(line => [Math.round(line.right - box.right), Math.round(line.bottom - box.bottom)]),
        narration: Boolean(narration?.textContent && narration.getClientRects().length),
        lead: Boolean(page?.classList.contains('is-lead')),
        clipped: Boolean(narration?.getClientRects().length && narration.scrollHeight > narration.clientHeight + 1),
        prev: Boolean(document.querySelector('.snow-page-prev')?.checkVisibility?.({ visibilityProperty: true })) };
    }"""
    READY = "() => { const s = document.querySelector('#stage-speech'); return s.classList.contains('snow-paged') && !s.classList.contains('is-revealing'); }"

    def read_pages(self, page, keyboard_from=None):
        """Turn every page by clicking the dialogue (or pressing Enter from page `keyboard_from`) and return each page's state."""
        pages = [page.evaluate(self.PAGE_STATE)]
        total = int(pages[0]["meter"].split("/")[1].split("·")[0])
        for index in range(1, total):
            previous = pages[-1]["meter"]
            if keyboard_from is not None and index >= keyboard_from:
                page.locator("#stage-dialogue").focus()
                page.keyboard.press("Enter")
            else:
                page.locator("#stage-speech").click(position={"x": 24, "y": 18})
            try:
                page.wait_for_function("value => document.querySelector('.snow-page-meter').textContent !== value", arg=previous)
            except Exception as error:
                raise AssertionError({"index": index, "state": page.evaluate(self.PAGE_STATE),
                                      "focus": page.evaluate("() => document.activeElement?.id || document.activeElement?.tagName")}) from error
            page.wait_for_function(self.READY)
            pages.append(page.evaluate(self.PAGE_STATE))
        return pages

    def idb_preferences(self, page):
        return page.evaluate("""async () => new Promise(resolve => { const open = indexedDB.open('project-snow-public');
          open.onsuccess = () => { const request = open.result.transaction('app_state').objectStore('app_state').get('ui_preferences');
            request.onsuccess = () => resolve(request.result || {}); }; })""")

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
        page.locator('[data-settings-tab="conversation"]').click()
        toggle = page.locator("#snow-usage-enabled")
        # 第二批起「显示每轮用量」放在「对话」里，「记录」里不再重复。
        self.assertEqual(page.locator("#settings-panel-history #snow-usage-setting").count(), 0)
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

    # ---------- 第二批：分页、自动、提醒、设置「对话」 ----------
    def test_long_line_pages_fit_beside_the_quick_menu_and_turn_in_order(self):
        for width, height in ((1440, 900), (390, 844)):
            with self.subTest(width=width):
                page = self.open(self.page(width=width, height=height, locale="zh-CN"))
                self.enter_stage(page)
                self.long_reply(page)
                self.send_long(page)
                pages = self.read_pages(page, keyboard_from=2)
                self.assertGreaterEqual(len(pages), 2, pages)
                total = len(pages)
                self.assertEqual([state["meter"].split(" · ")[0] for state in pages], [f"{index} / {total}" for index in range(1, total + 1)])
                # Every page is fully shown, stays inside the speech box and never runs under the menu, ▼ or page count.
                self.assertEqual([(state["covered"], state["outside"]) for state in pages], [(0, 0)] * total,
                                 [(state["meter"], state["where"], state["text"][-12:]) for state in pages if state["covered"] or state["outside"]])
                self.assertTrue(all(state["ready"] and state["lines"] >= 1 for state in pages), pages)
                # The pages cover the whole line in order; only breaks between pages may drop whitespace.
                self.assertEqual("".join(state["text"] for state in pages).replace("\n", ""), LONG_SPEECH)
                # Pages break after punctuation where they can.
                self.assertTrue(all(state["text"].rstrip()[-1] in "。！？，、；：…" for state in pages[:-1]), [state["text"][-6:] for state in pages])
                # The action line shows with the first page and folds away on later pages.
                self.assertEqual([state["narration"] for state in pages], [True] + [False] * (total - 1))
                # The last page cannot be advanced past. Pages read for the first time are not labelled 已读,
                # even once they have been shown in full; that label is for pages read before this showing.
                page.locator("#stage-speech").click(position={"x": 24, "y": 18})
                page.wait_for_timeout(200)
                self.assertEqual(page.evaluate(self.PAGE_STATE)["meter"], f"{total} / {total}")
                self.assertEqual([state["meter"] for state in pages], [f"{index} / {total}" for index in range(1, total + 1)])
                if width == 390:
                    # Approved: on phones the dialogue is about 60px taller while paging is available.
                    taller, normal = page.evaluate("""() => { const surface = document.querySelector('#in-person-surface'), box = document.querySelector('#stage-dialogue');
                      const a = box.getBoundingClientRect().height; surface.removeAttribute('data-snow-pager');
                      const b = box.getBoundingClientRect().height; surface.setAttribute('data-snow-pager', '1'); return [a, b]; }""")
                    self.assertTrue(50 <= taller - normal <= 70, (taller, normal))
                page.close()
        self.assertEqual(self.errors, [])

    def test_long_action_gets_its_own_page_instead_of_squeezing_the_line(self):
        # 小B reported: several action sentences took the box, leaving the line cut off under the quick menu
        # (desktop) or the action itself hidden behind the narration's own scrollbar (phone).
        for width, height in ((1440, 900), (390, 844)):
            with self.subTest(width=width):
                page = self.open(self.page(width=width, height=height, locale="zh-CN"))
                self.enter_stage(page)
                self.long_reply(page, speech=SHORT_SPEECH, action=MANY_ACTIONS)
                self.send_long(page)
                pages = self.read_pages(page)
                leads = [state for state in pages if state["lead"]]
                lines = [state for state in pages if not state["lead"]]
                self.assertTrue(leads and pages[:len(leads)] == leads, [(state["meter"], state["lead"]) for state in pages])
                # The action pages carry the whole action in order; the line then starts on a full box.
                self.assertEqual("".join(state["text"] for state in leads).replace("\n", ""), MANY_ACTIONS)
                self.assertEqual("".join(state["text"] for state in lines).replace("\n", ""), SHORT_SPEECH)
                self.assertTrue(all(not state["narration"] for state in pages), pages)
                self.assertEqual([(state["covered"], state["outside"]) for state in pages], [(0, 0)] * len(pages),
                                 [(state["meter"], state["where"], state["text"][-12:]) for state in pages if state["covered"] or state["outside"]])
                # A line page that shares the box with nothing shows at least two lines unless the line is shorter.
                self.assertGreaterEqual(lines[0]["lines"], 2, lines[0])
                page.close()
        # A one-line action still shares the first page with the line, as before.
        page = self.open(self.page(width=1440, height=900, locale="zh-CN"))
        self.enter_stage(page)
        self.long_reply(page)
        self.send_long(page)
        state = page.evaluate(self.PAGE_STATE)
        self.assertEqual((state["lead"], state["narration"], state["clipped"]), (False, True, False), state)
        self.assertEqual(self.errors, [])

    def test_previous_page_button_and_keys_return_to_the_page_before(self):
        page = self.open(self.page(width=1440, height=900, locale="zh-CN"))
        self.enter_stage(page)
        self.long_reply(page, action=MANY_ACTIONS)
        self.send_long(page)
        first = page.evaluate(self.PAGE_STATE)
        self.assertTrue(first["lead"], first)
        self.assertFalse(first["prev"], "no previous page on page 1")
        pages = self.read_pages(page)
        total = len(pages)
        self.assertGreaterEqual(total, 3)
        # 「上一页」 goes back one page and shows it whole at once (it has been read).
        page.locator(".snow-page-prev").click()
        page.wait_for_function(self.READY)
        back = page.evaluate(self.PAGE_STATE)
        self.assertEqual(back["meter"].split(" · ")[0], f"{total - 1} / {total}")
        self.assertEqual(back["text"], pages[-2]["text"])
        self.assertTrue(back["ready"] and back["prev"], back)
        # ← and PageUp go back too, down to the action page; → returns without retyping.
        page.locator("#stage-dialogue").focus()
        for _ in range(total):
            page.keyboard.press("ArrowLeft" if _ % 2 == 0 else "PageUp")
            page.wait_for_timeout(60)
        state = page.evaluate(self.PAGE_STATE)
        self.assertEqual((state["meter"].split(" · ")[0], state["lead"], state["text"]), (f"1 / {total}", True, pages[0]["text"]))
        page.keyboard.press("ArrowRight")
        page.wait_for_function("() => document.querySelector('.snow-page-meter').textContent.startsWith('2 /')")
        self.assertEqual(page.evaluate(self.PAGE_STATE)["text"], pages[1]["text"])
        self.assertTrue(page.evaluate(self.PAGE_STATE)["ready"])
        # Clicking 「上一页」 never also turns the page forward.
        page.locator(".snow-page-prev").click()
        page.wait_for_timeout(200)
        self.assertTrue(page.evaluate(self.PAGE_STATE)["meter"].startswith("1 /"))
        self.assertEqual(self.errors, [])

    def test_time_of_day_change_crossfades_the_whole_page(self):
        def shifted(motion, reduced):
            page = self.page(width=1280, height=760, tone="dusk")
            if not reduced:
                page.emulate_media(reduced_motion="no-preference")
            page.add_init_script(f"localStorage.setItem('project-snow-public:motion',{json.dumps(motion)});" + """(() => {
              const set = () => { if (!document.documentElement) return false; document.documentElement.dataset.snowTime='day'; return true; };
              if (!set()) { const o=new MutationObserver(() => { if (set()) o.disconnect(); }); o.observe(document,{childList:true}); } })();""")
            self.open(page)
            self.enter_stage(page)
            page.wait_for_function("() => document.querySelector('link[href*=\"snow-scenes\"]')")
            page.wait_for_timeout(2500)
            before = page.evaluate("() => [document.documentElement.dataset.tone, document.querySelector('#scene-stage .snow-layer-far').dataset.time]")
            page.evaluate("""() => { window.__shifts = []; const root = document.documentElement;
              new MutationObserver(() => window.__shifts.push([root.dataset.snowTimeShift || '', performance.now()]))
                .observe(root, { attributeFilter: ['data-snow-time-shift'] });
              root.dataset.snowTime = 'dusk'; }""")
            page.wait_for_timeout(2600)
            after = page.evaluate("() => [document.documentElement.dataset.tone, document.querySelector('#scene-stage .snow-layer-far').dataset.time, document.documentElement.dataset.snowTimeShift || '']")
            rows = page.evaluate("() => window.__shifts")
            page.close()
            # The marker stays on <html> for exactly as long as the view transition runs.
            shifts = [value for value, _ in rows]
            durations = [round(rows[1][1] - rows[0][1])] if len(rows) == 2 else []
            return before, durations, after, shifts

        before, durations, after, shifts = shifted("auto", reduced=False)
        self.assertEqual(before, ["light", "day"])
        # Dusk arrives within one view transition: stage backdrop and tone change together, over 1.6s, then the marker clears.
        self.assertEqual(after, ["dark", "dusk", ""])
        self.assertEqual(shifts, ["1", ""])
        self.assertTrue(durations and 1500 <= durations[0] <= 2300, durations)
        # Reduced motion (the app then runs with motion off) and motion off switch at once, as scene changes do.
        for motion, reduced in (("auto", True), ("off", False)):
            with self.subTest(motion=motion, reduced=reduced):
                _, durations, after, shifts = shifted(motion, reduced=reduced)
                self.assertEqual((after, shifts, durations), (["dark", "dusk", ""], [], []))
        self.assertEqual(self.errors, [])

    def test_restored_line_after_reload_is_paged_and_pages_read_before_say_so(self):
        page = self.open(self.page(width=1440, height=900, locale="zh-CN"))
        self.enter_stage(page)
        self.long_reply(page)
        self.send_long(page)
        total = len(self.read_pages(page))
        # The pager loads after the first stage render of a reload; once registered it takes over the line already on stage,
        # so a long restored line is paged instead of running under the quick menu.
        page.reload(wait_until="networkidle")
        page.locator("#snow-backlog-tools").wait_for(state="attached")
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.wait_for_function(self.READY)
        state = page.evaluate(self.PAGE_STATE)
        self.assertEqual(state["meter"], f"1 / {total} · 已读")
        self.assertEqual((state["covered"], state["outside"]), (0, 0), state)
        self.assertTrue(LONG_SPEECH.startswith(state["text"].strip()), state["text"])
        self.assertEqual(page.locator("#snow-stage-favorite").get_attribute("aria-label"), "收藏这一页台词")
        self.assertEqual(self.errors, [])

    def test_auto_turns_pages_and_the_star_saves_the_page_being_read(self):
        page = self.open(self.page(width=1440, height=900, locale="zh-CN"))
        page.evaluate("() => localStorage.setItem('project-snow-public:reading:v1', JSON.stringify({autoDelay: 'short'}))")
        page.reload(wait_until="networkidle")
        page.locator("#snow-backlog-tools").wait_for(state="attached")
        self.enter_stage(page)
        self.long_reply(page)
        self.send_long(page)
        page.locator("#stage-speech").click(position={"x": 24, "y": 18})
        page.wait_for_function("() => document.querySelector('.snow-page-meter').textContent.startsWith('2 /')")
        page.wait_for_function(self.READY)
        second = page.evaluate(self.PAGE_STATE)["text"].strip()
        star = page.locator("#snow-stage-favorite")
        self.assertEqual(star.get_attribute("aria-label"), "收藏这一页台词")
        star.click()
        page.wait_for_function("() => document.querySelector('#snow-stage-favorite').getAttribute('aria-pressed') === 'true'")
        saved = self.idb_preferences(page)["favoriteLines"]
        self.assertEqual([item["text"] for item in saved], [second])
        # 「自动」排在快捷菜单最前；开启后按停留时间自动翻到下一页，新的一页星标是空的。
        auto = page.locator('#stage-quick-menu [data-stage-quick="auto"]')
        self.assertEqual(page.locator("#stage-quick-menu button").first.get_attribute("data-stage-quick"), "auto")
        self.assertEqual(auto.get_attribute("aria-pressed"), "false")
        auto.click()
        self.assertEqual(auto.get_attribute("aria-pressed"), "true")
        page.locator(".snow-auto-progress:not([hidden])").wait_for(state="attached")
        page.wait_for_function("() => document.querySelector('.snow-page-meter').textContent.startsWith('3 /')", timeout=15000)
        self.assertEqual(page.locator("#snow-stage-favorite").get_attribute("aria-pressed"), "false")
        auto.click()
        self.assertEqual(auto.get_attribute("aria-pressed"), "false")
        self.assertTrue(page.locator(".snow-auto-progress").evaluate("node => node.hidden"))
        self.assertEqual(self.errors, [])

    def set_tone(self, page, value):
        page.evaluate("""value => { const select = document.querySelector('#tone-preference'); select.value = value;
          select.dispatchEvent(new Event('change', { bubbles: true })); }""", value)
        page.wait_for_function("value => document.documentElement.dataset.tone === value", arg="light" if value == "light" else "dark")
        page.wait_for_timeout(150)

    def test_cost_dialog_footer_keeps_its_buttons_together_on_a_phone(self):
        # Candidate shots at 390px showed the two buttons stacked and the hint squeezed into a narrow column.
        for width, height in ((1440, 900), (390, 844), (360, 740)):
            with self.subTest(width=width):
                page = self.open(self.page(width=width, height=height, locale="zh-CN", proactive=True))
                page.evaluate("() => document.getElementById('open-settings').click()")
                page.locator('[data-settings-tab="conversation"]').click()
                page.locator("#snow-proactive-enabled").click()
                dialog = page.locator("#snow-proactive-dialog")
                dialog.wait_for(state="visible")
                foot = dialog.locator(".snow-modal-foot").evaluate("""foot => {
                  const box = node => node.getBoundingClientRect(), d = box(foot.closest('dialog'));
                  const buttons = [...foot.querySelectorAll('.modal-actions button')].map(box);
                  const range = document.createRange(); range.selectNodeContents(foot.querySelector(':scope > span'));
                  const lines = new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
                  return {rows: new Set(buttons.map(rect => Math.round(rect.top))).size, hintLines: lines,
                          inside: buttons.every(rect => rect.left >= d.left && rect.right <= d.right)};
                }""")
                self.assertEqual(foot, {"rows": 1, "hintLines": 1, "inside": True}, (width, foot))
                page.close()
        self.assertEqual(self.errors, [])

    def test_invitation_reminder_lines_up_with_the_dialog_and_fits_a_phone(self):
        # Candidate shots in Windows Chrome showed the reminder panel reaching the dialog edge, and at 390px the
        # fourth option dropping alone onto a second row inside a stretched pill.
        measure = """() => {
          const pick = document.querySelector('#snow-movement-reminder');
          pick.scrollIntoView({block: 'center'});
          const box = node => node.getBoundingClientRect();
          const p = box(pick), preview = box(document.querySelector('#movement-form .movement-preview'));
          const seg = pick.querySelector('.snow-seg'), s = box(seg);
          const buttons = [...seg.querySelectorAll('button')].map(button => ({rect: box(button), clipped: button.scrollWidth > button.clientWidth + 1}));
          const rows = {};
          for (const {rect} of buttons) rows[Math.round(rect.top)] = (rows[Math.round(rect.top)] || 0) + 1;
          return {
            left: p.left - preview.left, right: p.right - preview.right,
            segInside: s.left >= p.left - 0.5 && s.right <= p.right + 0.5,
            buttonsInside: buttons.every(({rect}) => rect.left >= s.left - 0.5 && rect.right <= s.right + 0.5 && rect.bottom <= s.bottom + 0.5),
            clipped: buttons.filter(item => item.clipped).length,
            rows: Object.values(rows),
          };
        }"""
        for width, height in ((1440, 900), (390, 844), (360, 740)):
            with self.subTest(width=width):
                page = self.open(self.page(width=width, height=height, locale="zh-CN"))
                page.evaluate("() => document.getElementById('open-movement-shortcuts').click()")
                page.locator("#movement-dialog").wait_for(state="visible")
                page.locator("#snow-movement-reminder").wait_for(state="visible")
                for stress in (False, True):
                    if stress:
                        # Wider glyphs than this machine's fonts, like CI's Linux fonts or other Windows fallbacks.
                        page.add_style_tag(content="#snow-movement-reminder .snow-seg button { letter-spacing: 0.08em; }")
                    result = page.evaluate(measure)
                    label = (width, stress, result)
                    self.assertLessEqual(abs(result["left"]), 1, label)
                    self.assertLessEqual(abs(result["right"]), 1, label)
                    self.assertTrue(result["segInside"], label)
                    self.assertTrue(result["buttonsInside"], label)
                    self.assertEqual(result["clipped"], 0, label)
                    # Options fill whole rows: four in one row, or two by two, never three plus one.
                    self.assertEqual(len(set(result["rows"])), 1, label)
                page.close()
        self.assertEqual(self.errors, [])

    def test_invitation_reminder_binds_when_she_agrees_and_rings_without_a_model_call(self):
        hk = timezone(timedelta(hours=8))
        now = datetime.now(hk)
        # Reminders are same-day only; the signed fixture state follows the real clock, so the fake clock starts at real time.
        if now.hour == 23 and now.minute >= 40:
            self.skipTest("too close to Hong Kong midnight for a same-day reminder")
        clock = lambda moment: moment.strftime("%H:%M")  # noqa: E731
        page = self.page(width=1280, height=820, locale="zh-CN")
        page.clock.install(time=now)
        self.open(page)
        page.evaluate("() => document.getElementById('open-movement-shortcuts').click()")
        page.locator("#movement-dialog").wait_for(state="visible")
        pick = page.locator("#snow-movement-reminder")
        self.assertTrue(pick.is_visible())
        self.assertEqual(pick.locator('[aria-checked="true"]').inner_text(), "不提醒")
        option = page.locator("#movement-options [data-movement-id]").first
        place = option.get_attribute("data-movement-name")
        option.click()
        pick.locator('[data-snow-remind="custom"]').click()
        current = datetime.fromtimestamp(page.evaluate("() => Date.now()") / 1000, hk)
        pick.locator('input[type="time"]').fill(clock(current))
        self.assertIn("晚于现在", pick.locator(".snow-remind-error").inner_text())
        target = clock(current + timedelta(minutes=3))
        pick.locator('input[type="time"]').fill(target)
        self.assertTrue(pick.locator(".snow-remind-error").is_hidden())
        page.locator("#send-movement-invitation").click()
        page.locator("#movement-dialog").wait_for(state="hidden")
        # The reminder is planned with the invitation's own request id and binds once she is waiting.
        button = page.locator("#current-rendezvous .snow-remind-button")
        button.wait_for()
        page.wait_for_function("text => document.querySelector('#current-rendezvous .snow-remind-button').textContent === text", arg=f"提醒 {target}")
        chats = len([row for row in dialogue.DialogueHandler.requests if row["path"].endswith("/chat/stream")])
        request_id = [row for row in dialogue.DialogueHandler.requests if row["path"].endswith("/chat/stream")][-1]["payload"]["chat"]["request_id"]
        stored = self.idb_preferences(page)["rendezvousReminders"]
        self.assertEqual([(row["pendingRequestId"], row["rendezvousId"]) for row in stored], [(request_id, f"{request_id}:waiting")])
        # Due: a card at the top of the communicator, readable in every tone.
        page.clock.fast_forward("04:00")
        card = page.locator("#snow-reminder-cards .snow-reminder")
        card.wait_for()
        # Its own row between the rendezvous bar and the timeline (auto placement used to drop it under the timeline,
        # beneath the floating feedback button), so nothing else sits on its buttons.
        box = page.evaluate("""() => { const rect = id => document.getElementById(id).getBoundingClientRect();
          const card = rect('snow-reminder-cards'), bar = rect('current-rendezvous'), timeline = rect('timeline');
          const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
          const others = [...document.querySelectorAll('#floating-feedback, #open-global-feedback')]
            .filter(node => node.getClientRects().length).map(node => node.getBoundingClientRect());
          return { below: card.top >= bar.bottom - 0.5, above: card.bottom <= timeline.top + 0.5, clear: !others.some(other => hit(other, card)) }; }""")
        self.assertEqual(box, {"below": True, "above": True, "clear": True})
        self.assertEqual(card.locator("strong").inner_text(), f"约定提醒 · {place}")
        self.assertIn(target, card.locator("p").inner_text())
        self.assertEqual(button.inner_text(), "提醒已到")
        for tone in ("light", "dark", "auto"):
            self.set_tone(page, tone)
            ratios = page.locator("#snow-reminder-cards :is(strong, p, button), #current-rendezvous .snow-remind-button").evaluate_all(CONTRAST)
            self.assertEqual([item for item in ratios if item[1] < 4.5], [], (tone, ratios))
        self.set_tone(page, "light")
        # Snooze moves it ten minutes; close ends it.
        card.locator("button", has_text="10 分钟后再提醒").click()
        card.wait_for(state="detached")
        snoozed = clock(datetime.fromtimestamp(page.evaluate("() => Date.now()") / 1000, hk) + timedelta(minutes=10))
        page.wait_for_function("""texts => texts.includes(document.querySelector('#current-rendezvous .snow-remind-button').textContent)""",
                               arg=[f"提醒 {snoozed}", f"提醒 {clock(datetime.strptime(snoozed, '%H:%M') - timedelta(minutes=1))}"])
        page.clock.fast_forward("10:30")
        card.wait_for()
        card.locator('button[aria-label^="关闭"]').click()
        card.wait_for(state="detached")
        page.wait_for_function("() => document.querySelector('#current-rendezvous .snow-remind-button').textContent === '到点提醒'")
        # Reminders never call the model.
        self.assertEqual(len([row for row in dialogue.DialogueHandler.requests if row["path"].endswith("/chat/stream")]), chats)
        self.assertEqual([path for path in self.requested if "/rendezvous/" in path], [])
        self.assertEqual(self.errors, [])

    def test_proactive_consent_follows_a_credential_kept_on_this_device(self):
        # app.js hands the credential's retention and matching storage to the proactive client (handoff 13.3 item 6):
        # a credential kept on this device keeps the cost confirmation for a new tab; clearing removes both.
        context = self.browser.new_context(viewport={"width": 1280, "height": 820}, reduced_motion="reduce",
                                           timezone_id="Asia/Hong_Kong", locale="zh-CN")
        consent, sessions = [], []
        auth = "project-snow-public:proactive-consent:v1"

        def session(route):
            body = route.request.post_data_json
            sessions.append(body.get("retention", "tab"))
            route.fulfill(json={"provider": body["provider"], "credential": f"synthetic-{body.get('retention', 'tab')}-{len(sessions)}",
                                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
                                "stored": False, **({"retention": body["retention"]} if "retention" in body else {})})

        def grant(route):
            body = route.request.post_data_json
            consent.append(body)
            route.fulfill(json={"consent_token": "synthetic-consent-only", "notice_version": "proactive-cost-1",
                                "model_session_id": body["model_session_id"],
                                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        context.route("**/public/v1/byok/session", session)
        context.route("**/public/v1/byok/revoke", lambda route: route.fulfill(json={"revoked": True}))
        context.route("**/public/v2/rendezvous/consent", grant)
        stores = f"() => [sessionStorage.getItem({json.dumps(auth)}), localStorage.getItem({json.dumps(auth)})]"

        page = self.open(self.page(context=context, proactive=True, retention=True))
        page.evaluate("() => document.getElementById('open-settings').click()")
        page.locator('[data-settings-tab="models"]').click()
        page.locator("#credential-retention").select_option("7d")
        notice = page.locator(".credential-retention-modal[open]")
        notice.locator('input[type="checkbox"]').check()
        notice.locator("[data-retention-confirm]").click()
        page.locator("#api-key").fill("sk-e2e-only-not-real")
        if not page.locator("#model-id").is_visible():
            page.locator("#toggle-advanced-model").click()
        page.locator("#model-id").fill("gpt-e2e")
        page.locator("#save-model").click()
        page.locator("#settings-dialog").wait_for(state="hidden")
        self.assertEqual(sessions, ["tab", "7d"])
        page.evaluate("() => document.getElementById('open-settings').click()")
        page.locator('[data-settings-tab="conversation"]').click()
        page.wait_for_function("() => !document.querySelector('#snow-proactive-enabled').disabled")
        page.locator("#snow-proactive-enabled").click()
        page.locator("#snow-proactive-dialog button", has_text="我知道会产生费用，开启").click()
        page.wait_for_function("() => document.querySelector('#snow-proactive-enabled').checked")
        page.wait_for_function(f"() => localStorage.getItem({json.dumps(auth)}) !== null")
        self.assertIsNone(page.evaluate(stores)[0])
        self.assertEqual(len(consent), 1)
        # Close the tab, as a user would, so the new tab becomes the writer that may send proactive messages.
        page.close()

        second = self.page(context=context, proactive=True, retention=True)
        second.goto(self.base_url, wait_until="networkidle")
        second.wait_for_function("() => Boolean(document.querySelector('#snow-lock')) || !document.querySelector('#snow-boot')")
        if second.locator("#snow-lock").count():
            second.locator("#snow-lock").focus(); second.keyboard.press("Enter")
        second.locator("#snow-boot").wait_for(state="detached")
        if second.locator("#skip-onboarding").is_visible():
            second.locator("#skip-onboarding").click()
        second.locator("#snow-backlog-tools").wait_for(state="attached")
        second.evaluate("() => document.getElementById('open-settings').click()")
        self.assertIn("加密凭证已保存在本机", second.locator("#credential-status").inner_text())
        second.locator('[data-settings-tab="conversation"]').click()
        second.wait_for_function("() => document.querySelector('#snow-proactive-enabled').checked")
        self.assertIn("已开启", second.locator("#snow-proactive-note").inner_text())
        self.assertEqual(len(consent), 1)

        second.locator('[data-settings-tab="models"]').click()
        second.locator("#clear-credential").click()
        second.wait_for_function(f"() => sessionStorage.getItem({json.dumps(auth)}) === null && localStorage.getItem({json.dumps(auth)}) === null")
        self.assertEqual(self.errors, [])
        context.close()

    def test_conversation_settings_store_preferences_and_paid_messages_need_cost_confirmation(self):
        page = self.open(self.page(width=1280, height=820, locale="zh-CN", proactive=True))
        consent, recoveries = [], []

        def grant(route):
            body = route.request.post_data_json
            consent.append(body)
            route.fulfill(json={"consent_token": "synthetic-consent-only", "notice_version": "proactive-cost-1",
                                "model_session_id": body["model_session_id"],
                                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})

        def recover(route):
            body = route.request.post_data_json
            recoveries.append((route.request.headers.get("x-snow-recovery-only"), body.get("credential"), body["request_id"]))
            route.fulfill(json={"request_id": body["request_id"], "character_id": body["character_id"], "source": "proactive_reminder",
                                "status": "failed", "model_called": True, "usage": {"provider_calls": 1, "total_tokens": None}})
        page.route("**/public/v2/rendezvous/consent", grant)
        page.route("**/public/v2/rendezvous/message", recover)
        page.evaluate("() => document.getElementById('open-settings').click()")
        page.locator('[data-settings-tab="conversation"]').click()
        panel = page.locator("#settings-panel-conversation")
        panel.wait_for(state="visible")
        self.assertTrue(page.locator("#settings-panel-history").is_hidden())
        self.assertEqual(page.locator("#settings-tab-conversation").get_attribute("aria-selected"), "true")
        self.assertEqual(page.locator('[data-settings-tab="history"] + [data-settings-tab]').get_attribute("id"), "settings-tab-conversation")
        self.assertEqual(panel.locator("#snow-usage-setting").count(), 1)
        # Reading preferences are saved on this browser only.
        panel.locator('[data-snow-speed="fast"]').click()
        panel.locator('[data-snow-delay="long"]').click()
        page.locator("#snow-fast-forward-read").uncheck()
        page.wait_for_function("() => JSON.parse(localStorage.getItem('project-snow-public:reading:v1') || '{}').fastForwardRead === false")
        self.assertEqual(page.evaluate("() => JSON.parse(localStorage.getItem('project-snow-public:reading:v1'))"),
                         {"textSpeed": "fast", "autoDelay": "long", "fastForwardRead": False})
        self.assertEqual(panel.locator('[data-snow-speed][aria-checked="true"]').inner_text(), "快")
        # Paid messages depend on reminders.
        page.locator("#snow-reminders-enabled").uncheck()
        page.wait_for_function("() => document.querySelector('#snow-proactive-enabled').disabled")
        self.assertIn("约定提醒", page.locator("#snow-proactive-note").inner_text())
        self.assertFalse(self.idb_preferences(page)["reminderPreferences"]["enabled"])
        page.locator("#snow-reminders-enabled").check()
        page.wait_for_function("() => !document.querySelector('#snow-proactive-enabled').disabled")
        # Switching on asks about the cost first; declining sends nothing.
        toggle, dialog = page.locator("#snow-proactive-enabled"), page.locator("#snow-proactive-dialog")
        toggle.click()
        dialog.wait_for(state="visible")
        self.assertFalse(toggle.is_checked())
        self.assertIn("每条消息会调用一次你的模型", dialog.inner_text())
        dialog.locator("button", has_text="暂不开启").click()
        dialog.wait_for(state="hidden")
        self.assertFalse(toggle.is_checked())
        self.assertEqual(consent, [])
        toggle.click()
        dialog.locator("button", has_text="我知道会产生费用，开启").click()
        dialog.wait_for(state="hidden")
        page.wait_for_function("() => document.querySelector('#snow-proactive-enabled').checked")
        self.assertEqual([(row["accepted_cost_notice"], row["notice_version"]) for row in consent], [(True, "proactive-cost-1")])
        self.assertIn("已开启", page.locator("#snow-proactive-note").inner_text())
        toggle.click()
        page.wait_for_function("() => !document.querySelector('#snow-proactive-enabled').checked")
        self.assertFalse(self.idb_preferences(page)["proactivePreferences"]["enabled"])
        # A paid request whose outcome is unknown is only ever looked up, never re-sent.
        page.evaluate("""async () => new Promise((resolve, reject) => { const open = indexedDB.open('project-snow-public');
          open.onsuccess = () => { const db = open.result, threads = db.transaction('threads').objectStore('threads').getAll();
            threads.onsuccess = () => {
          const characterId = threads.result[0].characterId, requestId = crypto.randomUUID();
          const record = { key: `proactive_request_v1:2026-10-19:${characterId}:rv-1`, status: 'unknown', dispatched: true, createdAt: Date.now(),
            envelope: { request_id: requestId, character_id: characterId, rendezvous_id: 'rv-1', segment_id: 'segment', state_package: '',
              session_context_package: '', provider: 'openai', model: 'synthetic-model', reasoning_effort: null, model_session_id: 'session',
              notice_version: 'proactive-cost-1' } };
          const tx = db.transaction('app_state', 'readwrite'); tx.objectStore('app_state').put(record);
            tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); }; }; })""")
        page.locator('[data-settings-tab="history"]').click()
        page.locator('[data-settings-tab="conversation"]').click()
        row = page.locator("#snow-proactive-requests .snow-request-row")
        row.wait_for()
        self.assertEqual(row.locator(".snow-request-status").inner_text(), "结果未确认")
        row.locator("button", has_text="查询原请求").click()
        page.wait_for_function("() => document.querySelector('#snow-proactive-requests .snow-request-status')?.textContent === '没有送达'")
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0][:2], ("1", ""))
        self.assertEqual(self.errors, [])

    def test_second_batch_surfaces_are_readable_in_every_tone(self):
        for tone in ("light", "dusk", "dark"):
            with self.subTest(tone=tone):
                page = self.open(self.page(width=1440, height=900, tone=tone, locale="zh-CN", proactive=True))
                self.enter_stage(page)
                self.long_reply(page)
                self.send_long(page)
                page.locator('#stage-quick-menu [data-stage-quick="auto"]').click()
                ratios = page.locator('.snow-page-meter, #stage-quick-menu [data-stage-quick="auto"]').evaluate_all(CONTRAST)
                page.locator('#stage-quick-menu [data-stage-quick="auto"]').click()
                page.evaluate("() => document.getElementById('stage-open-settings').click()")
                page.locator('[data-settings-tab="conversation"]').click()
                page.locator("#settings-panel-conversation").wait_for(state="visible")
                ratios += page.locator("#settings-panel-conversation :is(p, strong, small, .snow-seg button, .snow-cost-tag)").evaluate_all(CONTRAST)
                page.locator("#snow-proactive-enabled").click()
                page.locator("#snow-proactive-dialog").wait_for(state="visible")
                ratios += page.locator("#snow-proactive-dialog :is(h2, li, .snow-cost-tag, .panel-kicker, .snow-modal-foot > span, button)").evaluate_all(CONTRAST)
                low = [item for item in ratios if item[1] < 4.5]
                self.assertGreater(len(ratios), 12)
                self.assertEqual(low, [], low)
                page.close()
        self.assertEqual(self.errors, [])

    STAGE_BOX = """() => {
      const box = selector => { const node = document.querySelector(selector); if (!node || !node.getClientRects().length) return null;
        const rect = node.getBoundingClientRect(); return [rect.left, rect.top, rect.right, rect.bottom].map(Math.round); };
      return { dialogue: box('#stage-dialogue'), art: box('#stage-character-art'), row: box('#composer .composer-row'),
        tab: box('#composer .request-status-row'), toast: box('#toast') };
    }"""

    def assert_stage_still(self, rest, state):
        keys = ("dialogue", "art", "row")
        self.assertEqual({key: state[key] for key in keys}, {key: rest[key] for key in keys}, state)

    def assert_tab_above_dialogue(self, state):
        tab, box = state["tab"], state["dialogue"]
        self.assertIsNotNone(tab, state)
        self.assertGreaterEqual(tab[1], 0, state)
        self.assertLessEqual(tab[3], box[1] - 4, state)
        self.assertGreaterEqual(tab[0], box[0], state)
        self.assertLessEqual(tab[2], box[2], state)

    def test_request_status_and_typing_never_resize_the_stage(self):
        # Face to face, the request status (stop waiting, done, failed + retry), the input hint and toasts used to sit
        # between the dialogue box and the composer: they split the two apart and shrank the stage and the portrait.
        for width, height, tone in ((1440, 900, "light"), (1440, 900, "dusk"), (390, 844, "dark")):
            with self.subTest(width=width, tone=tone):
                dialogue.DialogueHandler.scenario = "success"
                page = self.open(self.page(width=width, height=height, tone=tone, locale="zh-CN"))
                self.enter_stage(page)
                page.wait_for_timeout(300)
                rest = page.evaluate(self.STAGE_BOX)
                self.assertIsNone(rest["tab"])
                page.locator("#message-input").fill("好")
                page.wait_for_timeout(200)
                self.assert_stage_still(rest, page.evaluate(self.STAGE_BOX))
                # A multi-line draft lifts the dialogue box with the composer, but the portrait keeps its size.
                page.locator("#message-input").fill("好\n再说一句\n还有一句\n最后一句")
                page.wait_for_timeout(200)
                grown = page.evaluate(self.STAGE_BOX)
                self.assertLess(grown["row"][1], rest["row"][1] - 20, grown)
                self.assertLessEqual(max(abs(a - b) for a, b in zip(grown["art"], rest["art"])), 6, (grown["art"], rest["art"]))
                page.locator("#message-input").fill("好")
                page.wait_for_timeout(200)
                self.assert_stage_still(rest, page.evaluate(self.STAGE_BOX))

                held = []
                page.route("**/public/v2/chat/stream", lambda route: held.append(route))
                page.locator("#send-message").click()
                page.locator("#stop-waiting").wait_for(state="visible")
                page.wait_for_timeout(200)
                pending = page.evaluate(self.STAGE_BOX)
                self.assert_stage_still(rest, pending)
                self.assert_tab_above_dialogue(pending)
                ratios = page.locator("#composer .request-status-row #stop-waiting").evaluate_all(CONTRAST)
                page.evaluate("() => { const toast = document.getElementById('toast'); toast.textContent = '模型会话已保存到当前标签页'; toast.hidden = false; }")
                toast = page.evaluate(self.STAGE_BOX)["toast"]
                self.assertLessEqual(toast[3], pending["tab"][1] - 4, (toast, pending["tab"]))
                page.evaluate("() => { document.getElementById('toast').hidden = true; }")

                route = held[0]
                response = route.fetch()
                headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
                page.unroute("**/public/v2/chat/stream")
                route.fulfill(status=response.status, headers=headers, body=response.text())
                page.wait_for_function("() => document.querySelector('#request-status')?.textContent === '已完成'")
                page.wait_for_function("() => document.querySelector('#stage-advance')?.classList.contains('is-ready')")
                done = page.evaluate(self.STAGE_BOX)
                self.assert_stage_still(rest, done)
                self.assertIsNone(done["tab"], "▼ already marks the line as finished; no lingering 已完成 tag")

                dialogue.DialogueHandler.scenario = "manual"
                page.locator("#message-input").fill("合成日常聊天")
                page.locator("#send-message").click()
                page.locator("#composer .request-status-row .request-retry").wait_for(state="visible")
                failed = page.evaluate(self.STAGE_BOX)
                self.assert_stage_still(rest, failed)
                self.assert_tab_above_dialogue(failed)
                ratios += page.locator("#composer .request-status-row :is(.request-status, .request-retry)").evaluate_all(CONTRAST)
                self.assertEqual(page.locator("#composer .request-retry").evaluate("node => getComputedStyle(node).borderTopLeftRadius"), "999px")
                low = [item for item in ratios if item[1] < 4.5]
                self.assertGreaterEqual(len(ratios), 3)
                self.assertEqual(low, [], low)
                page.close()
        self.assertEqual(self.errors, [])

    def test_returning_visitor_reopens_the_last_character_and_channel(self):
        page = self.open(self.page())
        if page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
            page.locator("#open-contacts").click()
        # Not the character the app opens by default.
        target = page.locator("[data-character][aria-current='false']").first
        character_id = target.get_attribute("data-character")
        target.click()
        current = "id => document.querySelector(`[data-character=\"${id}\"]`)?.getAttribute('aria-current') === 'true'"
        page.wait_for_function(current, arg=character_id)
        self.send(page, "下次接着聊")
        channel = page.evaluate("document.getElementById('chat-app').dataset.channel")
        page.reload(wait_until="networkidle")
        page.wait_for_function("document.querySelector('#connection-status')?.textContent === '服务已连接'")
        page.wait_for_function(current, arg=character_id)
        self.assertEqual(page.evaluate("document.getElementById('chat-app').dataset.channel"), channel)
        if channel == "text":
            page.locator("#timeline").get_by_text("下次接着聊").wait_for(state="visible")
        self.assertEqual(self.errors, [])

    def test_flag_off_never_loads_the_feature_modules(self):
        page = self.open(self.page(enabled=False), enabled=False)
        page.wait_for_timeout(800)
        self.assertEqual([path for path in self.requested if path.startswith("/modules/snow-feature") or path in {
            "/modules/snow-features.js", "/modules/snow-pager.js", "/modules/snow-conversation-ui.js"}], [])
        self.assertEqual(page.locator("#snow-backlog-tools, #snow-memory-dialog, .snow-line-tools, #snow-usage-setting, "
                                      "#settings-tab-conversation, .snow-page-meter, #snow-movement-reminder, [data-stage-quick=\"auto\"]").count(), 0)
        self.assertEqual(self.errors, [])
