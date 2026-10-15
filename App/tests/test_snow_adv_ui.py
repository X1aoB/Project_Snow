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

    def test_rollout_pipeline_does_not_accept_the_flag_yet(self):
        # Production enablement is a later, separately reviewed change to the
        # deploy allow-list; until then the flag cannot reach a public env file.
        self.assertNotIn("PUBLIC_UI_SNOW_ADV", (APP_ROOT / "ops" / "deploy.sh").read_text(encoding="utf-8"))
        self.assertNotIn("PUBLIC_UI_SNOW_ADV", (APP_ROOT / "ops" / "public.env.example").read_text(encoding="utf-8"))


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
        self.assertEqual(self.page.evaluate("() => document.documentElement.dataset.motionLevel"), "off")
        # With motion off there must be no persistent rAF loop repainting the
        # snow canvases.  A running loop clears + redraws every frame, so the
        # drawn pixels drift; a stopped loop leaves them byte-identical.  Sample
        # the far canvas across several animation frames and require no change.
        drifted = self.page.evaluate("""async () => {
          const canvas = document.querySelector('#scene-stage canvas.snow-canvas-far');
          const ctx = canvas.getContext('2d');
          const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
          const sample = () => ctx.getImageData(0, 0, canvas.width, canvas.height).data.join(',');
          await frame();
          const first = sample();
          for (let i = 0; i < 8; i += 1) await frame();
          return first !== sample();
        }""")
        self.assertFalse(drifted, "motion off must not keep repainting the snow canvas")
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
        names = page.evaluate("""() => [...document.querySelectorAll('#stage-quick-menu button')]
          .map(button => button.getAttribute('aria-label'))""")
        self.assertEqual(names, ["跳过", "回看", "隐藏", "设置"])
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
        self.assertEqual(state["names"], ["跳过", "回看", "隐藏", "设置"])
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
        return page.evaluate(
            """async character => {
          const art = document.getElementById('stage-character-art');
          const previous = art.getAttribute('src');
          const samples = { outgoingMin: 1, incomingMin: 1, incomingMax: 0, bothSemi: false };
          let raf = true;
          const probe = () => {
            if (!raf) return;
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
            requestAnimationFrame(probe);
          };
          requestAnimationFrame(probe);
          await window.__projectSnowTest.updateStageCharacterArt(art, character, 'happy');
          // Sample across the full crossfade duration.
          await new Promise(resolve => setTimeout(resolve, 360));
          raf = false;
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
        page.wait_for_function("!document.querySelector('.stage-character-art-outgoing')", timeout=2000)
        remaining = page.evaluate(
            "() => document.querySelectorAll('#scene-stage .stage-character-art, #scene-stage .stage-character-art-outgoing').length")
        self.assertEqual(remaining, 1)
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

