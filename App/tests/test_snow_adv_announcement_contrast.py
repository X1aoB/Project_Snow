"""Real announcement DOM with synthetic loopback feed; no provider or user data."""
from __future__ import annotations

import hashlib
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from tests import test_public_frontend_e2e as fixture

PUBLIC = Path(__file__).resolve().parents[1] / "public_frontend"
CSS = PUBLIC / "snow-adv.css"
PIN_LIGHT_TONE = "localStorage.setItem('project-snow-public:tone', 'light')"
BASELINE_COMMIT = "0d6f97abb371bfd9a4b740c77f8d08e47943bf94"
BASELINE_LF_SHA256 = "8bdfead121ba60ce8921c2cdf519412a306365b7240e194849c5d950d3bbf083"
QUERIES = {("/shared/immersive.css", "v=0.9.2"), ("/app.css", "v=1.0.1"),
           ("/snow-adv.css", "v=1.0.1"), ("/app.js", "v=1.0.1")}


def synthetic_feed():
    return {"schema_version": 1, "timezone": "Asia/Shanghai", "birthdays": [], "updates": [
        {"id": "synthetic-" + str(index), "published_at": f"2020-01-0{3-index}T12:00:00+08:00",
         "title": title, "summary": "合成公告正文：检查深色与暮色的文字可读性。",
         "items": ["这是一条合成列表，包含标记。", "保留历史公告的完整内容。"]}
        for index, title in enumerate(("合成最新公告", "合成过往公告一", "合成过往公告二"))]}


def allowed_get_paths():
    paths = {"/", "/index.html", "/app.js", "/app.css", "/snow-adv.css", "/snow-adv-fonts.css",
             "/dialogue-context.js", "/announcements.json", "/public/v1/config", "/public/v1/build-info", "/public/v1/characters"}
    for directory in ("modules", "statistics", "assets"):
        paths.update("/" + path.relative_to(PUBLIC).as_posix() for path in (PUBLIC / directory).rglob("*") if path.is_file())
    for root, prefix in ((fixture.SHARED_ROOT, "/shared/"), (fixture.IMMERSIVE_ROOT, "/assets/immersive/")):
        paths.update(prefix + path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    return paths


class AnnouncementFixtureServer:
    """Context manager; callers own browser/page teardown and inspect violations."""
    def __init__(self, feed_raw=None):
        self.feed_raw = (json.dumps(synthetic_feed(), ensure_ascii=False).encode()
                         if feed_raw is None else feed_raw)
        self.feed = json.loads(self.feed_raw)
        self.feed_sha256 = hashlib.sha256(self.feed_raw).hexdigest()

    def __enter__(self):
        allowed, violations = allowed_get_paths(), []
        feed_raw = self.feed_raw
        class Handler(fixture.PublicFrontendHandler):
            turnstile_site_key = ""
            announcement_payload = synthetic_feed()
            announcement_requests = announcement_failures = 0
            request_paths, chat_payloads = [], []

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path not in allowed or (parsed.query and (parsed.path, parsed.query) not in QUERIES):
                    violations.append("server_get_denied")
                    return self._json({"error": "synthetic_route_denied"}, status=403)
                if parsed.path == "/announcements.json":
                    type(self).announcement_requests += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(feed_raw)))
                    self.end_headers()
                    return self.wfile.write(feed_raw)
                if parsed.path == "/public/v1/characters":
                    return self._json({"count": 0, "characters": []})
                return super().do_GET()

            def do_POST(self):
                violations.append("server_write_denied")
                return self._json({"error": "synthetic_write_denied"}, status=405)
            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST

        class Server(ThreadingHTTPServer):
            daemon_threads = False
            block_on_close = True
            def handle_error(self, _request, _client_address):
                violations.append("server_handler_error")

        self.server, self.handler, self.violations, self.closed = Server(("127.0.0.1", 0), Handler), Handler, violations, False
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.closed = not self.thread.is_alive()
        if not self.closed: self.violations.append("server_thread_not_closed")


