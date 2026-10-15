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
