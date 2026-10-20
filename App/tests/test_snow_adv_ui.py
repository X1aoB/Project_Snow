"""雪境 ADV (P1) contracts: default-off flag, scoped styles, font and icon assets.

Static checks run in every tier.  The browser checks join the existing
RUN_PUBLIC_E2E tier and use only the synthetic e2e fixture server; no model,
provider or runtime database is involved.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase, mock, skipUnless

from fastapi.testclient import TestClient

from backend.snow_app.config import PublicSettings, Settings
from backend.snow_app.public_main import create_app
from backend.snow_app.public_store import PublicStore
from scripts.build_snow_adv_fonts import EXCLUDED_RANGES, parse_ranges
from scripts.fingerprint_public_frontend import fingerprint
from tests.test_public_api import _settings

APP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = APP_ROOT / "public_frontend"
SKIN_CSS = PUBLIC_ROOT / "snow-adv.css"
FONT_CSS = PUBLIC_ROOT / "snow-adv-fonts.css"
FONT_DIR = PUBLIC_ROOT / "assets" / "fonts" / "lxgw-wenkai-screen"
PREFIX = 'html[data-ui="snow-adv"]'
# P8: the interface tone follows the stage time of day (dark at dusk and
# night). Existing assertions describe the snow-white tone, so every page
# pins it; the dark-tone tests opt in explicitly.
PIN_LIGHT_TONE = "window.localStorage.setItem('project-snow-public:tone', 'light')"
DAYLIGHT_CONTRAST_MARKER = "/* C8.1 daylight contrast */"


def _rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # @keyframes blocks hold no selectors; they only take effect when a scoped rule references them.
    css = re.sub(r"@keyframes\s+[\w-]+\s*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    return [(selector.strip(), body) for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _split_selectors(selector: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for character in selector:
        depth += character == "("
        depth -= character == ")"
        if character == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += character
    return [part for part in [*parts, current.strip()] if part]


class SnowAdvFlagTests(TestCase):
    def test_environment_flag_is_off_unless_exactly_true(self):
        for value, expected in ((None, False), ("false", False), ("1", False), ("TRUE", True), ("true", True)):
            environment = {key: item for key, item in os.environ.items() if key != "PUBLIC_UI_SNOW_ADV"}
            if value is not None:
                environment["PUBLIC_UI_SNOW_ADV"] = value
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertIs(PublicSettings.from_environment().ui_snow_adv, expected, value)

    def test_public_config_reports_the_flag(self):
        for enabled in (False, True):
            settings = replace(_settings(), ui_snow_adv=enabled)
            store = PublicStore(settings.database_url)
            store.create_schema()
            client = TestClient(create_app(settings, Settings.from_environment(), store))
            try:
                self.assertIs(client.get("/public/v1/config").json()["features"]["ui_snow_adv"], enabled)
            finally:
                client.close()

    def test_rollout_pipeline_accepts_the_flag_and_defaults_it_off(self):
        # The deploy allow-list and candidate acceptance know the flag; the
        # shared example env keeps it off so only an exact-SHA candidate file
        # (or a reviewed env change) can enable it. Behaviour: tests/test_snow_adv_deployment.py.
        deploy = (APP_ROOT / "ops" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("PUBLIC_UI_SNOW_ADV", deploy)
        self.assertIn("public_ui_snow_adv=false", deploy)
        self.assertIn('"PUBLIC_UI_SNOW_ADV": "ui_snow_adv"', (APP_ROOT / "ops" / "candidate_acceptance.py").read_text(encoding="utf-8"))
        self.assertIn("PUBLIC_UI_SNOW_ADV=false", (APP_ROOT / "ops" / "public.env.example").read_text(encoding="utf-8"))


class SnowAdvStyleContractTests(TestCase):
    def test_every_skin_rule_is_scoped_to_the_flag(self):
        rules = _rules(SKIN_CSS.read_text(encoding="utf-8"))
        self.assertGreater(len(rules), 20)
        self.assertNotIn("@import", SKIN_CSS.read_text(encoding="utf-8"))
        for selector, _ in rules:
            for part in _split_selectors(selector):
                self.assertTrue(part.startswith(PREFIX), part)

    def test_no_interface_text_below_twelve_pixels_when_enabled(self):
        skin = SKIN_CSS.read_text(encoding="utf-8")
        minimum = re.search(r":is\(([^{]*)\)\s*\{\s*font-size:\s*var\(--fs-12\)", skin, flags=re.S)
        self.assertIsNotNone(minimum)
        covered = {re.sub(r"\s+", " ", item) for item in _split_selectors(minimum.group(1))}
        small = set()
        for selector, body in _rules((PUBLIC_ROOT / "app.css").read_text(encoding="utf-8")):
            size = re.search(r"font-size:\s*(\d*\.?\d+)rem", body)
            if size and float(size.group(1)) < 0.75:
                small.update(re.sub(r"\s+", " ", item) for item in _split_selectors(selector))
        self.assertTrue(small)
        self.assertEqual(sorted(small - covered), [])

    def test_story_font_is_not_part_of_the_eager_stylesheet(self):
        self.assertNotIn("@font-face", SKIN_CSS.read_text(encoding="utf-8"))
        index = (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/snow-adv.css?v=', index)
        self.assertNotIn("snow-adv-fonts.css", index)


class SnowAdvAssetContractTests(TestCase):
    def test_font_files_are_content_addressed_and_emoji_free(self):
        stylesheet = FONT_CSS.read_text(encoding="utf-8")
        referenced = re.findall(r"url\('/assets/fonts/lxgw-wenkai-screen/([^']+)'\)", stylesheet)
        on_disk = sorted(path.name for path in FONT_DIR.glob("*.woff2"))
        self.assertEqual(sorted(referenced), on_disk)
        self.assertGreater(len(on_disk), 80)
        source = json.loads((FONT_DIR / "SOURCE.json").read_text(encoding="utf-8"))
        self.assertEqual((source["package"], source["version"]), ("lxgw-wenkai-screen-webfont", "1.7.0"))
        for name in on_disk:
            payload = (FONT_DIR / name).read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            self.assertRegex(name, rf"\.{digest[:16]}\.woff2$")
            self.assertEqual(source["files"][name], digest)
            self.assertEqual(payload[:4], b"wOF2")
        self.assertIn("SIL OPEN FONT LICENSE", (FONT_DIR / "OFL.txt").read_text(encoding="utf-8").upper())
        for ranges in re.findall(r"unicode-range:([^}]+)\}", stylesheet):
            for low, high in parse_ranges(ranges):
                for excluded_low, excluded_high in EXCLUDED_RANGES:
                    self.assertTrue(high < excluded_low or low > excluded_high, f"U+{low:x}-{high:x}")
        self.assertEqual(stylesheet.count("font-display:swap"), len(on_disk))

    def test_icon_module_is_generated_from_the_vendored_lucide(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        result = subprocess.run([node, str(APP_ROOT / "scripts" / "build_snow_adv_icons.mjs"), "--check"],
                                capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        module = (PUBLIC_ROOT / "modules" / "snow-icons.js").read_text(encoding="utf-8")
        self.assertIn("ISC License", module)
        self.assertNotIn("innerHTML", module)

    def test_snow_world_module_is_registered_and_csp_safe(self):
        # P2: the procedural snow-world module ships behind the flag.  It must be
        # declared in the build manifest (ordered before app.js) and listed as an
        # app.js dependency so fingerprinting rewrites its import, and its body
        # must stay CSP-safe: no inline <style>, no external URLs beyond the SVG
        # namespace, and no dynamic code execution.
        manifest = json.loads((PUBLIC_ROOT / "assets-manifest.json").read_text(encoding="utf-8"))
        urls = [item["public_url"] for item in manifest["assets"]]
        self.assertIn("/modules/snow-world.js", urls)
        self.assertLess(urls.index("/modules/snow-world.js"), urls.index("/app.js"))
        app_entry = next(item for item in manifest["assets"] if item["public_url"] == "/app.js")
        self.assertIn("/modules/snow-world.js", app_entry.get("dependencies", []))
        source = (PUBLIC_ROOT / "modules" / "snow-world.js").read_text(encoding="utf-8")
        self.assertIn('from "/modules/snow-world.js"', (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8"))
        self.assertNotIn("<style", source)
        self.assertFalse(re.search(r"\beval\b|new\s+Function", source), "no dynamic code execution")
        external = [url for url in re.findall(r"https?://[^\s\"')]+", source) if url != "http://www.w3.org/2000/svg"]
        self.assertEqual(external, [], external)

    def test_production_fingerprints_cover_the_skin_assets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ("public_frontend", "frontend/shared", "frontend/assets/immersive/scenes"):
                shutil.copytree(APP_ROOT / path, root / path)
            result = fingerprint(root)
            index = (root / "public_frontend/index.html").read_text(encoding="utf-8")
            app = (root / "public_frontend/app.js").read_text(encoding="utf-8")
            self.assertIn(result["assets"]["/snow-adv.css"], index)
            self.assertRegex(result["assets"]["/snow-adv.css"], r"^/snow-adv\.[0-9a-f]{16}\.css$")
            self.assertIn(result["assets"]["/snow-adv-fonts.css"], app)
            self.assertIn(result["assets"]["/modules/snow-icons.js"], app)
            self.assertIn("/shared/brand/xiaoji-icon-192.", index)
            self.assertNotIn('"/shared/brand/xiaoji-icon.png', index)


from tests import test_public_frontend_e2e as fixture  # noqa: E402


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvBrowserTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 850})
        self.page.add_init_script(PIN_LIGHT_TONE)
        self.errors = []
        self.failed = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("response", lambda response: self.failed.append(response.url) if response.status >= 400 else None)
        self.page.set_default_timeout(8000)

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

    def open(self, enabled: bool):
        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
            route.fulfill(json=payload)
        self.page.route("**/public/v1/config", configure)
        self.page.goto(self.base_url, wait_until="networkidle")

    def test_disabled_flag_leaves_the_legacy_interface_untouched(self):
        self.open(False)
        state = self.page.evaluate("""() => ({
          ui: document.documentElement.dataset.ui || null,
          fonts: document.querySelectorAll('link[data-snow-adv-fonts]').length,
          icons: document.querySelectorAll('svg.snow-icon').length,
          settings: document.getElementById('open-settings').textContent,
          send: document.querySelector('#send-message .composer-tool-icon').textContent,
          close: document.getElementById('close-info').textContent,
        })""")
        self.assertEqual(state, {"ui": None, "fonts": 0, "icons": 0, "settings": "⚙", "send": "➤", "close": "×"})
        self.assertEqual(self.errors, [])
        self.assertEqual([url for url in self.failed if "snow-adv" in url or "/assets/fonts/" in url], [])

    def test_enabled_flag_applies_tokens_icons_and_story_font(self):
        self.open(True)
        self.page.wait_for_function("document.fonts.check('16px \"LXGW WenKai Screen\"', '你好') || document.querySelector('link[data-snow-adv-fonts]')")
        state = self.page.evaluate("""() => {
          const style = getComputedStyle(document.documentElement);
          const label = button => document.getElementById(button).getAttribute('aria-label');
          return {
            ui: document.documentElement.dataset.ui,
            fonts: document.querySelectorAll('link[data-snow-adv-fonts]').length,
            ice: style.getPropertyValue('--ice-600').trim(),
            settingsIcon: Boolean(document.querySelector('#open-settings svg.snow-icon-settings')),
            settingsLabel: label('open-settings'),
            closeIcon: Boolean(document.querySelector('#close-info svg.snow-icon-x')),
            searchIcon: Boolean(document.querySelector('.contact-search svg.snow-icon-search')),
            hiddenIcons: [...document.querySelectorAll('svg.snow-icon')].every(icon => icon.getAttribute('aria-hidden') === 'true'),
            storyFont: getComputedStyle(document.getElementById('stage-speech')).fontFamily,
            smallest: Math.min(...[...document.querySelectorAll('.form-hint, .settings-tabs button, .composer-foot')]
              .map(node => parseFloat(getComputedStyle(node).fontSize))),
          };
        }""")
        self.assertEqual(state["ui"], "snow-adv")
        self.assertEqual(state["fonts"], 1)
        self.assertEqual(state["ice"], "#3a7ca5")
        self.assertTrue(state["settingsIcon"] and state["closeIcon"] and state["searchIcon"] and state["hiddenIcons"])
        self.assertEqual(state["settingsLabel"], "设置")
        self.assertIn("LXGW WenKai Screen", state["storyFont"])
        self.assertGreaterEqual(state["smallest"], 12)
        self.assertEqual(self.errors, [])
        self.assertEqual([url for url in self.failed if "snow-adv" in url or "/assets/fonts/" in url], [])


    def _reach_in_person(self):
        # Drive the proven e2e path to the in-person surface so a real scene
        # (fixture visual_key = "observation") is established.
        # P6a：先消隐开场层（桌面开机自动消隐 / 手机锁屏点击解锁），再走既有流程。
        if self.page.locator("#snow-lock").count() and self.page.locator("#snow-lock").is_visible():
            self.page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        self.page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        self.page.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        self.page.locator("#accept-experience-notice").click()
        fixture.PublicFrontendE2ETests._configure_model(self.page)
        if self.page.locator("#skip-onboarding").is_visible():
            self.page.locator("#skip-onboarding").click()
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.locator("#in-person-surface:not([hidden])").wait_for()
        self.page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)

    def test_disabled_flag_builds_no_snow_world_and_keeps_the_backdrop(self):
        self.open(False)
        self._reach_in_person()
        state = self.page.evaluate("""() => ({
          ui: document.documentElement.dataset.ui || null,
          layers: document.querySelectorAll('#scene-stage [data-snow-world]').length,
          canvases: document.querySelectorAll('#scene-stage canvas.snow-canvas').length,
          backdropShown: getComputedStyle(document.getElementById('scene-backdrop')).display !== 'none',
          backdropSrc: document.getElementById('scene-backdrop').getAttribute('src') || '',
        })""")
        self.assertEqual(state["ui"], None)
        self.assertEqual(state["layers"], 0)
        self.assertEqual(state["canvases"], 0)
        self.assertTrue(state["backdropShown"])
        self.assertIn("observation", state["backdropSrc"])
        self.assertEqual(self.errors, [])
        self.assertEqual([url for url in self.failed if "snow-adv" in url or "/modules/snow-world" in url], [])

    def test_enabled_flag_layers_the_world_hides_the_backdrop_and_matches_the_scene(self):
        self.open(True)
        self._reach_in_person()
        self.page.wait_for_function("document.querySelector('#scene-stage canvas.snow-canvas-far')")
        state = self.page.evaluate("""() => {
          const stage = document.getElementById('scene-stage');
          const far = stage.querySelector('.snow-layer-far');
          const room = stage.querySelector('.snow-layer-room');
          const art = document.getElementById('stage-character-art');
          const canvasFar = stage.querySelector('canvas.snow-canvas-far');
          const canvasNear = stage.querySelector('canvas.snow-canvas-near');
          const order = [...stage.children].map(node => node.className || node.id);
          return {
            ui: document.documentElement.dataset.ui,
            hasFar: Boolean(far), hasRoom: Boolean(room),
            hasCanvasFar: Boolean(canvasFar), hasCanvasNear: Boolean(canvasNear),
            farSvg: far ? far.querySelector('svg[data-layer="far"]') !== null : false,
            roomSvg: room ? room.querySelector('svg[data-layer="room"]') !== null : false,
            scene: far ? far.dataset.scene : null,
            backdropHidden: getComputedStyle(document.getElementById('scene-backdrop')).display === 'none',
            atmosphereHidden: getComputedStyle(document.querySelector('.snow-atmosphere')).display === 'none',
            hidden: [...stage.querySelectorAll('[data-snow-world]')].every(node => node.getAttribute('aria-hidden') === 'true'),
            canvasPx: canvasFar ? canvasFar.width * canvasFar.height : 0,
            artPresent: Boolean(art),
            order,
          };
        }""")
        self.assertEqual(state["ui"], "snow-adv")
        self.assertTrue(state["hasFar"] and state["hasRoom"] and state["hasCanvasFar"] and state["hasCanvasNear"])
        self.assertTrue(state["farSvg"] and state["roomSvg"])
        self.assertEqual(state["scene"], "observation")
        self.assertTrue(state["backdropHidden"])
        self.assertTrue(state["atmosphereHidden"])
        self.assertTrue(state["hidden"])
        self.assertGreater(state["canvasPx"], 0)
        self.assertTrue(state["artPresent"])
        self.assertEqual(self.errors, [])
        self.assertEqual([url for url in self.failed if "snow-adv" in url or "/modules/snow-world" in url], [])

    def test_motion_off_runs_no_persistent_animation_loop(self):
        # Reduce motion before the config load so syncMotionState starts the
        # world in the static tier (motionLevel = off).
        self.page.add_init_script("window.localStorage.setItem('project-snow-public:motion', 'off')")
        self.open(True)
        self._reach_in_person()
        self.page.wait_for_function("document.querySelector('#scene-stage canvas.snow-canvas-far')")
        self.page.wait_for_function("""() => {
          const probe = window.__projectSnowTest.snowProbe();
          return probe.bootDone && probe.worldScene === 'observation' && !probe.pushing
            && document.getElementById('stage-presence-status').hidden
            && document.getElementById('stage-speech').textContent.trim() === '你来了。'
            && !document.getAnimations().some(animation =>
              (animation.playState === 'running' || animation.pending)
              && Number.isFinite(animation.effect?.getComputedTiming().endTime));
        }""")
        self.page.evaluate("() => document.fonts.ready.then(() => undefined)")
        self.assertEqual(self.page.evaluate("() => document.documentElement.dataset.motionLevel"), "off")
        # With motion off there must be no persistent rAF loop repainting the
        # snow canvases.  A running loop clears + redraws every frame, so the
        # drawn pixels drift; a stopped loop leaves them byte-identical.  Sample
        # the far canvas across several animation frames and require no change.
        observation = self.page.evaluate("""async () => {
          const canvas = document.querySelector('#scene-stage canvas.snow-canvas-far');
          const ctx = canvas.getContext('2d');
          const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
          const sample = () => ctx.getImageData(0, 0, canvas.width, canvas.height).data.join(',');
          await frame();
          // Chrome can round its initial canvas readback differently without
          // any redraw. Prime it once before the strict eight-frame comparison.
          ctx.getImageData(0, 0, canvas.width, canvas.height);
          await frame();
          const first = sample();
          const loopCounts = [window.__projectSnowTest.snowProbe().activeLoops];
          for (let i = 0; i < 8; i += 1) {
            await frame();
            loopCounts.push(window.__projectSnowTest.snowProbe().activeLoops);
          }
          return {drifted: first !== sample(), loopCounts};
        }""")
        self.assertEqual(observation["loopCounts"], [0] * 9)
        self.assertFalse(observation["drifted"], "motion off must not keep repainting the snow canvas")
        self.assertEqual(self.errors, [])
        self.assertEqual([url for url in self.failed if "snow-adv" in url or "/modules/snow-world" in url], [])



@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvStageTests(TestCase):
    """P3 面对面主屏：全屏舞台、磨砂对白框、▼ 推进标记、快捷菜单、到场提示去重、立绘中轴。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors = []
        self.failed = []
        self.page = None

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

    def _page(self, width=1280, height=820):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.add_init_script(PIN_LIGHT_TONE)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("response", lambda response: self.failed.append(response.url) if response.status >= 400 else None)
        page.set_default_timeout(8000)
        self.page = page
        return page

    def _open(self, page, enabled):
        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
            route.fulfill(json=payload)
        page.route("**/public/v1/config", configure)
        page.goto(self.base_url, wait_until="networkidle")

    def _reach_in_person(self, page):
        # Drive the proven e2e path to the in-person surface and arrival reaction.
        # P6a：先消隐开场层（桌面开机自动消隐 / 手机锁屏点击解锁），再走既有流程。
        if page.locator("#snow-lock").count() and page.locator("#snow-lock").is_visible():
            page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        page.locator("#accept-experience-notice").click()
        # Wait until the scene has loaded (the presence button reads the location)
        # before opening settings, so the control is settled and actionable.
        page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
        if page.locator("#onboarding-guide:not([hidden])").count() and page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        # On a narrow viewport the contact panel is an off-screen drawer; the model
        # settings entry lives inside it, so open it before configuring.
        narrow = page.viewport_size["width"] <= 820
        if narrow and page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
            page.locator("#open-contacts").click()
            page.locator("#contact-panel.open").wait_for()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if narrow and page.locator("#contact-panel.open").count():
            # Close the drawer again so it does not cover the stage we assert on.
            if page.locator("#close-contacts").is_visible():
                page.locator("#close-contacts").click()
            page.locator("#contact-panel:not(.open)").wait_for()
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator("#in-person-surface:not([hidden])").wait_for()
        page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
        # The fixture arrival reaction speaks "你来了。"; wait until it has fully revealed.
        page.wait_for_function("document.getElementById('stage-speech')?.textContent.includes('你来了')", timeout=8000)

    def _failed_skin_requests(self):
        return [url for url in self.failed if "snow-adv" in url or "/assets/fonts/" in url or "/modules/snow-world" in url]

    def test_flag_off_keeps_legacy_stage_and_shows_quick_menu_nowhere(self):
        page = self._page()
        self._open(page, False)
        self._reach_in_person(page)
        state = page.evaluate("""() => {
          const shell = document.querySelector('.app-shell');
          const panel = document.getElementById('contact-panel');
          return {
            ui: document.documentElement.dataset.ui || null,
            quickMenu: document.querySelectorAll('#stage-dialogue .stage-quick-menu').length,
            advance: document.querySelectorAll('#stage-dialogue .stage-advance').length,
            // A permanent sidebar column: the contact panel is in normal flow, not fixed.
            panelFixed: getComputedStyle(panel).position === 'fixed',
            sidebarCollapsed: shell.classList.contains('sidebar-collapsed'),
            dialogueRadius: getComputedStyle(document.getElementById('stage-dialogue')).borderTopLeftRadius,
            bannerShown: !document.getElementById('system-banner').hidden,
            bannerText: document.getElementById('system-banner').textContent,
            speech: document.getElementById('stage-speech').textContent,
          };
        }""")
        self.assertEqual(state["ui"], None)
        self.assertEqual(state["quickMenu"], 0)
        self.assertEqual(state["advance"], 0)
        self.assertFalse(state["panelFixed"])
        self.assertFalse(state["sidebarCollapsed"])
        # Legacy arrival notices: the stage reaction AND the system banner are both shown.
        self.assertTrue(state["bannerShown"])
        self.assertIn("到来", state["bannerText"])
        self.assertIn("你来了", state["speech"])
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_on_full_width_stage_frosted_box_and_nameplate_colour(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        state = page.evaluate("""() => {
          const shell = document.querySelector('.app-shell');
          const stage = document.getElementById('scene-stage');
          const dialogue = document.getElementById('stage-dialogue');
          const nameplate = document.querySelector('#stage-dialogue .stage-speaker-line strong');
          const chatApp = document.getElementById('chat-app');
          // Resolve var(--char-deep) to a concrete rgb via a probe, since custom
          // properties can hold an unresolved color-mix() expression.
          const paint = document.createElement('canvas').getContext('2d');
          const toRgb = color => { paint.clearRect(0, 0, 1, 1); paint.fillStyle = '#000'; paint.fillStyle = color; paint.fillRect(0, 0, 1, 1);
            const [r, g, b] = paint.getImageData(0, 0, 1, 1).data; return `rgb(${r}, ${g}, ${b})`; };
          const probe = document.createElement('span');
          probe.style.color = 'var(--char-deep)';
          chatApp.appendChild(probe);
          const charDeep = toRgb(getComputedStyle(probe).color);
          probe.remove();
          const fallback = document.createElement('span');
          fallback.style.color = 'var(--char-deep)';
          document.body.appendChild(fallback);
          const defaultDeep = toRgb(getComputedStyle(fallback).color);
          fallback.remove();
          const bg = getComputedStyle(dialogue).backgroundColor;
          const bgImage = getComputedStyle(dialogue).backgroundImage;
          const panel = document.getElementById('contact-panel');
          return {
            ui: document.documentElement.dataset.ui,
            viewportWidth: window.innerWidth,
            stageWidth: Math.round(stage.getBoundingClientRect().width),
            dialogueBg: bg,
            dialogueBgImage: bgImage,
            dialogueRadius: getComputedStyle(dialogue).borderTopLeftRadius,
            nameplateBg: toRgb(getComputedStyle(nameplate).backgroundColor),
            charDeep,
            defaultDeep,
            character: chatApp.dataset.snowCharacter || '',
            panelFixed: getComputedStyle(panel).position === 'fixed',
            sidebarCollapsed: shell.classList.contains('sidebar-collapsed'),
          };
        }""")
        self.assertEqual(state["ui"], "snow-adv")
        # Full-screen stage: it spans the whole viewport width on desktop.
        self.assertEqual(state["stageWidth"], state["viewportWidth"])
        # Overlay drawer, closed by default -> the sidebar is a fixed overlay and collapsed.
        self.assertTrue(state["panelFixed"])
        self.assertTrue(state["sidebarCollapsed"])
        # Box opaque enough that art never shows through (effective alpha >= 0.92).
        alpha = _min_background_alpha(state["dialogueBg"], state["dialogueBgImage"])
        self.assertGreaterEqual(alpha, 0.92, (state["dialogueBg"], state["dialogueBgImage"]))
        # Rounded top.
        self.assertNotEqual(state["dialogueRadius"], "0px")
        # Nameplate uses the character colour var(--char-deep).
        self.assertEqual(_rgb(state["nameplateBg"]), _rgb_from_any(state["charDeep"]))
        # ...and that colour is the selected character's, not the default ice tone.
        self.assertTrue(state["character"])
        self.assertNotEqual(_rgb_from_any(state["charDeep"]), _rgb_from_any(state["defaultDeep"]))
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_on_quick_menu_invokes_existing_actions(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        # 按需加载的分页器会把「自动」放到最前；等它装好再读，结果不受加载时机影响。
        page.locator('#stage-quick-menu [data-stage-quick="auto"]').wait_for(state="attached")
        names = page.evaluate("""() => [...document.querySelectorAll('#stage-quick-menu button')]
          .map(button => button.getAttribute('aria-label'))""")
        self.assertEqual(names, ["自动", "跳过", "回看", "隐藏", "设置"])
        # 回看 opens the transcript drawer, reusing the existing handler.
        page.locator('#stage-quick-menu [data-stage-quick="backlog"]').click()
        page.locator("#transcript-panel.open").wait_for()
        page.locator("#close-transcript").click()
        page.locator("#transcript-panel:not(.open)").wait_for()
        # 隐藏 hides the stage UI; the existing restore control + H key bring it back.
        page.locator('#stage-quick-menu [data-stage-quick="hide"]').click()
        page.locator("#in-person-surface.ui-hidden").wait_for()
        self.assertFalse(page.locator("#restore-stage-ui").is_hidden())
        page.locator("#restore-stage-ui").click()
        page.locator("#in-person-surface:not(.ui-hidden)").wait_for()
        # H key toggles hide again (reuses the existing shortcut).
        page.keyboard.press("h")
        page.locator("#in-person-surface.ui-hidden").wait_for()
        page.keyboard.press("h")
        page.locator("#in-person-surface:not(.ui-hidden)").wait_for()
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_on_advance_marker_shows_when_idle_and_hides_while_revealing(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        # After the reply finished revealing, the advance marker is ready (visible).
        page.wait_for_function("document.getElementById('stage-advance')?.classList.contains('is-ready')", timeout=8000)
        # Let the 200ms reveal transition settle before reading the painted opacity.
        page.wait_for_function(
            "Number(getComputedStyle(document.getElementById('stage-advance')).opacity) > 0.5", timeout=4000)
        ready = page.evaluate("""() => {
          const advance = document.getElementById('stage-advance');
          return { ready: advance.classList.contains('is-ready'), opacity: Number(getComputedStyle(advance).opacity), aria: advance.getAttribute('aria-hidden') };
        }""")
        self.assertTrue(ready["ready"])
        self.assertGreater(ready["opacity"], 0.5)
        self.assertEqual(ready["aria"], "true")
        # While a fresh reply is revealing, the marker hides again. Drive a chat turn.
        page.locator("#message-input").fill("再说一句")
        page.locator("#send-message").click()
        hid = page.wait_for_function(
            "!document.getElementById('stage-advance').classList.contains('is-ready')", timeout=8000)
        self.assertTrue(hid)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_on_arrival_shows_one_visible_notice(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        state = page.evaluate("""() => ({
          bannerShown: !document.getElementById('system-banner').hidden,
          statusText: document.getElementById('request-status').textContent.trim(),
          narration: document.getElementById('stage-narration').textContent,
          speech: document.getElementById('stage-speech').textContent,
        })""")
        # The duplicate banner is suppressed; the stage dialogue is the single notice.
        self.assertFalse(state["bannerShown"])
        self.assertIn("你来了", state["speech"])
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_on_character_art_axis_is_centred(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        page.wait_for_function(
            "document.getElementById('stage-character-art') && !document.getElementById('stage-character-art').hidden"
            " && document.getElementById('stage-character-art').dataset.actorAnchor", timeout=8000)
        offset = page.evaluate("""() => {
          const stage = document.getElementById('scene-stage');
          const art = document.getElementById('stage-character-art');
          const anchor = Number(art.dataset.actorAnchor);
          const stageRect = stage.getBoundingClientRect();
          const artRect = art.getBoundingClientRect();
          // The body axis sits at `anchor` across the art's own width; it should land on
          // the stage centre line.
          const axisX = artRect.left + artRect.width * anchor;
          const stageCentre = stageRect.left + stageRect.width / 2;
          return { delta: Math.abs(axisX - stageCentre), anchor };
        }""")
        self.assertLessEqual(offset["delta"], 4, offset)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_on_mobile_quick_menu_collapses_to_icons_with_names(self):
        page = self._page(390, 844)
        self._open(page, True)
        self._reach_in_person(page)
        page.locator('#stage-quick-menu [data-stage-quick="auto"]').wait_for(state="attached")
        state = page.evaluate("""() => {
          const buttons = [...document.querySelectorAll('#stage-quick-menu button')];
          const input = document.getElementById('message-input').getBoundingClientRect();
          const menu = document.getElementById('stage-quick-menu').getBoundingClientRect();
          const smallest = Math.min(...[...document.querySelectorAll('#stage-dialogue, #stage-speech, #stage-quick-menu button')]
            .map(node => parseFloat(getComputedStyle(node).fontSize)));
          return {
            names: buttons.map(button => button.getAttribute('aria-label')),
            // Icon-only: visible label text is clipped (sr-only), width <= an icon pill.
            labelsHidden: buttons.every(button => {
              const label = button.querySelector('.stage-quick-label');
              return label && getComputedStyle(label).position === 'absolute';
            }),
            overlapsInput: menu.bottom > input.top && menu.top < input.bottom && menu.right > input.left && menu.left < input.right,
            smallest,
          };
        }""")
        self.assertEqual(state["names"], ["自动", "跳过", "回看", "隐藏", "设置"])
        self.assertTrue(state["labelsHidden"])
        self.assertFalse(state["overlapsInput"])
        self.assertGreaterEqual(state["smallest"], 12)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])


def _rgb(value):
    numbers = re.findall(r"[\d.]+", value or "")
    return tuple(round(float(number)) for number in numbers[:3])


def _rgb_from_any(value):
    value = (value or "").strip()
    if value.startswith("#"):
        hexv = value[1:]
        if len(hexv) == 3:
            hexv = "".join(ch * 2 for ch in hexv)
        return tuple(int(hexv[index:index + 2], 16) for index in (0, 2, 4))
    return _rgb(value)


def _min_background_alpha(background_color, background_image):
    # The frosted box paints its fill through a linear-gradient, so the opacity
    # lives in background-image's colour stops, not background-color. Prefer the
    # gradient stops; only an element with no gradient uses background-color
    # (where a transparent rgba(0,0,0,0) merely means "colour is in the image").
    def _alphas(source):
        found = []
        for match in re.finditer(r"rgba?\(([^)]*)\)", source or ""):
            numbers = re.findall(r"[\d.]+", match.group(1))
            found.append(float(numbers[3]) if len(numbers) >= 4 else 1.0)
        return found
    image_alphas = _alphas(background_image)
    if image_alphas:
        return min(image_alphas)
    color_alphas = _alphas(background_color)
    return min(color_alphas) if color_alphas else 1.0


# --------------------------------------------------------------------------- P4
# 演出与转场：换表情不出现透明空档、立绘呼吸按档位、白雪转场遮罩、无页面错误。
# 立绘换帧用与 test_stage_presentation_timing.py 相同的做法：给角色装一份带不同表情
# 图的清单，再直接调用 __projectSnowTest.updateStageCharacterArt 触发换帧。
from hashlib import sha256 as _sha256  # noqa: E402
from io import BytesIO as _BytesIO  # noqa: E402
from urllib.parse import urlparse as _urlparse  # noqa: E402

_P4_CHARACTER = "25b23cb64398"


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvPerformanceTests(TestCase):
    """P4 面对面演出：换表情无透明空档、呼吸、动作、白雪转场遮罩。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        from PIL import Image

        self.images = {}
        for index, name in enumerate(("neutral", "happy", "thinking")):
            image = Image.new("RGBA", (8, 10), (30 + index * 60, 160 - index * 30, 80 + index * 40, 255))
            buffer = _BytesIO()
            image.save(buffer, format="PNG")
            self.images[name] = buffer.getvalue()
        manifest = fixture.PublicFrontendHandler._expression_manifest(_P4_CHARACTER)
        manifest.pop("presentations", None)
        manifest.pop("performances", None)
        for state_name, item in manifest["expressions"].items():
            name = state_name if state_name in ("happy", "thinking") else "neutral"
            item.pop("presentation_id", None)
            item.update(stage_asset_path=f"/p4art/{name}.png", stage_asset_sha256=_sha256(self.images[name]).hexdigest())
        self.manifest_bytes = json.dumps(manifest).encode()
        self.character = {
            "character_id": _P4_CHARACTER,
            "expression_manifest_url": f"/p4art/{_P4_CHARACTER}/manifest.json",
            "expression_manifest_sha256": _sha256(self.manifest_bytes).hexdigest(),
        }
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors = []
        self.failed = []
        self.page = None

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

    def _page(self, width=1280, height=820, reduced_motion=None):
        page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion=reduced_motion)
        page.add_init_script(PIN_LIGHT_TONE)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("response", lambda response: self.failed.append(response.url) if response.status >= 400 else None)
        page.set_default_timeout(8000)
        self.page = page
        return page

    def _open(self, page, enabled, motion=None):
        if motion is not None:
            page.add_init_script(f"window.localStorage.setItem('project-snow-public:motion', {json.dumps(motion)})")

        def route_fixture(route):
            path = _urlparse(route.request.url).path
            if path == "/public/v1/config":
                payload = route.fetch().json()
                payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
                route.fulfill(json=payload)
            elif path == "/public/v1/characters":
                payload = route.fetch().json()
                for character in payload["characters"]:
                    if character["character_id"] == _P4_CHARACTER:
                        character.update(self.character)
                route.fulfill(json=payload)
            elif path.startswith("/p4art/"):
                name = path.rsplit("/", 1)[-1]
                if name == "manifest.json":
                    route.fulfill(content_type="application/json", body=self.manifest_bytes)
                else:
                    route.fulfill(content_type="image/png", body=self.images[name.removesuffix(".png")])
            else:
                route.continue_()

        page.route("**/*", route_fixture)
        page.goto(self.base_url, wait_until="networkidle")

    def _reach_in_person(self, page):
        # P6a：先消隐开场层（桌面开机自动消隐 / 手机锁屏点击解锁），再走既有流程。
        if page.locator("#snow-lock").count() and page.locator("#snow-lock").is_visible():
            page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        page.locator("#accept-experience-notice").click()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator("#in-person-surface:not([hidden])").wait_for()
        page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
        page.wait_for_function(
            "document.getElementById('stage-character-art') && !document.getElementById('stage-character-art').hidden"
            " && document.getElementById('stage-character-art').getAttribute('src')", timeout=8000)

    def _failed_skin_requests(self):
        return [url for url in self.failed if "snow-adv" in url or "/assets/fonts/" in url
                or "/modules/snow-world" in url or "/p4art/" in url]

    def _swap_and_sample(self, page):
        # Swap neutral -> happy and sample both art nodes' opacity across the
        # transition. Returns the minimum opacity seen on the OUTGOING (old) art
        # and whether the incoming rose from < 1 to 1, plus the final node count.
        # The first-entry animation also owns opacity; finish it before measuring
        # an expression swap. Do not await every animation (breathing is infinite).
        page.wait_for_function("""() => {
          const art = document.getElementById('stage-character-art');
          return art && !art.dataset.stageEnter && !art.dataset.stageArtTransition
            && !document.querySelector('.stage-character-art-outgoing');
        }""", timeout=5000)
        return page.evaluate(
            """async character => {
          const art = document.getElementById('stage-character-art');
          const previous = art.getAttribute('src');
          const samples = { outgoingMin: 1, incomingMin: 1, incomingMax: 0, bothSemi: false };
          const sample = () => {
            const outgoing = document.querySelector('.stage-character-art-outgoing');
            const inOpacity = Number(getComputedStyle(art).opacity);
            samples.incomingMin = Math.min(samples.incomingMin, inOpacity);
            samples.incomingMax = Math.max(samples.incomingMax, inOpacity);
            if (outgoing) {
              const outOpacity = Number(getComputedStyle(outgoing).opacity);
              samples.outgoingMin = Math.min(samples.outgoingMin, outOpacity);
              // The forbidden state: BOTH visible art nodes semi-transparent at once.
              if (outOpacity < 0.95 && inOpacity < 0.95) samples.bothSemi = true;
            }
          };
          await window.__projectSnowTest.updateStageCharacterArt(art, character, 'happy');
          // updateStageCharacterArt resolves when the decoded art is committed,
          // before WAAPI finishes. A wall-clock delay can expire before rendering
          // starts on a busy runner, so sample through the actual cleanup instead.
          await new Promise((resolve, reject) => {
            let frame = 0;
            const deadline = setTimeout(() => {
              cancelAnimationFrame(frame);
              reject(new Error('stage art swap did not settle: ' + JSON.stringify(samples)));
            }, 5000);
            const probe = () => {
              sample();
              if (!art.dataset.stageArtTransition && !document.querySelector('.stage-character-art-outgoing')) {
                // Include a real rendered terminal frame after cleanup removed
                // fill:both animations, rather than inferring opacity from time.
                frame = requestAnimationFrame(() => { sample(); clearTimeout(deadline); resolve(); });
              } else frame = requestAnimationFrame(probe);
            };
            probe();
          });
          samples.changed = art.getAttribute('src') !== previous;
          samples.state = art.dataset.expressionState;
          samples.artNodes = document.querySelectorAll(
            '#scene-stage .stage-character-art, #scene-stage .stage-character-art-outgoing').length;
          return samples;
        }""",
            self.character,
        )

    def test_expression_swap_under_flag_never_dips_transparent(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        samples = self._swap_and_sample(page)
        self.assertTrue(samples["changed"])
        self.assertEqual(samples["state"], "happy")
        # Outgoing (old) art stays fully opaque the whole time; the two art nodes
        # are never both semi-transparent, so no mid-swap transparency dip.
        self.assertGreaterEqual(samples["outgoingMin"], 0.95, samples)
        self.assertFalse(samples["bothSemi"], samples)
        # Incoming genuinely rose from below 1 up to 1 (a real fade-in on top).
        self.assertLess(samples["incomingMin"], 0.5, samples)
        self.assertGreaterEqual(samples["incomingMax"], 0.99, samples)
        # Exactly one art node remains after the swap settles.
        self.assertEqual(samples["artNodes"], 1, samples)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_expression_swap_flag_off_keeps_legacy_crossfade(self):
        page = self._page(1280, 820)
        self._open(page, False)
        self._reach_in_person(page)
        samples = self._swap_and_sample(page)
        self.assertTrue(samples["changed"])
        self.assertEqual(samples["state"], "happy")
        # Legacy behaviour: the outgoing art cross-fades out (drops toward 0).
        self.assertLess(samples["outgoingMin"], 0.5, samples)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_expression_swap_samples_until_delayed_animation_finishes(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        # Model a renderer whose animation timeline starts after the old 360 ms
        # sampling window. Keep the real WAAPI fade and its finished/cleanup path.
        page.evaluate("""() => {
          const animate = Element.prototype.animate;
          window.__delayedSwapAnimations = 0;
          Element.prototype.animate = function(frames, options) {
            const animation = animate.call(this, frames, options);
            if (this.id === 'stage-character-art' && this.dataset.stageArtTransition === 'incoming') {
              window.__delayedSwapAnimations += 1;
              animation.pause();
              setTimeout(() => animation.play(), 500);
            }
            return animation;
          };
        }""")
        samples = self._swap_and_sample(page)
        self.assertEqual(page.evaluate("window.__delayedSwapAnimations"), 1)
        self.assertEqual(samples["artNodes"], 1, samples)
        self.assertTrue(samples["changed"])
        self.assertEqual(samples["state"], "happy")
        self.assertGreaterEqual(samples["outgoingMin"], 0.95, samples)
        self.assertFalse(samples["bothSemi"], samples)
        self.assertLess(samples["incomingMin"], 0.5, samples)
        self.assertGreaterEqual(samples["incomingMax"], 0.99, samples)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def _breathing(self, page):
        return page.evaluate(
            """() => {
          const art = document.getElementById('stage-character-art');
          const names = art.getAnimations().map(a => a.animationName || (a.effect && a.effect.getKeyframes && 'js'));
          const cssBreath = art.getAnimations().some(a => a.animationName === 'snow-actor-breath');
          return { cssBreath, animationName: getComputedStyle(art).animationName, names };
        }""")

    def test_breathing_present_at_auto_absent_at_light_and_off(self):
        # auto: breathing animation is running on the stage art.
        page = self._page(1280, 820, reduced_motion="no-preference")
        self._open(page, True, motion="auto")
        self._reach_in_person(page)
        page.wait_for_function(
            "document.getElementById('stage-character-art').dataset.actorAnchor", timeout=8000)
        auto = self._breathing(page)
        self.assertIn("snow-actor-breath", auto["animationName"], auto)
        page.close()
        # light: no breathing.
        page = self._page(1280, 820, reduced_motion="no-preference")
        self._open(page, True, motion="light")
        self._reach_in_person(page)
        page.wait_for_function(
            "document.getElementById('stage-character-art').dataset.actorAnchor", timeout=8000)
        light = self._breathing(page)
        self.assertEqual(light["animationName"].strip(), "none", light)
        page.close()
        # off: no breathing.
        page = self._page(1280, 820, reduced_motion="no-preference")
        self._open(page, True, motion="off")
        self._reach_in_person(page)
        page.wait_for_function(
            "document.getElementById('stage-character-art').dataset.actorAnchor", timeout=8000)
        off = self._breathing(page)
        self.assertEqual(off["animationName"].strip(), "none", off)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def _veil_state(self, page):
        # Drive the existing scene-transition overlay to a running phase (a
        # presentation-only probe; it changes no logic) and read how the veil
        # renders: story-font title, footprints hidden, and transition durations.
        return page.evaluate(
            """() => {
          const layer = document.getElementById('scene-transition-layer');
          document.getElementById('transition-title').textContent = '观景区';
          document.getElementById('transition-detail').textContent = '正在建立新的场景';
          layer.hidden = false;
          layer.dataset.phase = 'arriving';
          const title = document.getElementById('transition-title');
          const footsteps = layer.querySelector('.transition-footsteps');
          const dots = layer.querySelector('.transition-dots');
          const dur = node => (getComputedStyle(node).transitionDuration + ' ' + getComputedStyle(node).animationDuration)
            .split(/[ ,]+/).map(parseFloat).filter(v => !Number.isNaN(v));
          return {
            titleFont: getComputedStyle(title).fontFamily,
            titleText: title.textContent,
            footstepsHidden: getComputedStyle(footsteps).display === 'none',
            dotsHidden: getComputedStyle(dots).display === 'none',
            layerDurations: dur(layer),
            cardDurations: dur(layer.querySelector('.transition-card')),
          };
        }""")

    def test_scene_transition_veil_shows_story_title_and_hides_footprints(self):
        page = self._page(1280, 820)
        self._open(page, True, motion="auto")
        self._reach_in_person(page)
        veil = self._veil_state(page)
        self.assertIn("LXGW WenKai Screen", veil["titleFont"], veil)
        self.assertEqual(veil["titleText"], "观景区")
        self.assertTrue(veil["footstepsHidden"], veil)
        self.assertTrue(veil["dotsHidden"], veil)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_scene_transition_veil_off_has_no_animated_durations(self):
        page = self._page(1280, 820)
        self._open(page, True, motion="off")
        self._reach_in_person(page)
        veil = self._veil_state(page)
        # At motion off there must be no CSS transition/animation durations > 0 on the veil layers.
        self.assertTrue(all(value == 0 for value in veil["layerDurations"]), veil)
        self.assertTrue(all(value == 0 for value in veil["cardDurations"]), veil)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])



# --------------------------------------------------------------------------- P5
# 通讯器与其余界面：联系人卡片 per-row 角色色、气泡 pop-in 按档位、移动端 placeholder
# 单行与 HUD 名字不截断、文字通讯/设置/公告弹窗全部 ≥12px、隐私页仅在 flag 开启时皮肤化。


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvCommunicatorTests(TestCase):
    """P5 通讯器与其余界面：联系人卡片、气泡 pop-in、移动端修复、最小字号、隐私页。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors = []
        self.failed = []
        self.page = None

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

    def _page(self, width=1280, height=820):
        page = self.browser.new_page(viewport={"width": width, "height": height})
        page.add_init_script(PIN_LIGHT_TONE)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("response", lambda response: self.failed.append(response.url) if response.status >= 400 else None)
        page.set_default_timeout(8000)
        self.page = page
        return page

    def _open(self, page, enabled, motion=None):
        if motion is not None:
            page.add_init_script(f"window.localStorage.setItem('project-snow-public:motion', {json.dumps(motion)})")

        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
            route.fulfill(json=payload)

        page.route("**/public/v1/config", configure)
        page.goto(self.base_url, wait_until="networkidle")

    def _failed_skin_requests(self):
        return [url for url in self.failed if "snow-adv" in url or "/assets/fonts/" in url or "/modules/snow-world" in url]

    def _reach_in_person(self, page):
        # P6a：先消隐开场层（手机锁屏/桌面开机），再走既有须知/面对面流程。
        self._dismiss_snow_opening(page)
        # Mirror the proven P3 path to the in-person surface and arrival reaction.
        page.locator("#accept-experience-notice").click()
        page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
        if page.locator("#onboarding-guide:not([hidden])").count() and page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        narrow = page.viewport_size["width"] <= 820
        if narrow and page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
            page.locator("#open-contacts").click()
            page.locator("#contact-panel.open").wait_for()
        fixture.PublicFrontendE2ETests._configure_model(page)
        if narrow and page.locator("#contact-panel.open").count():
            if page.locator("#close-contacts").is_visible():
                page.locator("#close-contacts").click()
            page.locator("#contact-panel:not(.open)").wait_for()
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator("#in-person-surface:not([hidden])").wait_for()
        page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)

    def _dismiss_snow_opening(self, page):
        # P6a：消隐开场层——手机锁屏点击解锁、桌面开机等待自动消隐；关闭开关时二者都不存在。
        if page.locator("#snow-lock").count() and page.locator("#snow-lock").is_visible():
            page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)

    def _dismiss_notice(self, page):
        self._dismiss_snow_opening(page)
        if page.locator("#accept-experience-notice").is_visible():
            page.locator("#accept-experience-notice").click()
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()

    def test_contact_cards_carry_per_row_character_colour(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._dismiss_notice(page)
        page.locator(".character-row[data-snow-character]").first.wait_for()
        rows = page.evaluate(
            """() => {
          const paint = document.createElement('canvas').getContext('2d');
          const toRgb = color => { paint.fillStyle = '#000'; paint.fillStyle = color; paint.fillRect(0, 0, 1, 1);
            const [r, g, b] = paint.getImageData(0, 0, 1, 1).data; return `${r},${g},${b}`; };
          return [...document.querySelectorAll('.character-row[data-snow-character]')].slice(0, 4).map(row => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--char)';
            row.appendChild(probe);
            const colour = toRgb(getComputedStyle(probe).color);
            probe.remove();
            return { id: row.dataset.snowCharacter, colour };
          });
        }""")
        self.assertGreaterEqual(len(rows), 2)
        # Every row carries a resolved --char, and at least two differ (per-row colour).
        self.assertTrue(all(row["id"] and row["colour"] for row in rows))
        self.assertGreater(len({row["colour"] for row in rows}), 1)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_contact_rows_have_no_colour_attribute_when_flag_off(self):
        page = self._page(1280, 820)
        self._open(page, False)
        self._dismiss_notice(page)
        page.locator(".character-row").first.wait_for()
        count = page.evaluate("() => document.querySelectorAll('.character-row[data-snow-character]').length")
        self.assertEqual(count, 0)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_bubble_pop_in_present_at_auto_and_absent_at_off(self):
        # The pop-in is a CSS keyframe animation gated by motion level; probe it
        # on a representative .message node under the skinned chat shell.
        def animation_name(motion):
            page = self._page(1280, 820)
            self._open(page, True, motion=motion)
            self._dismiss_notice(page)
            name = page.evaluate(
                """() => {
              const timeline = document.getElementById('timeline');
              const node = document.createElement('article');
              node.className = 'message assistant sent is-snow-pop';
              node.innerHTML = '<div class="message-avatar"></div><div class="message-body">'
                + '<div class="message-content-stack"><div class="content-message">你好</div></div></div>';
              timeline.appendChild(node);
              const value = getComputedStyle(node).animationName;
              node.remove();
              return value;
            }""")
            errors = list(self.errors)
            failed = self._failed_skin_requests()
            page.close()
            return name, errors, failed

        auto_name, auto_errors, auto_failed = animation_name("auto")
        self.assertIn("snow-bubble-pop", auto_name)
        self.assertEqual(auto_errors, [])
        self.assertEqual(auto_failed, [])

        off_name, off_errors, off_failed = animation_name("off")
        self.assertEqual(off_name.strip(), "none")
        self.assertEqual(off_errors, [])
        self.assertEqual(off_failed, [])

    def test_mobile_placeholder_single_line_and_hud_name_not_truncated(self):
        page = self._page(390, 844)
        self._open(page, True)
        self._reach_in_person(page)
        # The in-person composer placeholder is the long "说些什么，也可只填写动作……".
        page.wait_for_function(
            "document.getElementById('message-input').placeholder.includes('说些什么')", timeout=8000)
        state = page.evaluate(
            """() => {
          const input = document.getElementById('message-input');
          const style = getComputedStyle(input, '::placeholder');
          const name = document.getElementById('stage-character-name');
          return {
            placeholder: input.placeholder,
            // Single-line textarea: content box does not overflow its own height.
            fitsOneLine: input.scrollHeight <= input.clientHeight + 2,
            placeholderNowrap: style.whiteSpace,
            nameText: name.textContent,
            // The HUD name is fully shown (not clipped): scrollWidth <= clientWidth (+1 rounding).
            nameNotTruncated: name.scrollWidth <= name.clientWidth + 1,
          };
        }""")
        self.assertIn("说些什么", state["placeholder"])
        self.assertTrue(state["fitsOneLine"], state)
        self.assertGreaterEqual(len(state["nameText"]), 3)
        self.assertTrue(state["nameNotTruncated"], state)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_all_visible_text_is_at_least_twelve_pixels(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._dismiss_notice(page)
        page.locator(".character-row").first.wait_for()

        def smallest(selector_scope):
            return page.evaluate(
                """scope => {
              const root = scope ? document.querySelector(scope) : document.body;
              if (!root) return 999;
              const nodes = [...root.querySelectorAll('*')].filter(node => {
                if (node.closest('[hidden]') || node.hidden) return false;
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                const text = [...node.childNodes].some(child => child.nodeType === 3 && child.textContent.trim());
                return text;
              });
              return nodes.length ? Math.min(...nodes.map(node => parseFloat(getComputedStyle(node).fontSize))) : 999;
            }""",
                selector_scope,
            )

        # Text channel (contact list + chat shell visible text).
        self.assertGreaterEqual(smallest("#text-surface"), 12)
        self.assertGreaterEqual(smallest("#contact-panel"), 12)
        # Settings dialog.
        page.locator("#open-settings").click()
        page.locator("#settings-dialog[open]").wait_for()
        self.assertGreaterEqual(smallest("#settings-dialog"), 12)
        page.locator('#settings-dialog [data-close-dialog="settings-dialog"]').click()
        # Announcements dialog.
        page.locator("#open-announcements").click()
        page.locator("#announcements-dialog[open]").wait_for()
        self.assertGreaterEqual(smallest("#announcements-dialog"), 12)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def _open_privacy(self, enabled):
        page = self._page(1280, 820)

        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
            route.fulfill(json=payload)

        def attributions(route):
            route.fulfill(json={"avatars": [], "stickers": [], "knowledge_data": {}})

        page.route("**/public/v1/config", configure)
        page.route("**/public/v1/attributions", attributions)
        page.goto(f"{self.base_url}/privacy/", wait_until="networkidle")
        return page

    def test_privacy_page_skinned_and_attribution_only_when_flag_on(self):
        page = self._open_privacy(True)
        page.wait_for_function("document.documentElement.dataset.ui === 'snow-adv'")
        state = page.evaluate(
            """() => ({
          ui: document.documentElement.dataset.ui,
          attributionVisible: (() => { const node = document.querySelector('.snow-adv-attribution');
            return node ? getComputedStyle(node).display !== 'none' : false; })(),
          heroFont: getComputedStyle(document.querySelector('.privacy-hero h1')).fontFamily,
        })""")
        self.assertEqual(state["ui"], "snow-adv")
        self.assertTrue(state["attributionVisible"])
        self.assertIn("LXGW WenKai Screen", state["heroFont"])
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_privacy_page_unskinned_and_attribution_hidden_when_flag_off(self):
        page = self._open_privacy(False)
        # Give privacy.js a chance to run its config fetch before asserting.
        page.wait_for_function("document.querySelector('.snow-adv-attribution') !== null")
        state = page.evaluate(
            """() => ({
          ui: document.documentElement.dataset.ui || null,
          attributionVisible: (() => { const node = document.querySelector('.snow-adv-attribution');
            return node ? getComputedStyle(node).display !== 'none' : false; })(),
        })""")
        self.assertIsNone(state["ui"])
        self.assertFalse(state["attributionVisible"])
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])



@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvOpeningTests(TestCase):
    """P6a 开场与通讯器世界层：桌面开机 + 首次入室镜头推进、手机锁屏、首帧防闪、flag-off。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        fixture.PublicFrontendE2ETests.setUp(self)
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors = []
        self.failed = []
        self.page = None

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()

    def _page(self, width=1280, height=820, reduced_motion=None):
        page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion=reduced_motion)
        page.add_init_script(PIN_LIGHT_TONE)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("response", lambda response: self.failed.append(response.url) if response.status >= 400 else None)
        page.set_default_timeout(8000)
        self.page = page
        return page

    def _open(self, page, enabled, motion=None, init_script=None, wait_until="networkidle"):
        if motion is not None:
            page.add_init_script(f"window.localStorage.setItem('project-snow-public:motion', {json.dumps(motion)})")
        if init_script:
            page.add_init_script(init_script)

        def configure(route):
            payload = route.fetch().json()
            payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
            route.fulfill(json=payload)

        page.route("**/public/v1/config", configure)
        page.goto(self.base_url, wait_until=wait_until)

    def _enable_statistics(self, page):
        # Exercise the real consent UI with a synthetic, local collector only.
        config = {"enabled": True, "app": "project_snow", "endpoint": self.base_url + "/analytics/v1/events", "paths": ["/"]}
        page.route("**/statistics/config.mjs", lambda route: route.fulfill(
            content_type="text/javascript", body="export default " + json.dumps(config)))
        events = []

        def receive(route):
            events.append(route.request.post_data)
            route.fulfill(status=202, content_type="application/json", body='{"accepted":1}')

        page.route("**/analytics/v1/events", receive)
        return events

    def _assert_statistics_waits_for_opening(self, page):
        notice = page.locator("#snow-statistics-notice")
        notice.wait_for(state="attached")
        self.assertFalse(notice.is_visible(), "Consent notice must not cover the opening")
        self.assertFalse(notice.evaluate("node => node.hidden"), "Opening must not change the consent UI state")
        self.assertFalse(notice.evaluate("""node => {
          for (const control of node.querySelectorAll('button, a')) {
            control.focus();
            if (document.activeElement === control) return true;
          }
          return false;
        }"""), "Hidden consent controls must not receive focus")
        self.assertIsNone(page.evaluate("localStorage.getItem('snow.statistics.v1.project_snow.consent')"))
        return notice.element_handle()

    def _assert_statistics_resumes_unchanged(self, page, original, events):
        # Fresh visitors still finish the existing modal/onboarding sequence
        # before interacting with this non-modal consent panel.
        page.locator("#accept-experience-notice").wait_for(state="visible")
        page.locator("#accept-experience-notice").click()
        page.locator("#skip-onboarding").wait_for(state="visible")
        page.locator("#skip-onboarding").click()
        notice = page.locator("#snow-statistics-notice")
        notice.wait_for(state="visible")
        self.assertTrue(original.evaluate("node => node === document.getElementById('snow-statistics-notice')"))
        self.assertIsNone(page.evaluate("localStorage.getItem('snow.statistics.v1.project_snow.consent')"))
        self.assertEqual(events, [])
        decline = notice.get_by_role("button", name="暂不允许", exact=True)
        decline.focus()
        self.assertTrue(decline.evaluate("node => node === document.activeElement"))
        decline.click()
        self.assertFalse(notice.is_visible())
        self.assertLess(page.evaluate("Number(localStorage.getItem('snow.statistics.v1.project_snow.consent'))"), 0)
        self.assertEqual(events, [])
        self.assertEqual(self.errors, [])

    def test_statistics_consent_waits_for_desktop_boot_then_resumes(self):
        page = self._page(1280, 820)
        events = self._enable_statistics(page)
        held = []
        # Keep the actual boot active by holding its synthetic data dependency,
        # rather than sleeping or replacing any opening lifecycle code.
        page.route("**/public/v1/characters", lambda route: held.append(route))
        self._open(page, True, wait_until="domcontentloaded")
        try:
            page.locator("#snow-boot").wait_for(state="visible")
            original = self._assert_statistics_waits_for_opening(page)
            self.assertEqual(len(held), 1)
        finally:
            for route in held:
                route.continue_()
        page.locator("#snow-boot").wait_for(state="detached")
        self._assert_statistics_resumes_unchanged(page, original, events)

    def test_statistics_consent_waits_for_phone_unlock_then_resumes(self):
        page = self._page(390, 844)
        events = self._enable_statistics(page)
        self._open(page, True)
        lock = page.locator("#snow-lock")
        lock.wait_for(state="visible")
        original = self._assert_statistics_waits_for_opening(page)
        lock.focus()
        page.keyboard.press("Tab")
        self.assertTrue(page.evaluate("Boolean(document.activeElement.closest('#snow-lock'))"))
        lock.focus()
        page.keyboard.press("Enter")
        lock.wait_for(state="detached")
        self._assert_statistics_resumes_unchanged(page, original, events)

    def test_statistics_consent_flag_off_stays_visible_and_usable(self):
        page = self._page(1280, 820)
        events = self._enable_statistics(page)
        self._open(page, False)
        self.assertEqual(page.locator("#snow-boot, #snow-lock").count(), 0)
        original = page.locator("#snow-statistics-notice").element_handle()
        self._assert_statistics_resumes_unchanged(page, original, events)

    def _failed_skin_requests(self):
        return [url for url in self.failed if "snow-adv" in url or "/assets/fonts/" in url or "/modules/snow-world" in url]

    # 适配 P1–P5 flag-on 流程：先消隐 P6a 开机/锁屏层，再进入既有须知/引导/面对面流程。
    def _dismiss_opening(self, page):
        # 手机锁屏：点击任意处解锁；桌面开机：等待自动消隐。
        if page.locator("#snow-lock").count() and page.locator("#snow-lock").is_visible():
            page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)

    def _dismiss_notice(self, page):
        if page.locator("#accept-experience-notice").is_visible():
            page.locator("#accept-experience-notice").click()
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()

    def _reach_in_person(self, page):
        self._dismiss_opening(page)
        self._dismiss_notice(page)
        fixture.PublicFrontendE2ETests._configure_model(page)
        if page.locator("#skip-onboarding").is_visible():
            page.locator("#skip-onboarding").click()
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator("#in-person-surface:not([hidden])").wait_for()
        page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)

    # ---- 桌面开机 + 通讯器浮于可见外景世界 ----
    def test_desktop_shows_boot_then_communicator_over_exterior_world(self):
        page = self._page(1280, 820)
        self._open(page, True)
        # 开机层出现并自动消隐（夹具加载极快，可能在断言前已消隐；用探针确认开机确曾运行）。
        page.wait_for_function("window.__projectSnowTest && window.__projectSnowTest.snowProbe().bootDone === true", timeout=8000)
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        self._dismiss_notice(page)
        # 通讯器背后世界：body 级 #snow-backdrop 可见（非零矩形、不在 hidden/display:none 祖先里），
        # 显示 generic 外景，canvas 有像素；#chat-app data-snow-world=exterior；只有一个世界在动。
        page.wait_for_function(
            "window.__projectSnowTest.snowProbe().backdropScene === 'generic'"
            " && document.querySelector('#snow-backdrop canvas.snow-canvas-far')", timeout=8000)
        state = page.evaluate("""() => {
          const n = document.getElementById('snow-backdrop');
          const rect = n ? n.getBoundingClientRect() : null;
          const canvasFar = n ? n.querySelector('canvas.snow-canvas-far') : null;
          const farSvg = n ? n.querySelector('.snow-layer-far svg[data-layer="far"]') : null;
          const probe = window.__projectSnowTest.snowProbe();
          return {
            present: Boolean(n),
            visibleRect: Boolean(rect && rect.width > 0 && rect.height > 0),
            inHidden: Boolean(n && n.closest('[hidden]')),
            display: n ? getComputedStyle(n).display : null,
            canvasPx: canvasFar ? canvasFar.width * canvasFar.height : 0,
            hasFarSvg: Boolean(farSvg),
            world: document.getElementById('chat-app').dataset.snowWorld,
            backdropScene: probe.backdropScene,
            beenInPerson: probe.beenInPerson,
            activeLoops: probe.activeLoops,
          };
        }""")
        self.assertTrue(state["present"] and state["visibleRect"])
        self.assertFalse(state["inHidden"])
        self.assertNotEqual(state["display"], "none")
        self.assertGreater(state["canvasPx"], 0)
        self.assertTrue(state["hasFarSvg"])
        self.assertEqual(state["world"], "exterior")
        self.assertEqual(state["backdropScene"], "generic")
        self.assertFalse(state["beenInPerson"])
        self.assertEqual(state["activeLoops"], 1)  # 只有背后世界在动
        # 像素证据：从 timeline 空白区域截图采样若干点，颜色不应全相同（证明背后雪景透出，
        # 而非 P5 的纯色平背景）——用 variance / 极差 > 阈值判定。
        shot = page.locator("#timeline").screenshot()
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(shot)).convert("RGB")
        w, h = img.size
        samples = [img.getpixel((int(w * fx), int(h * fy))) for fx in (0.2, 0.5, 0.8) for fy in (0.3, 0.6, 0.85)]
        reds = [p[0] for p in samples]
        self.assertGreater(max(reds) - min(reds), 6, f"timeline 背后应有雪景渐变/雪花，采样却近乎纯色: {samples}")
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_same_day_revisit_skips_boot_visuals(self):
        page = self._page(1280, 820)
        # 预置「今天见过开机」标记：用页面本地时间构造同样的键值。
        page.add_init_script(
            "const n=new Date();window.localStorage.setItem("
            "'project-snow-public:snow-adv:boot-seen', n.getFullYear()+'-'+(n.getMonth()+1)+'-'+n.getDate());")
        self._open(page, True)
        # 同日重访不建开机层。
        self.assertEqual(page.locator("#snow-boot").count(), 0)
        # 世界背景仍挂载。
        page.wait_for_function("document.getElementById('chat-app').dataset.snowWorld === 'exterior'", timeout=8000)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    # ---- 首次入室镜头推进 → P4 veil（显示目的地） ----
    def test_first_indoor_move_plays_push_then_veil_second_move_no_push(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._dismiss_opening(page)
        self._dismiss_notice(page)
        # 人仍在外面：背后世界为 generic。
        page.wait_for_function("window.__projectSnowTest && window.__projectSnowTest.snowProbe().backdropScene === 'generic'", timeout=8000)
        # 建好舞台 veil 节点（真实到场会建）；并模拟亲身入室（arrival 会先置 beenInPerson）。
        page.evaluate("""() => {
          document.getElementById('text-surface').hidden = true;
          document.getElementById('in-person-surface').hidden = false;
          document.documentElement.dataset.motionLevel = 'auto';
          window.__projectSnowTest.snowSetBeenInPerson(true);
        }""")
        # 布防（排除首帧/历史恢复的同步突发）：先触发一次同地点 render，并等待布防完成。
        page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'generic', character_location: '基地外' })")
        page.wait_for_function("window.__projectSnowTest.snowProbe().veilArmed === true", timeout=4000)
        # 首次由外→室内：背后世界 (#snow-backdrop) 的层应标记 data-pushing 并真的在动（WAAPI）。
        page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'observation', character_location: '观景区', character_activity: '和分析员一起看风景' })")
        page.wait_for_function("document.querySelector('#snow-backdrop .snow-layer-far[data-pushing]')", timeout=4000)
        pushing = page.evaluate("""() => {
          const room = document.querySelector('#snow-backdrop .snow-layer-room');
          const far = document.querySelector('#snow-backdrop .snow-layer-far');
          const roomAnims = room && room.getAnimations ? room.getAnimations().length : 0;
          const farAnims = far && far.getAnimations ? far.getAnimations().length : 0;
          return { pushPlayed: window.__projectSnowTest.snowProbe().pushPlayed, roomAnims, farAnims };
        }""")
        self.assertTrue(pushing["pushPlayed"], "首次入室应播放镜头推进")
        self.assertGreaterEqual(pushing["roomAnims"] + pushing["farAnims"], 1, "推进期间背后世界层应有运行中的 Web Animations（非恒等变换）")
        # 推进在白光峰值拉起 veil，标题必须是目的地「观景区」，副标题是活动，绝不是外景「基地外」。
        # （相位 up 是瞬态；标题一旦被 veil 写入即稳定，直接等标题变为目的地。）
        page.wait_for_function(
            "document.querySelector('#snow-scene-veil .snow-scene-veil-title')"
            " && document.querySelector('#snow-scene-veil .snow-scene-veil-title').textContent === '观景区'", timeout=6000)
        veil = page.evaluate("""() => ({
          title: document.querySelector('#snow-scene-veil .snow-scene-veil-title').textContent,
          sub: document.querySelector('#snow-scene-veil .snow-scene-veil-sub').textContent,
        })""")
        self.assertEqual(veil["title"], "观景区")
        self.assertNotEqual(veil["title"], "基地外")
        self.assertEqual(veil["sub"], "和分析员一起看风景")
        # 推进结束。第二次移动：不再推进（只走 veil）。
        page.wait_for_function("!document.querySelector('#snow-backdrop .snow-layer-far[data-pushing]')", timeout=6000)
        page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'archive', character_location: '资料室' })")
        page.wait_for_timeout(150)
        self.assertFalse(page.evaluate("() => document.querySelector('#snow-backdrop .snow-layer-far[data-pushing]') !== null"), "第二次移动不应再推进")
        # 同一时刻只有一个 SnowField rAF 循环在跑（面对面可见时背后世界暂停）。
        self.assertLessEqual(page.evaluate("() => window.__projectSnowTest.snowProbe().activeLoops"), 1)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_light_and_off_motion_do_not_play_push(self):
        for motion in ("light", "off"):
            page = self._page(1280, 820)
            self._open(page, True, motion=motion)
            self._dismiss_opening(page)
            self._dismiss_notice(page)
            page.evaluate("""() => {
              document.getElementById('text-surface').hidden = true;
              document.getElementById('in-person-surface').hidden = false;
              window.__projectSnowTest.snowSetBeenInPerson(true);
            }""")
            page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'generic', character_location: '基地外' })")
            page.wait_for_function("window.__projectSnowTest.snowProbe().veilArmed === true", timeout=4000)
            page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'observation', character_location: '观景区' })")
            page.wait_for_timeout(200)
            probe = page.evaluate("() => window.__projectSnowTest.snowProbe()")
            self.assertFalse(probe["pushPlayed"], f"{motion} 档位不应推进")
            self.assertFalse(page.evaluate("() => document.querySelector('#snow-backdrop .snow-layer-far[data-pushing]') !== null"))
            errors = list(self.errors)
            page.close()
            self.assertEqual(errors, [])

    # ---- 手机锁屏 ----
    def test_phone_shows_lock_with_clock_and_date(self):
        page = self._page(390, 844)
        self._open(page, True)
        page.locator("#snow-lock").wait_for(state="visible", timeout=8000)
        state = page.evaluate("""() => ({
          lock: Boolean(document.getElementById('snow-lock')),
          time: document.getElementById('snow-lock-time').textContent,
          date: document.getElementById('snow-lock-date').textContent,
          tod: document.querySelector('.snow-lock-tod').textContent,
          boot: document.getElementById('snow-boot') ? 1 : 0,
          worldAttr: document.getElementById('chat-app').dataset.snowWorld || null,
        })""")
        self.assertTrue(state["lock"])
        self.assertRegex(state["time"], r"^\d{2}:\d{2}$")
        self.assertIn("日", state["date"])
        self.assertIn(state["tod"], ("清晨", "白天", "黄昏", "夜晚"))
        self.assertEqual(state["boot"], 0)        # 手机走锁屏，不显示开机层
        self.assertIsNone(state["worldAttr"])      # 手机竖屏不做通讯器背后世界
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_phone_unlock_by_click_and_by_keyboard(self):
        # 点击解锁。
        page = self._page(390, 844)
        self._open(page, True)
        page.locator("#snow-lock").wait_for(state="visible", timeout=8000)
        page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        # 解锁后体验须知可达。
        page.locator("#accept-experience-notice").wait_for(state="visible", timeout=8000)
        self.assertEqual(self.errors, [])
        page.close()
        # 键盘解锁（Enter）。
        page2 = self._page(390, 844)
        self._open(page2, True)
        page2.locator("#snow-lock").wait_for(state="visible", timeout=8000)
        page2.locator("#snow-lock").focus()
        page2.keyboard.press("Enter")
        page2.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        page2.locator("#accept-experience-notice").wait_for(state="visible", timeout=8000)
        self.assertEqual(self.errors, [])

    def test_phone_lock_notification_card_opens_its_target(self):
        # 用夹具公告 feed 造一条未读公告：锁屏加载公告后出现「公告」通知卡；点开经既有
        # openAnnouncements 处理打开公告弹窗（解锁 → 先过体验须知 → 再弹公告）。
        page = self._page(390, 844)

        def announcements(route):
            route.fulfill(json=fixture._announcement_test_feed())

        page.route("**/announcements.json", announcements)
        self._open(page, True)
        page.locator("#snow-lock").wait_for(state="visible", timeout=8000)
        # 公告异步就绪后，锁屏补上未读公告通知卡。
        page.locator('#snow-lock .snow-note-card[data-snow-note="notice"]').wait_for(state="visible", timeout=8000)
        page.locator('#snow-lock .snow-note-card[data-snow-note="notice"]').click()
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        # 解锁后先过须知，再打开公告弹窗（既有 openAnnouncements 处理）。
        page.locator("#accept-experience-notice").wait_for(state="visible", timeout=8000)
        page.locator("#accept-experience-notice").click()
        page.locator("#announcements-dialog[open]").wait_for(timeout=8000)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_phone_lock_keyboard_notification_and_focus_stay_in_lock(self):
        page = self._page(390, 844)
        page.route("**/announcements.json", lambda route: route.fulfill(json=fixture._announcement_test_feed()))
        self._open(page, True)
        card = page.locator('#snow-lock .snow-note-card[data-snow-note="notice"]')
        card.wait_for(state="visible")
        page.locator("#snow-lock").focus()
        for _ in range(4):
            page.keyboard.press("Tab")
            self.assertTrue(page.evaluate("Boolean(document.activeElement.closest('#snow-lock'))"))
        card.focus()
        page.keyboard.press("Enter")
        page.locator("#snow-lock").wait_for(state="detached")
        self.assertFalse(page.evaluate("document.getElementById('chat-app').inert"))
        page.locator("#accept-experience-notice").click()
        page.locator("#announcements-dialog[open]").wait_for()
        self.assertEqual(self.errors, [])

    def test_phone_lock_skipped_on_same_session_revisit(self):
        page = self._page(390, 844)
        page.add_init_script("window.sessionStorage.setItem('project-snow-public:snow-adv:lock-seen', '1')")
        self._open(page, True)
        # 同一会话重访不建锁屏；须知直接可达。
        page.locator("#accept-experience-notice").wait_for(state="visible", timeout=8000)
        self.assertEqual(page.locator("#snow-lock").count(), 0)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    # ---- 体验须知在开场后仍可达 ----
    def test_experience_notice_reachable_after_desktop_boot(self):
        page = self._page(1280, 820)
        self._open(page, True)
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        page.locator("#accept-experience-notice").wait_for(state="visible", timeout=8000)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    # ---- 开场层占屏时，新手引导与体验须知都不得盖在其上 ----
    def test_onboarding_and_notice_do_not_overlay_lock_until_unlock(self):
        page = self._page(390, 844)
        self._open(page, True)
        page.locator("#snow-lock").wait_for(state="visible", timeout=8000)
        # 锁屏期间：新手引导不可见、体验须知不可命中（<dialog> 走顶层亦不得先于解锁出现）。
        locked = page.evaluate("""() => {
          const guide = document.getElementById('onboarding-guide');
          const notice = document.getElementById('experience-notice-dialog');
          const guideVisible = guide && !guide.hidden && getComputedStyle(guide).display !== 'none'
            && getComputedStyle(guide).visibility !== 'hidden';
          // 命中测试：锁屏覆盖全屏，屏幕中点上层元素应属于锁屏，而非引导/须知。
          const mid = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
          const topInLock = Boolean(mid && mid.closest('#snow-lock'));
          return { guideVisible, noticeOpen: Boolean(notice && notice.open), topInLock };
        }""")
        self.assertFalse(locked["guideVisible"], "锁屏期间不得显示新手引导")
        self.assertFalse(locked["noticeOpen"], "锁屏期间不得弹出体验须知")
        self.assertTrue(locked["topInLock"], "屏幕中点应被锁屏覆盖，不被引导/须知占据")
        # 解锁 → 先过体验须知 → 新手引导（onboarding 待完成）出现。
        page.locator("#snow-lock").click(position={"x": 10, "y": 10})
        page.locator("#snow-lock").wait_for(state="detached", timeout=8000)
        page.locator("#accept-experience-notice").wait_for(state="visible", timeout=8000)
        page.locator("#accept-experience-notice").click()
        page.locator("#onboarding-guide:not([hidden])").wait_for(state="visible", timeout=8000)
        step = page.evaluate("() => document.getElementById('onboarding-step-label').textContent")
        self.assertIn("第 1 步", step)
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    # ---- 首帧中性保持：config 取回失败也立即解除 ----
    def test_first_flash_hold_cleared_on_config_failure(self):
        page = self._page(1280, 820)
        # 预置「上次见过雪境」标记，使首帧守卫把根节点暂设不可见。
        page.add_init_script("window.localStorage.setItem('project-snow-public:snow-adv:last-seen', '1')")

        def fail_config(route):
            route.fulfill(status=500, content_type="application/json", body='{"error":"boom"}')

        page.route("**/public/v1/config", fail_config)
        page.goto(self.base_url, wait_until="domcontentloaded")
        # 远早于 1000ms 兜底：失败路径（boot().catch）应已清除内联 visibility 保持。
        page.wait_for_function(
            "document.documentElement.style.visibility !== 'hidden'"
            " && !document.documentElement.dataset.snowBooting", timeout=900)
        self.assertNotEqual(page.evaluate("() => document.documentElement.style.visibility"), "hidden")
        self.assertEqual(self._failed_skin_requests(), [])

    # ---- flag-off：开场层与世界层都不存在 ----
    def test_flag_off_has_no_opening_or_world_layer_desktop(self):
        page = self._page(1280, 820)
        self._open(page, False)
        self._dismiss_notice(page)
        state = page.evaluate("""() => ({
          ui: document.documentElement.dataset.ui || null,
          boot: document.getElementById('snow-boot') ? 1 : 0,
          lock: document.getElementById('snow-lock') ? 1 : 0,
          backdrop: document.getElementById('snow-backdrop') ? 1 : 0,
          worldAttr: document.getElementById('chat-app').dataset.snowWorld || null,
          worldLayers: document.querySelectorAll('#scene-stage [data-snow-world]').length,
        })""")
        self.assertEqual(state, {"ui": None, "boot": 0, "lock": 0, "backdrop": 0, "worldAttr": None, "worldLayers": 0})
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_off_has_no_lock_on_phone(self):
        page = self._page(390, 844)
        self._open(page, False)
        self._dismiss_notice(page)
        state = page.evaluate("""() => ({
          ui: document.documentElement.dataset.ui || null,
          lock: document.getElementById('snow-lock') ? 1 : 0,
        })""")
        self.assertEqual(state, {"ui": None, "lock": 0})
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvPolishTests(TestCase):
    """P7 上线后修整：首帧保持、开机交接、全屏舞台、弹窗叠放、锁屏雪白、外景镜像、空会话。"""

    setUpClass = classmethod(SnowAdvOpeningTests.setUpClass.__func__)
    tearDownClass = classmethod(SnowAdvOpeningTests.tearDownClass.__func__)
    setUp = SnowAdvOpeningTests.setUp
    tearDown = SnowAdvOpeningTests.tearDown
    _page = SnowAdvOpeningTests._page
    _open = SnowAdvOpeningTests._open
    _failed_skin_requests = SnowAdvOpeningTests._failed_skin_requests
    _dismiss_opening = SnowAdvOpeningTests._dismiss_opening
    _dismiss_notice = SnowAdvOpeningTests._dismiss_notice
    _reach_in_person = SnowAdvOpeningTests._reach_in_person

    def test_static_first_paint_hold_covers_shell_until_config(self):
        # index.html 静态带 data-snow-hold；config 未到时只显示品牌底色，界面不可见。
        self.assertIn('data-snow-hold="1"', (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8"))
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                page = self._page(1280, 820)
                held = []
                page.route("**/public/v1/config", lambda route: held.append(route))
                page.goto(self.base_url, wait_until="domcontentloaded")
                page.wait_for_function("document.getElementById('chat-app')")
                waiting = page.evaluate("""() => ({
                  hold: document.documentElement.dataset.snowHold || null,
                  shell: getComputedStyle(document.getElementById('chat-app')).visibility,
                  boot: Boolean(document.getElementById('snow-boot')),
                })""")
                self.assertEqual(waiting, {"hold": "1", "shell": "hidden", "boot": False})
                # 模块在打开本地存储后才请求 config；慢机器上此时可能尚未发出，等它到达再放行。
                for _ in range(80):
                    if held:
                        break
                    page.wait_for_timeout(25)
                self.assertEqual(len(held), 1)
                payload = held[0].fetch().json()
                payload["features"] = {**payload.get("features", {}), "ui_snow_adv": enabled}
                held[0].fulfill(json=payload)
                page.wait_for_function("!document.documentElement.dataset.snowHold", timeout=4000)
                self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('chat-app')).visibility"), "visible")
                self.assertEqual(page.evaluate("document.documentElement.dataset.ui || null"), "snow-adv" if enabled else None)
                page.close()
        self.assertEqual(self.errors, [])

    def test_first_paint_hold_released_on_config_failure(self):
        page = self._page(1280, 820)
        page.route("**/public/v1/config", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error":"boom"}'))
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.wait_for_function("!document.documentElement.dataset.snowHold", timeout=2000)
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('chat-app')).visibility"), "visible")

    def test_desktop_boot_hands_over_only_after_entrance(self):
        # 完整档：标志收起→底色淡开（不拦截点击）→通讯器入场→开机层移除→体验须知。
        page = self._page(1280, 820)
        held = []
        page.route("**/public/v1/characters", lambda route: held.append(route))
        self._open(page, True, motion="auto", wait_until="domcontentloaded", init_script="""(() => {
          const log = []; window.__p7log = log; const t = () => Math.round(performance.now());
          new MutationObserver(() => {
            const boot = document.getElementById('snow-boot');
            const app = document.getElementById('chat-app');
            const notice = document.getElementById('experience-notice-dialog');
            const s = [boot ? (boot.dataset.phase || 'in') : 'gone', app && app.dataset.snowEntering ? 'enter' : '-', notice && notice.open ? 'notice' : '-'].join('|');
            if (log.length === 0 || log[log.length - 1][1] !== s) log.push([t(), s]);
          }).observe(document, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-phase', 'data-snow-entering', 'open'] });
        })();""")
        try:
            page.locator("#snow-boot").wait_for(state="visible")
        finally:
            for route in held:
                route.continue_()
        page.wait_for_function("document.querySelector('.snow-boot[data-phase=out]')")
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('snow-boot')).pointerEvents"), "none")
        page.locator("#snow-boot").wait_for(state="detached", timeout=8000)
        page.locator("#accept-experience-notice").wait_for(state="visible", timeout=4000)
        states = [state for _, state in page.evaluate("window.__p7log")]
        self.assertIn("out|enter|-", states)
        out_at = states.index("out|enter|-")
        self.assertNotIn("notice", "".join(states[:out_at + 1]), "须知不得在开机层退场完成前弹出")
        after = states[out_at + 1:]
        self.assertTrue(after and after[0].startswith("gone|-"), f"开机层应在入场结束时才移除: {states}")
        self.assertIn("gone|-|notice", after)
        self.assertEqual(self.errors, [])

    def test_in_person_stage_is_full_bleed_with_narration_in_dialogue(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._reach_in_person(page)
        layout = page.evaluate("""() => {
          const rect = (id) => document.getElementById(id).getBoundingClientRect();
          const shown = (id) => { const n = document.getElementById(id); return Boolean(n) && getComputedStyle(n).display !== 'none' && !n.closest('[hidden]'); };
          const hud = document.querySelector('#in-person-surface .scene-hud');
          return {
            narrationInDialogue: Boolean(document.querySelector('#stage-dialogue #stage-narration')),
            stageTop: Math.round(rect('scene-stage').top - rect('in-person-surface').top),
            hudBorder: getComputedStyle(hud).borderBottomWidth,
            quickMenu: Boolean(document.getElementById('stage-quick-menu')),
            hudTranscript: shown('open-transcript'),
            hudHide: shown('toggle-stage-ui'),
          };
        }""")
        self.assertEqual(layout, {
            "narrationInDialogue": True, "stageTop": 0, "hudBorder": "0px",
            "quickMenu": True, "hudTranscript": False, "hudHide": False,
        })
        self.assertEqual(self.errors, [])
        self.assertEqual(self._failed_skin_requests(), [])

    def test_flag_off_keeps_legacy_narration_and_hud_buttons(self):
        page = self._page(1280, 820)
        self._open(page, False)
        self._reach_in_person(page)
        layout = page.evaluate("""() => ({
          narrationInStage: document.getElementById('stage-narration').parentElement.id,
          hudTranscript: getComputedStyle(document.getElementById('open-transcript')).display !== 'none',
          hold: document.documentElement.dataset.snowHold || null,
        })""")
        self.assertEqual(layout, {"narrationInStage": "scene-stage", "hudTranscript": True, "hold": None})
        self.assertEqual(self.errors, [])

    def test_dialogs_hide_onboarding_and_keep_controls_in_view(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._dismiss_opening(page)
        page.locator("#accept-experience-notice").wait_for(state="visible")
        # 须知复选框紧贴文字。
        gap = page.evaluate("""() => {
          const input = document.getElementById('notice-auto-summary');
          const range = document.createRange(); range.selectNodeContents(input.parentElement);
          const text = [...range.getClientRects()].filter(r => r.left > input.getBoundingClientRect().right - 1);
          return Math.round(Math.min(...text.map(r => r.left)) - input.getBoundingClientRect().right);
        }""")
        self.assertLess(gap, 24)
        page.locator("#accept-experience-notice").click()
        guide = page.locator("#onboarding-guide:not([hidden])")
        guide.wait_for(state="visible")
        # 任一模态弹窗打开时引导暂隐，关闭后恢复。
        page.locator("#open-announcements").click()
        page.locator("#announcements-dialog[open]").wait_for()
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('onboarding-guide')).visibility"), "hidden")
        ack = page.locator("#acknowledge-announcements").bounding_box()
        self.assertLessEqual(ack["y"] + ack["height"], 820, "公告「知道了」须在首屏内")
        page.locator("#acknowledge-announcements").click()
        page.locator("#announcements-dialog:not([open])").wait_for(state="attached")
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('onboarding-guide')).visibility"), "visible")
        self.assertEqual(self.errors, [])

    def test_empty_conversation_prompts_sit_under_greeting(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._dismiss_opening(page)
        self._dismiss_notice(page)
        page.locator(".empty-conversation .starter-prompts").first.wait_for()
        gap = page.evaluate("""() => {
          const box = document.querySelector('.empty-conversation');
          const prompts = box.querySelector('.starter-prompts');
          const before = prompts.previousElementSibling;
          return Math.round(prompts.getBoundingClientRect().top - before.getBoundingClientRect().bottom);
        }""")
        self.assertLessEqual(gap, 32)

    def test_generic_backdrop_is_mirrored_so_the_door_sits_right(self):
        page = self._page(1280, 820)
        self._open(page, True)
        self._dismiss_opening(page)
        page.locator("#snow-backdrop-stage .snow-layer-room svg").wait_for(state="attached")
        transform = page.evaluate("getComputedStyle(document.querySelector('#snow-backdrop-stage > .snow-layer-room > svg')).transform")
        self.assertEqual(transform, "matrix(-1, 0, 0, 1, 0, 0)")

    def test_phone_lock_screen_is_snow_white(self):
        from PIL import Image
        import io as _io

        page = self._page(390, 844)
        self._open(page, True)
        lock = page.locator("#snow-lock")
        lock.wait_for(state="visible")
        img = Image.open(_io.BytesIO(page.screenshot())).convert("L")
        top = img.crop((0, 0, 390, 300)).resize((1, 1)).getpixel((0, 0))
        self.assertGreater(top, 225, "锁屏上部应为雪白")
        self.assertLess(page.evaluate("""() => {
          const [r, g, b] = getComputedStyle(document.getElementById('snow-lock')).color.match(/\\d+/g).map(Number);
          return (r + g + b) / 3;
        }"""), 90, "锁屏文字应为深色")
        self.assertEqual(self.errors, [])


class SnowAdvP8ContractTests(TestCase):
    """P8 静态契约：墙面涂装编号与地点胶囊编号一致；开机层不再显示身份行。"""

    def test_wall_marks_match_location_pill_codes(self):
        js = (PUBLIC_ROOT / "modules" / "snow-world.js").read_text(encoding="utf-8")
        block = js[js.index("const ROOM_MARKS = {"):]
        block = block[: block.index("};")]
        marks = dict(re.findall(r'(\w+): \["(B-0\d)"', block))
        css = SKIN_CSS.read_text(encoding="utf-8")
        pills = dict(re.findall(r'\[data-scene="(\w+)"\] \.scene-location > span::before \{ content: "(B-0\d)"', css))
        self.assertGreaterEqual(len(marks), 6)
        for key, code in marks.items():
            self.assertEqual(pills.get(key), code, key)
        self.assertNotIn("corridor", marks, "走廊墙面是透视面，不放编号")

    def test_boot_screen_has_no_identity_subtitle(self):
        js = (PUBLIC_ROOT / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("海姆达尔战队 · 分析员终端", js)
        self.assertIn('id="tone-preference"', (PUBLIC_ROOT / "index.html").read_text(encoding="utf-8"))


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvP8VisualTests(TestCase):
    """P8：界面色调三档（默认跟随舞台时段）、面对面无底部白条、圆形图标、区域编号。"""

    # Text contrast against the composited background of each node (WCAG ratio), for `items => ratios`.
    CONTRAST_JS = """items => items.map(node => {
          const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
          const paint=canvas.getContext('2d',{willReadFrequently:true});
          const rgba=c=>{paint.clearRect(0,0,1,1);paint.fillStyle=c;paint.fillRect(0,0,1,1);const a=[...paint.getImageData(0,0,1,1).data];return [a[0],a[1],a[2],a[3]/255];};
          const over=(f,b)=>[0,1,2].map(i=>f[i]*f[3]+b[i]*(1-f[3])).concat(1);
          const layers=[];for(let n=node;n;n=n.parentElement){const c=rgba(getComputedStyle(n).backgroundColor);if(c[3]>0)layers.push(c);if(c[3]>=0.999)break;}
          let bg=[255,255,255,1];for(const c of layers.reverse())bg=over(c,bg);
          const fg=over(rgba(getComputedStyle(node).color),bg);
          const lum=c=>{const v=c.slice(0,3).map(x=>{x/=255;return x<=0.03928?x/12.92:((x+0.055)/1.055)**2.4;});return 0.2126*v[0]+0.7152*v[1]+0.0722*v[2];};
          const [a,b]=[lum(fg),lum(bg)].sort((x,y)=>y-x);return (a+0.05)/(b+0.05);})"""

    setUpClass = classmethod(SnowAdvOpeningTests.setUpClass.__func__)
    tearDownClass = classmethod(SnowAdvOpeningTests.tearDownClass.__func__)
    setUp = SnowAdvOpeningTests.setUp
    tearDown = SnowAdvOpeningTests.tearDown
    _page = SnowAdvOpeningTests._page
    _open = SnowAdvOpeningTests._open
    _dismiss_opening = SnowAdvOpeningTests._dismiss_opening
    _dismiss_notice = SnowAdvOpeningTests._dismiss_notice
    _reach_in_person = SnowAdvOpeningTests._reach_in_person

    @staticmethod
    def _stage_time(time, tone=None):
        # 后注入的脚本覆盖 PIN_LIGHT_TONE；时段经 html[data-snow-time] 固定。
        script = ""
        if tone is not None:
            script += f"window.localStorage.setItem('project-snow-public:tone', {json.dumps(tone)});"
        script += """(() => { const t = %s;
          const set = () => { if (document.documentElement) { document.documentElement.dataset.snowTime = t; return true; } return false; };
          if (!set()) { const mo = new MutationObserver(() => { if (set()) mo.disconnect(); }); mo.observe(document, { childList: true }); } })();""" % json.dumps(time)
        return script

    def _choose_tone(self, page, value):
        page.evaluate("""(value) => { const select = document.getElementById('tone-preference');
          select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })); }""", value)

    def _tone(self, page):
        return page.evaluate("""() => ({
          tone: document.documentElement.dataset.tone,
          time: document.documentElement.dataset.toneTime || null,
          snow0: getComputedStyle(document.documentElement).getPropertyValue('--snow-0').trim(),
          stored: localStorage.getItem('project-snow-public:tone'),
        })""")

    def test_auto_tone_is_dusk_then_night_and_preference_overrides(self):
        # 黄昏：暮色（深色规则 + 偏紫暖色变量）；夜晚：夜色；白天：雪白。
        cases = (("night", "dark", None, "#142033"), ("dusk", "dark", "dusk", "#2e2439"), ("day", "light", None, None))
        for time, expected, variant, snow0 in cases:
            with self.subTest(time=time):
                page = self._page(1280, 820)
                self._open(page, True, init_script=self._stage_time(time, tone=None if time == "day" else "auto"))
                self._dismiss_opening(page)
                if time == "day":
                    # 没有存过偏好：默认就是跟随时段。
                    page.evaluate("localStorage.removeItem('project-snow-public:tone')")
                    self._choose_tone(page, "auto")
                state = self._tone(page)
                self.assertEqual(state["tone"], expected)
                self.assertEqual(state["time"], variant)
                if snow0:
                    self.assertEqual(state["snow0"], snow0)
                else:
                    self.assertNotIn(state["snow0"], ("#142033", "#2e2439"))
                self.assertFalse(page.locator("#tone-preference-setting").evaluate("node => node.hidden"))
                page.close()
        page = self._page(1280, 820)
        self._open(page, True, init_script=self._stage_time("dusk", tone="auto"))
        self._dismiss_opening(page)
        self._choose_tone(page, "light")
        self.assertEqual(self._tone(page)["tone"], "light")
        self.assertIsNone(self._tone(page)["time"])
        self.assertEqual(self._tone(page)["stored"], "light")
        self._choose_tone(page, "dark")
        self.assertEqual((self._tone(page)["tone"], self._tone(page)["time"]), ("dark", None), "固定深色在黄昏也用夜色")
        page.evaluate("document.documentElement.dataset.snowTime = 'day'")
        self._choose_tone(page, "light")
        self._choose_tone(page, "dark")
        self.assertEqual(self._tone(page)["tone"], "dark", "深色档不看时段")
        self.assertEqual(self.errors, [])

    def test_stage_menu_items_stay_readable_in_every_tone(self):
        # app.css 把菜单项文字写死为 #416277；深色和暮色把弹层底改深后，文字必须随色调变浅。
        contrast = self.CONTRAST_JS
        for time, tone in (("day", "light"), ("dusk", "auto"), ("night", "dark")):
            with self.subTest(time=time, tone=tone):
                page = self._page(1280, 820)
                self._open(page, True, init_script=self._stage_time(time, tone=tone))
                self._reach_in_person(page)
                page.locator("#stage-menu > summary").click()
                page.locator(".scene-menu-popover").wait_for(state="visible")
                ratios = page.locator(".scene-menu-popover").evaluate(
                    f"menu => ({contrast})([...menu.querySelectorAll('button:not(:disabled), a')].filter(n => n.getClientRects().length))")
                self.assertTrue(ratios and min(ratios) >= 4.5, ratios)
                page.close()
        self.assertEqual(self.errors, [])

    def test_provider_doc_links_stay_readable_in_every_tone(self):
        # app.css 把设置「模型」页的厂商文档链接画在 #fff 上；暮色和深色把链接文字改浅后，底色也必须跟着变深。
        for time, tone in (("day", "light"), ("dusk", "auto"), ("night", "dark")):
            with self.subTest(time=time, tone=tone):
                page = self._page(1280, 820)
                self._open(page, True, init_script=self._stage_time(time, tone=tone))
                SnowAdvOpeningTests._dismiss_opening(self, page)
                SnowAdvOpeningTests._dismiss_notice(self, page)
                page.evaluate("() => document.getElementById('open-settings').click()")
                page.locator("#settings-tab-models").click()
                # 测试服务器的厂商没有文档地址，链接区是空的；按 app.js 渲染的同款标记填入两条，只检查样式。
                page.locator("#provider-doc-links").evaluate("""n => { n.innerHTML = ['官方文档', '隐私说明'].map(text =>
                  `<a href="https://example.invalid/" target="_blank" rel="noreferrer">OpenAI ${text}</a>`).join(''); n.hidden = false; }""")
                page.locator("#provider-doc-links a").first.wait_for(state="visible")
                ratios = page.locator("#provider-doc-links").evaluate(
                    f"links => ({self.CONTRAST_JS})([...links.querySelectorAll('a')].filter(n => n.getClientRects().length))")
                self.assertTrue(ratios and min(ratios) >= 4.5, ratios)
                page.close()
        self.assertEqual(self.errors, [])

    def test_tone_setting_is_hidden_without_the_skin(self):
        page = self._page(1280, 820)
        self._open(page, False, init_script=self._stage_time("night", tone="auto"))
        page.wait_for_function("!document.documentElement.dataset.snowHold")
        self.assertTrue(page.locator("#tone-preference-setting").evaluate("node => node.hidden"))
        self.assertNotEqual(page.evaluate("document.documentElement.dataset.tone || ''"), "dark")

    def test_returning_visitor_first_paint_hold_matches_dusk_and_night(self):
        for time, hint in (("night", "dark"), ("dusk", "dusk")):
            with self.subTest(time=time):
                page = self._page(1280, 820)
                held = []
                page.add_init_script("window.localStorage.setItem('project-snow-public:snow-adv:last-seen', '1')")
                page.add_init_script(self._stage_time(time, tone="auto"))
                page.route("**/public/v1/config", lambda route: held.append(route))
                page.goto(self.base_url, wait_until="domcontentloaded")
                page.wait_for_function("document.getElementById('chat-app')")
                state = page.evaluate("[document.documentElement.dataset.snowHold || null, document.documentElement.dataset.toneHint || null]")
                self.assertEqual(state, ["1", hint])
                for route in held:
                    route.continue_()
                page.close()

    def test_in_person_stage_runs_under_the_composer(self):
        page = self._page(1440, 900)
        self._open(page, True)
        self._reach_in_person(page)
        layout = page.evaluate("""() => {
          const app = document.getElementById('chat-app');
          const stage = document.getElementById('scene-stage').getBoundingClientRect();
          const composer = document.getElementById('composer');
          return {
            composerH: parseFloat(app.style.getPropertyValue('--snow-composer-h')) || 0,
            stageBottom: Math.round(stage.bottom),
            composerTop: Math.round(composer.getBoundingClientRect().top),
            composerBg: getComputedStyle(composer).backgroundColor,
          };
        }""")
        self.assertGreater(layout["composerH"], 40)
        self.assertGreaterEqual(layout["stageBottom"], 899, "舞台要延伸到输入栏下面")
        self.assertLess(layout["composerTop"], layout["stageBottom"])
        self.assertEqual(layout["composerBg"], "rgba(0, 0, 0, 0)", "输入栏外层透明，不留白条")
        self.assertEqual(self.errors, [])

    def test_terminal_icon_is_cropped_to_its_disc_and_location_has_an_area_code(self):
        page = self._page(1280, 820)
        held = []
        # Keep the real boot active while inspecting it: networkidle may return
        # after this short-lived node has already been removed by a fast fixture.
        page.route("**/public/v1/characters", lambda route: held.append(route))
        self._open(page, True, wait_until="domcontentloaded")
        try:
            page.locator("#snow-boot .snow-boot-mark img").wait_for(state="attached")
            boot = page.evaluate("""() => {
              const style = getComputedStyle(document.querySelector('#snow-boot .snow-boot-mark img'));
              return { clip: style.clipPath, size: [style.width, style.height],
                text: document.getElementById('snow-boot').textContent };
            }""")
            self.assertEqual(boot["size"], ["72px", "72px"])
            self.assertIn("circle", boot["clip"])
            self.assertNotIn("海姆达尔", boot["text"])
        finally:
            for route in held:
                route.continue_()
        self._reach_in_person(page)
        code = page.evaluate("""() => getComputedStyle(document.querySelector('.in-person-surface .scene-location > span'), '::before').content""")
        self.assertRegex(code, r'^"(EXT|B-0\d)"')
        self.assertEqual(self.errors, [])


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvDaylightContrastTests(TestCase):
    """Screenshot pixels behind temporary synthetic text, with no live runtime."""

    setUpClass = classmethod(SnowAdvOpeningTests.setUpClass.__func__)
    tearDownClass = classmethod(SnowAdvOpeningTests.tearDownClass.__func__)
    tearDown = SnowAdvOpeningTests.tearDown

    def setUp(self):
        SnowAdvOpeningTests.setUp(self)
        # One source serves both sides of every comparison while other work on
        # model hints/network wording proceeds independently.
        self.pinned_script = (PUBLIC_ROOT / "app.js").read_bytes()
        self.blocked = []

    def _save_evidence(self, name, payload):
        root = os.getenv("SNOW_CONTRAST_EVIDENCE_DIR")
        if root:
            directory = Path(root)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / name).open("xb") as stream:
                stream.write(payload)

    def _contrast_page(self, time, tone, interior, stylesheet=None, enabled=True):
        page = self.browser.new_page(viewport={"width": 1440, "height": 900},
                                     reduced_motion="reduce", timezone_id="Asia/Hong_Kong")
        page.set_default_timeout(10000)
        page.add_init_script(PIN_LIGHT_TONE + ";" + SnowAdvP8VisualTests._stage_time(time, tone))
        page.add_init_script("localStorage.setItem('project-snow-public:motion', 'off')")
        page.on("pageerror", lambda error: self.errors.append(type(error).__name__))

        def network(route):
            if route.request.url.startswith(self.base_url + "/"):
                route.continue_()
            else:
                self.blocked.append(route.request.resource_type)
                route.abort()

        page.route("**/*", network)
        page.route(self.base_url + "/app.js*", lambda route: route.fulfill(
            content_type="text/javascript", body=self.pinned_script))
        if stylesheet is not None:
            page.route(self.base_url + "/snow-adv.css*", lambda route: route.fulfill(
                content_type="text/css", body=stylesheet))

        def config(route):
            payload = route.fetch().json()
            payload["features"] = {"ui_snow_adv": enabled}
            route.fulfill(json=payload)

        page.route(self.base_url + "/public/v1/config", config)
        # An empty roster retains the real initial empty-session hint. The six
        # CSS samples below are synthetic DOM fixtures, never user messages.
        page.route(self.base_url + "/public/v1/characters", lambda route: route.fulfill(
            json={"count": 0, "characters": []}))
        page.goto(self.base_url, wait_until="networkidle")
        if enabled:
            SnowAdvOpeningTests._dismiss_opening(self, page)
        page.locator("#accept-experience-notice").click()
        page.locator("#skip-onboarding").wait_for(state="visible")
        page.locator("#skip-onboarding").click()
        fixture.PublicFrontendE2ETests._configure_model(page)
        page.locator("#toast").wait_for(state="hidden")
        if enabled:
            page.evaluate("""interior => {
              window.__projectSnowTest.snowSetBeenInPerson(interior);
              window.__projectSnowTest.snowRenderSceneWith({visual_key: 'observation',
                character_location: '观景区', character_activity: '合成对比度夹具', co_located: false});
            }""", interior)
            page.wait_for_function("""({time, world}) => {
              const far = document.querySelector('.snow-backdrop .snow-layer-far');
              return far?.dataset.time === time && document.querySelector('#chat-app').dataset.snowWorld === world;
            }""", arg={"time": time, "world": "interior" if interior else "exterior"})
        page.evaluate("""() => {
          const root = document.querySelector('#timeline');
          Object.assign(root.style, {display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: '26px'});
          root.innerHTML = `<div class="empty-conversation" style="min-height:0"><p data-contrast="empty">选择一位角色开始通讯。</p></div>
            <div class="message-time" data-contrast="message-time">合成时间 · 12:00</div>
            <article class="message" style="display:block;width:auto;margin:0"><span class="meta" data-contrast="meta">合成角色 · 文字通讯</span></article>
            <span class="message-usage" style="margin:0" data-contrast="message-usage">合成用量 · 0 次调用</span>
            <span class="request-status" data-contrast="request-status">合成状态 · 已就绪</span>
            <span class="typing-label" data-contrast="typing-label">合成输入提示</span>`;
        }""")
        self._wait_for_contrast_paint(page)
        return page

    @staticmethod
    def _wait_for_contrast_paint(page):
        page.evaluate("() => document.fonts.ready")
        page.wait_for_function("""() => document.getAnimations().every(animation => {
          const timing = animation.effect?.getComputedTiming();
          return !timing || !Number.isFinite(timing.endTime) || ['finished', 'idle'].includes(animation.playState);
        })""")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    @staticmethod
    def _pixel_contrast(page):
        import numpy as np
        from PIL import Image

        targets = page.locator("[data-contrast]").evaluate_all("""nodes => nodes.map(node => {
          const rect = node.getBoundingClientRect();
          return {name: node.dataset.contrast, color: getComputedStyle(node).color,
            shadow: getComputedStyle(node).textShadow,
            rect: [Math.floor(rect.left), Math.floor(rect.top), Math.ceil(rect.right), Math.ceil(rect.bottom)]};
        })""")
        normal = page.screenshot(animations="disabled")
        # Keep the actual painted halo while removing the glyph fill, so these
        # pixels are the real browser background immediately behind each glyph.
        page.locator("[data-contrast]").evaluate_all("nodes => nodes.forEach(node => node.style.webkitTextFillColor = 'transparent')")
        background = page.screenshot(animations="disabled")
        page.locator("[data-contrast]").evaluate_all("nodes => nodes.forEach(node => node.style.removeProperty('-webkit-text-fill-color'))")
        foreground_image = np.asarray(Image.open(_BytesIO(normal)).convert("RGB"), dtype=float)
        background_image = np.asarray(Image.open(_BytesIO(background)).convert("RGB"), dtype=float)

        def luminance(values):
            rgb = values / 255
            linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
            return linear @ np.array([0.2126, 0.7152, 0.0722])

        measurements = []
        for target in targets:
            left, top, right, bottom = target["rect"]
            visible = foreground_image[top:bottom, left:right]
            behind = background_image[top:bottom, left:right]
            ink = np.array(_rgb(target["color"]), dtype=float)
            full_delta = np.linalg.norm(ink - behind, axis=2)
            fill_delta = np.linalg.norm(visible - behind, axis=2)
            # Ignore antialiased edge pixels; test the background behind opaque
            # glyph cores rather than the foreground itself or an unrelated swatch.
            mask = (full_delta > 1) & (fill_delta >= full_delta * 0.9)
            samples = behind[mask]
            if not len(samples):
                raise AssertionError(f"No painted glyph cores sampled: {target['name']}")
            ink_lum, background_lum = luminance(ink), luminance(samples)
            ratios = (np.maximum(ink_lum, background_lum) + 0.05) / (np.minimum(ink_lum, background_lum) + 0.05)
            measurements.append({**target, "sampled_glyph_pixels": len(samples),
                "minimum_contrast": float(ratios.min()),
                "worst_background_rgb": samples[int(ratios.argmin())].astype(int).tolist()})
        return normal, background, measurements

    def test_dark_daylight_communicator_text_has_screenshot_contrast(self):
        for time in ("dawn", "day"):
            for interior in (False, True):
                with self.subTest(time=time, interior=interior):
                    page = self._contrast_page(time, "dark", interior)
                    normal, background, measurements = self._pixel_contrast(page)
                    label = f"dark-{time}-{'interior' if interior else 'exterior'}"
                    self._save_evidence(label + ".png", normal)
                    self._save_evidence(label + "-background.png", background)
                    self._save_evidence(label + ".json", json.dumps(measurements, ensure_ascii=False, indent=2).encode())
                    for item in measurements:
                        self.assertGreaterEqual(item["minimum_contrast"], 4.5, item)
                    page.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.blocked, [])
        self.assertEqual(fixture.PublicFrontendHandler.chat_payloads, [])

    def test_daylight_fix_keeps_other_tones_pixel_identical(self):
        from PIL import Image, ImageChops

        candidate = SKIN_CSS.read_text(encoding="utf-8")
        start = candidate.index(DAYLIGHT_CONTRAST_MARKER)
        # Retain the complete pre-existing P8 tail, including its separator.
        end = candidate.index("\n/* P8 尘白元素（轻量）", start)
        baseline = candidate[:start] + candidate[end:]
        source_evidence = {"baseline_lf_sha256": hashlib.sha256(baseline.encode()).hexdigest(),
            "candidate_lf_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
            "baseline_lf_bytes": len(baseline.encode()), "candidate_lf_bytes": len(candidate.encode()),
            "complete_existing_p8_tail_preserved": True}
        sealed_path = os.getenv("SNOW_CONTRAST_BASELINE_CSS")
        if sealed_path:
            sealed = Path(sealed_path).read_text(encoding="utf-8")
            self.assertEqual(baseline.encode(), sealed.encode())
            source_evidence["sealed_before_lf_sha256"] = hashlib.sha256(sealed.encode()).hexdigest()
            source_evidence["baseline_matches_sealed_before_lf_bytes"] = True
        self._save_evidence("unchanged-sources.json", json.dumps(source_evidence, indent=2).encode())
        cases = [(tone, time) for tone in ("light", "auto") for time in ("dawn", "day", "dusk", "night")]
        cases += [("dark", "dusk"), ("dark", "night")]
        comparisons = []
        for tone, time in cases:
            for interior in (False, True):
                with self.subTest(tone=tone, time=time, interior=interior):
                    page = self._contrast_page(time, tone, interior, stylesheet=baseline)
                    before = page.screenshot(animations="disabled")
                    # The complete candidate preserves its internal rule order.
                    page.add_style_tag(content=candidate)
                    self._wait_for_contrast_paint(page)
                    after = page.screenshot(animations="disabled")
                    difference = ImageChops.difference(Image.open(_BytesIO(before)).convert("RGB"),
                                                       Image.open(_BytesIO(after)).convert("RGB"))
                    label = f"unchanged-{tone}-{time}-{'interior' if interior else 'exterior'}"
                    self._save_evidence(label + "-before.png", before)
                    self._save_evidence(label + "-after.png", after)
                    comparisons.append({"case": label, "before_sha256": hashlib.sha256(before).hexdigest(),
                        "after_sha256": hashlib.sha256(after).hexdigest(), "different_pixels": 0 if difference.getbbox() is None else 1})
                    self.assertIsNone(difference.getbbox(), label)
                    page.close()
        self._save_evidence("unchanged-combinations.json", json.dumps(comparisons, indent=2).encode())
        self.assertEqual(self.errors, [])
        self.assertEqual(self.blocked, [])