def wait_for_announcement_paint(page):
    page.evaluate("() => document.fonts.ready")
    page.wait_for_function("""() => document.getAnimations().every(animation => {
      const timing=animation.effect?.getComputedTiming();
      return !timing || !Number.isFinite(timing.endTime) || ['finished','idle'].includes(animation.playState);
    })""")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def open_announcement_page(browser, base_url, width, height, tone, time, snow=True, stylesheet=None, pinned_script=None, feed=None):
    """Open real UI with a loopback feed; no DOM renderer replacement."""
    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce", timezone_id="Asia/Hong_Kong")
    page.set_default_timeout(10000)
    guards = {name: [] for name in ("blocked", "page_errors", "http_failures", "request_failures", "writes")}
    origin, allowed = urlsplit(base_url), allowed_get_paths()
    feed = synthetic_feed() if feed is None else feed
    script = (PUBLIC / "app.js").read_bytes() if pinned_script is None else pinned_script
    page.add_init_script(PIN_LIGHT_TONE + ";" + """(() => {
      localStorage.setItem('project-snow-public:tone', %s);
      localStorage.setItem('project-snow-public:motion', 'off');
      localStorage.setItem('project-snow-public:announcements:read:v1', JSON.stringify(%s));
      const set=()=>{if(!document.documentElement)return false;document.documentElement.dataset.snowTime=%s;return true};
      if(!set()){const observer=new MutationObserver(()=>{if(set())observer.disconnect()});observer.observe(document,{childList:true})}
    })();""" % (json.dumps(tone), json.dumps(["update:"+row["id"]+":"+row["published_at"] for row in feed["updates"]]), json.dumps(time)))

    def network(route):
        request, parsed = route.request, urlsplit(route.request.url)
        same = (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc)
        if request.method != "GET":
            guards["writes"].append("non_get_denied"); route.abort(); return
        if not same or parsed.path not in allowed or parsed.fragment or (parsed.query and (parsed.path, parsed.query) not in QUERIES):
            guards["blocked"].append("request_denied"); route.abort(); return
        if parsed.path == "/app.js":
            route.fulfill(content_type="text/javascript", body=script); return
        if parsed.path == "/snow-adv.css" and stylesheet is not None:
            route.fulfill(content_type="text/css", body=stylesheet); return
        if parsed.path == "/public/v1/config":
            payload = route.fetch().json()
            payload["features"] = {"ui_snow_adv": snow, "motion_v2": True}
            route.fulfill(json=payload); return
        route.continue_()

    page.route("**/*", network)
    page.on("pageerror", lambda _error: guards["page_errors"].append("page_error"))
    page.on("response", lambda response: guards["http_failures"].append(response.status) if response.status >= 400 else None)
    page.on("requestfailed", lambda _request: guards["request_failures"].append("request_failed"))
    page.goto(base_url, wait_until="networkidle")
    if page.locator("#snow-lock").is_visible(): page.locator("#snow-lock").click(position={"x": 10, "y": 10})
    page.locator("#snow-boot").wait_for(state="detached")
    page.locator("#snow-lock").wait_for(state="detached")
    page.locator("#accept-experience-notice").click()
    page.locator("#skip-onboarding").wait_for(state="visible")
    page.locator("#skip-onboarding").click()
    if width <= 820 and page.locator("#contact-panel.open").count():
        page.locator("#close-contacts").click()
    if width <= 820:
        page.wait_for_function("""() => {const p=document.querySelector('#contact-panel');return !p.classList.contains('open') && p.inert && p.getBoundingClientRect().right<=0}""")
    page.locator("#open-announcements").click()
    page.locator("#announcements-dialog[open]").wait_for(state="visible")
    page.locator("#announcement-status").wait_for(state="hidden")
    page.wait_for_function("count => document.querySelectorAll('.announcement-update').length===count", arg=len(feed["updates"]))
    wait_for_announcement_paint(page)
    guards["credentials_absent"] = page.evaluate("""() => !sessionStorage.getItem('project-snow-public:byok')
      && !sessionStorage.getItem('project-snow-public:subscription-pair')
      && !document.querySelector('#api-key').value && !document.querySelector('#model-id').value""")
    return page, guards


def rgba(value):
    match = re.fullmatch(r"rgba?\(([^)]+)\)", value)
    if not match: raise AssertionError("unsupported_computed_color")
    parts = [float(part) for part in re.split(r"[, /]+", match[1].strip())]
    if len(parts) not in (3, 4): raise AssertionError("unsupported_computed_color")
    return [*parts[:3], parts[3] if len(parts) == 4 else 1.0]


def composite(foreground, background):
    return [foreground[index] * foreground[3] + background[index] * (1 - foreground[3]) for index in range(3)]


def contrast(first, second):
    def luminance(color):
        values = [channel / 255 for channel in color]
        return sum(weight * (value / 12.92 if value <= .04045 else ((value + .055) / 1.055)**2.4)
                   for value, weight in zip(values, (.2126, .7152, .0722)))
    a, b = sorted((luminance(first), luminance(second)))
    return (b + .05) / (a + .05)


def measure_announcement_contrast(page):
    """All history cards; actual CSS alpha compositing, no opaque-alpha shortcut."""
    cards = page.locator(".announcement-update").evaluate_all("""cards => cards.map((card,index) => {
      const targets=[['h3','h3'],['p','p'],['li','li'],['marker','li'],['meta','.announcement-update-meta > span'],['date','time']];
      const rows=[];
      for(const [field,selector] of targets)for(const node of card.querySelectorAll(selector)){
        const css=getComputedStyle(node,field==='marker'?'::marker':null),layers=[];
        let unsupported=false,opaque=false;
        for(let parent=node;parent;parent=parent.parentElement){
          const style=getComputedStyle(parent);
          if(Number(style.opacity)!==1 || style.filter!=='none' || style.mixBlendMode!=='normal')unsupported=true;
          if(!opaque){
            if(style.backgroundImage!=='none')unsupported=true;
            layers.push(style.backgroundColor);
            const match=style.backgroundColor.match(/^rgba?\\(([^)]+)\\)$/);
            if(match){const parts=match[1].split(/[, /]+/).filter(Boolean);opaque=parts.length===3 || Number(parts[3])===1}
          }
        }
        rows.push({field,color_css:css.color,background_css_layers:layers,unsupported:unsupported||!opaque});
      }
      return {card_index:index,background_css:getComputedStyle(card).backgroundColor,
        border_css:getComputedStyle(card).borderTopColor,rows};
    })""")
    if not cards: raise AssertionError("announcement_cards_missing")
    rows, minima = [], {}
    for card in cards:
        if {row["field"] for row in card["rows"]} != {"h3", "p", "li", "marker", "meta", "date"}:
            raise AssertionError("announcement_text_classes_missing")
        for row in card["rows"]:
            if row.pop("unsupported"): raise AssertionError("unsupported_announcement_paint")
            layers = [rgba(color) for color in row["background_css_layers"]]
            if layers[-1][3] != 1: raise AssertionError("opaque_background_not_established")
            background = layers[-1][:3]
            for layer in reversed(layers[:-1]): background = composite(layer, background)
            foreground = composite(rgba(row["color_css"]), background)
            ratio = contrast(foreground, background)
            rows.append({"card_index": card["card_index"], **row, "effective_background_rgb": background,
                         "effective_foreground_rgb": foreground, "contrast": ratio})
            minima[row["field"]] = min(minima.get(row["field"], ratio), ratio)
    theme = page.locator("html").evaluate("root => ({ui:root.dataset.ui,tone:root.dataset.tone,time:root.dataset.snowTime,surface:getComputedStyle(root).getPropertyValue('--tn-surface').trim()})")
    return {"schema_version": "project-snow-announcement-contrast-1", "card_count": len(cards), "actual_theme": theme,
            "minimum_contrast": min(row["contrast"] for row in rows), "by_class_minimum": minima,
            "card_backgrounds": [{key: value for key, value in card.items() if key != "rows"} for card in cards], "rows": rows,
            "background_method": "actual ancestor CSS backgrounds composed source-over; ancestor opacity/filter/blend and unknown paint rejected"}


def baseline_css(_candidate):
    # Later independent CSS changes must not alter the complete historical control.
    result = subprocess.run(
        ["git", "--no-replace-objects", "show", BASELINE_COMMIT + ":App/public_frontend/snow-adv.css"],
        cwd=PUBLIC.parents[1], capture_output=True, check=True, timeout=20,
    )
    baseline = result.stdout.replace(b"\r\n", b"\n").decode("utf-8")
    if hashlib.sha256(baseline.encode()).hexdigest() != BASELINE_LF_SHA256:
        raise AssertionError("complete_0d6_css_baseline_required")
    return baseline


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvAnnouncementContrastTests(TestCase):
    def setUp(self):
        self.fixture, self.playwright, self.browser = None, None, None
        self.script = (PUBLIC / "app.js").read_bytes()
        self.guards, self.evidence = [], []
        self.addCleanup(self.finish)
        self.fixture = AnnouncementFixtureServer().__enter__()
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)

    def save(self, name, raw):
        if directory := os.getenv("SNOW_ANNOUNCEMENT_EVIDENCE_DIR"):
            path = Path(directory); path.mkdir(parents=True, exist_ok=True)
            with (path / ("announcement-" + name)).open("xb") as stream: stream.write(raw)

    def finish(self):
        teardown_errors = []
        for target, method, code in ((self.browser, "close", "browser_close_failed"),
                                     (self.playwright, "stop", "playwright_stop_failed"),
                                     (self.fixture, "__exit__", "server_close_failed")):
            if target is not None:
                try: getattr(target, method)()
                except Exception: teardown_errors.append(code)
        candidate = CSS.read_text(encoding="utf-8")
        self.save(self._testMethodName + ".json", (json.dumps({"schema_version": "project-snow-announcement-browser-evidence-1",
            "technical_qa_only": True, "human_approval": False, "synthetic_fixture": True, "paid_calls": 0,
            "baseline_commit": BASELINE_COMMIT, "baseline_lf_sha256": BASELINE_LF_SHA256,
            "candidate_css_lf_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
            "pinned_app_js_sha256": hashlib.sha256(self.script).hexdigest(), "guards": self.guards,
            "fixture_feed_sha256": self.fixture.feed_sha256 if self.fixture else None,
            "zero_pixel_control": "same candidate app.js and identical feed on both sides; only full 0d6 CSS versus candidate CSS; no claim that adding announcement text is visually unchanged",
            "teardown_errors": teardown_errors, "server_closed": bool(self.fixture and self.fixture.closed),
            "server_violations": self.fixture.violations if self.fixture else [],
            "evidence": self.evidence}, ensure_ascii=False, indent=2) + "\n").encode())
        self.assertEqual(teardown_errors, [])
        self.assertIsNotNone(self.fixture)
        self.assertTrue(self.fixture.closed)
        self.assertEqual(self.fixture.violations, [])
        for guards in self.guards:
            self.assertIs(guards["credentials_absent"], True)
            for name in ("blocked", "page_errors", "http_failures", "request_failures", "writes"):
                self.assertEqual(guards[name], [], name)

    def open(self, width, height, tone, time, snow=True, stylesheet=None):
        page, guards = open_announcement_page(self.browser, self.fixture.base_url, width, height, tone, time,
                                              snow=snow, stylesheet=stylesheet, pinned_script=self.script)
        self.guards.append(guards)
        return page

    def test_all_history_card_text_contrast_in_dusk_and_night(self):
        for width, height in ((1440, 900), (390, 844)):
            for tone, time in (("auto", "dusk"), ("dark", "night")):
                with self.subTest(width=width, time=time):
                    page = self.open(width, height, tone, time)
                    measured = measure_announcement_contrast(page)
                    self.assertEqual(measured["card_count"], len(synthetic_feed()["updates"]))
                    self.assertEqual({key: measured["actual_theme"][key] for key in ("ui", "tone", "time")},
                                     {"ui": "snow-adv", "tone": "dark", "time": time})
                    surface = [float(part) for part in measured["actual_theme"]["surface"].split()]
                    for card in measured["card_backgrounds"]:
                        self.assertEqual(rgba(card["background_css"]), [*surface, 1.0])
                    self.save(f"{width}-{time}.png", page.screenshot(animations="disabled"))
                    self.evidence.append({"width": width, "height": height, "tone": tone, "time": time, **measured})
                    for row in measured["rows"]: self.assertGreaterEqual(row["contrast"], 4.5, row)
                    page.close()

    def test_snow_white_and_flag_off_are_pixel_identical_to_full_0d6_css(self):
        from PIL import Image, ImageChops
        candidate = CSS.read_text(encoding="utf-8")
        baseline = baseline_css(candidate)
        for width, height in ((1440, 900), (390, 844)):
            for tone, time, snow in (("light", "day", True), ("auto", "dusk", False), ("dark", "night", False)):
                with self.subTest(width=width, tone=tone, snow=snow):
                    page = self.open(width, height, tone, time, snow=snow, stylesheet=baseline)
                    before = page.screenshot(animations="disabled")
                    page.add_style_tag(content=candidate)
                    wait_for_announcement_paint(page)
                    after = page.screenshot(animations="disabled")
                    label = f"zero-{width}-{tone}-{'snow' if snow else 'classic'}"
                    self.save(label + "-before.png", before); self.save(label + "-after.png", after)
                    difference = ImageChops.difference(Image.open(io.BytesIO(before)).convert("RGBA"), Image.open(io.BytesIO(after)).convert("RGBA"))
                    bbox = difference.getbbox(alpha_only=False)
                    self.evidence.append({"case": label, "before_sha256": hashlib.sha256(before).hexdigest(),
                        "after_sha256": hashlib.sha256(after).hexdigest(), "pixel_difference_bbox": bbox,
                        "complete_original_css_tail_preserved": True})
                    self.assertIsNone(bbox, label)
                    page.close()
