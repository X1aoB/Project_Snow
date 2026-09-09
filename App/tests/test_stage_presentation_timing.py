"""Browser regressions for stage preparation, reply timing and shared image leases."""
from __future__ import annotations

import base64
import json
import os
import threading
from hashlib import sha256
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest import TestCase, skipUnless
from urllib.parse import urlparse

from tests import test_public_frontend_e2e as frontend_fixture
from tests import test_public_frontend_reliability as reliability_fixture


CHARACTER_A = "25b23cb64398"
CHARACTER_B = "9f5804761c56"


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class StagePresentationTimingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), frontend_fixture.PublicFrontendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        frontend_fixture.PublicFrontendE2ETests.setUp(self)
        from PIL import Image

        self.images = {}
        for index, name in enumerate(("neutral", "happy", "thinking", "other", "body", "head")):
            image = Image.new("RGBA", (8, 10), (30 + index * 35, 170 - index * 20, 70 + index * 20, 255))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            self.images[name] = buffer.getvalue()
        self.manifests = {}
        self.characters = {}
        for character_id in (CHARACTER_A, CHARACTER_B):
            manifest = frontend_fixture.PublicFrontendHandler._expression_manifest(character_id)
            manifest.pop("presentations", None)
            manifest.pop("performances", None)
            for state, item in manifest["expressions"].items():
                name = state if state in ("happy", "thinking") else "neutral"
                if character_id == CHARACTER_B:
                    name = "other"
                item.pop("presentation_id", None)
                item.update(stage_asset_path=f"/timing/{name}.png", stage_asset_sha256=sha256(self.images[name]).hexdigest())
            encoded = json.dumps(manifest).encode()
            self.manifests[character_id] = encoded
            self.characters[character_id] = {
                "character_id": character_id,
                "expression_manifest_url": f"/timing/{character_id}/manifest.json",
                "expression_manifest_sha256": sha256(encoded).hexdigest(),
            }

    def tearDown(self):
        frontend_fixture.PublicFrontendE2ETests.tearDown(self)

    def _install(self, page):
        def route_fixture(route):
            path = urlparse(route.request.url).path
            if path == "/public/v1/characters":
                payload = route.fetch().json()
                for character in payload["characters"]:
                    if character["character_id"] in self.characters:
                        character.update(self.characters[character["character_id"]])
                route.fulfill(json=payload)
            elif path.startswith("/timing/"):
                name = path.rsplit("/", 1)[-1]
                if name == "manifest.json":
                    route.fulfill(content_type="application/json", body=self.manifests[path.split("/")[-2]])
                else:
                    route.fulfill(content_type="image/png", body=self.images[name.removesuffix(".png")])
            else:
                route.continue_()

        page.route("**/*", route_fixture)
        page.add_init_script("""(() => {
          window.__imageRequests = []; window.__decodeCalls = {};
          const fetch = window.fetch;
          window.fetch = (url, options) => {
            if (String(url).includes('/timing/')) window.__imageRequests.push(String(url));
            return fetch(url, options);
          };
          const decode = HTMLImageElement.prototype.decode;
          HTMLImageElement.prototype.decode = async function() {
            const src = this.src;
            window.__decodeCalls[src] = (window.__decodeCalls[src] || 0) + 1;
            if (src === window.__gateSrc && window.__decodeGate) {
              if (!window.__gateStartedAt) {
                window.__gateStartedAt = performance.now();
                if (window.__gateDelay != null) setTimeout(window.__releaseDecode, window.__gateDelay);
              }
              await window.__decodeGate;
            }
            return decode.call(this);
          };
        })();""")
        reliability_fixture.PublicFrontendReliabilityTests.open(self, page)

    def _gate(self, page, image, delay=None):
        src = "data:image/png;base64," + base64.b64encode(self.images[image]).decode()
        page.evaluate("""({src, delay}) => {
          window.__gateSrc = src; window.__gateStartedAt = 0; window.__gateDelay = delay;
          window.__gateReleasedAt = 0;
          window.__decodeGate = new Promise(resolve => window.__releaseDecode = () => {
            window.__gateReleasedAt = performance.now(); resolve();
          });
        }""", {"src": src, "delay": delay})

    def _enter_stage(self, page):
        frontend_fixture.PublicFrontendE2ETests._configure_model(page)
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator("#in-person-surface").wait_for(state="visible")
        page.wait_for_function("document.querySelector('#stage-speech').textContent === '你来了。'")
        page.locator("#stage-character-art").wait_for(state="visible")
        page.wait_for_function("!document.querySelector('#send-message').disabled")

    def _layered_presentation(self):
        return {
            "id": "timing.layers", "renderStrategy": "layered_sprite", "declaredRenderStrategy": "layered_sprite",
            "stageAssetPath": "/timing/neutral.png", "stageAssetSha256": sha256(self.images["neutral"]).hexdigest(),
            "canvas": {"width": 8, "height": 10}, "layout": {"scale": 1, "focusX": 50},
            "layers": [{"assetPath": f"/timing/{name}.png", "sha256": sha256(self.images[name]).hexdigest(),
                        "width": 8, "height": 10, "x": 0, "y": 0} for name in ("body", "head")],
        }

    def test_layers_start_in_parallel_and_prepared_surface_reuses_decoded_images(self):
        with frontend_fixture.sync_playwright() as playwright:
            browser = frontend_fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self._install(page)
                self._gate(page, "body")
                page.evaluate("""presentation => {
                  window.__presentation = presentation;
                  window.__preparing = window.__projectSnowTest.preloadStagePresentation(presentation);
                }""", self._layered_presentation())
                page.wait_for_function("window.__gateStartedAt > 0")
                # The first layer cannot finish decoding until this test releases it.
                page.wait_for_function("window.__imageRequests.some(url => url.endsWith('/timing/head.png'))", timeout=1500)
                result = page.evaluate("""async () => {
                  window.__releaseDecode(); const first = await window.__preparing;
                  const before = JSON.stringify(window.__decodeCalls);
                  first.prepared.dispose?.();
                  const second = await window.__projectSnowTest.preloadStagePresentation(window.__presentation);
                  return {strategy: second.prepared.renderStrategy, reused: before === JSON.stringify(window.__decodeCalls),
                    same: first.prepared.src === second.prepared.src};
                }""")
                self.assertEqual(result["strategy"], "layered_sprite")
                self.assertTrue(result["reused"])
                self.assertTrue(result["same"])
            finally:
                browser.close()

    def test_aborting_one_shared_consumer_does_not_cancel_the_other_or_poison_the_cache(self):
        with frontend_fixture.sync_playwright() as playwright:
            browser = frontend_fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self._install(page)
                self._gate(page, "body")
                page.evaluate("""presentation => {
                  window.__presentation = presentation; window.__consumer = new AbortController();
                  window.__first = window.__projectSnowTest.preloadStagePresentation(presentation, window.__consumer.signal)
                    .then(() => 'success', error => error.name);
                  window.__second = window.__projectSnowTest.preloadStagePresentation(presentation);
                }""", self._layered_presentation())
                page.wait_for_function("window.__gateStartedAt > 0")
                result = page.evaluate("""async () => {
                  window.__consumer.abort(); const aborted = await window.__first;
                  window.__releaseDecode(); const ready = await window.__second;
                  const cached = await window.__projectSnowTest.preloadStagePresentation(window.__presentation);
                  return {aborted, strategy: ready.prepared.renderStrategy, same: ready.prepared.src === cached.prepared.src};
                }""")
                self.assertEqual(result, {"aborted": "AbortError", "strategy": "layered_sprite", "same": True})
            finally:
                browser.close()

    def test_cached_pixels_cannot_bypass_new_digests_or_composition_dimensions(self):
        with frontend_fixture.sync_playwright() as playwright:
            browser = frontend_fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self._install(page)
                result = page.evaluate("""async presentation => {
                  const hooks = window.__projectSnowTest;
                  const original = await hooks.preloadStagePresentation(presentation);
                  const changedHash = structuredClone(presentation);
                  changedHash.layers[0].sha256 = '0'.repeat(64);
                  const rejectedHash = await hooks.preloadStagePresentation(changedHash);
                  const changedSize = structuredClone(presentation);
                  changedSize.layers[0].width = 7;
                  const rejectedSize = await hooks.preloadStagePresentation(changedSize);
                  const changedCanvas = structuredClone(presentation);
                  changedCanvas.canvas.width = 9;
                  const wider = await hooks.preloadStagePresentation(changedCanvas);
                  return {original: original.prepared.renderStrategy,
                    hash: rejectedHash.prepared.renderStrategy, size: rejectedSize.prepared.renderStrategy,
                    width: wider.prepared.image.naturalWidth};
                }""", self._layered_presentation())
                self.assertEqual(result, {"original": "layered_sprite", "hash": "single_sprite",
                                          "size": "single_sprite", "width": 9})
            finally:
                browser.close()

    def test_same_character_expression_crossfades_quickly_and_respects_reduced_motion(self):
        with frontend_fixture.sync_playwright() as playwright:
            browser = frontend_fixture._launch_browser(playwright)
            try:
                page = browser.new_page()
                self._install(page)
                self._enter_stage(page)
                result = page.evaluate("""async character => {
                  const art = document.querySelector('#stage-character-art'); const previous = art.src;
                  await window.__projectSnowTest.updateStageCharacterArt(art, character, 'happy');
                  const outgoing = document.querySelector('.stage-character-art-outgoing');
                  return {state: art.dataset.expressionState, changed: art.src !== previous,
                    outgoing: outgoing?.src === previous,
                    durations: art.getAnimations().map(animation => animation.effect.getTiming().duration)};
                }""", self.characters[CHARACTER_A])
                self.assertEqual(result["state"], "happy")
                self.assertTrue(result["changed"])
                self.assertTrue(result["outgoing"])
                self.assertTrue(any(0 < duration <= 200 for duration in result["durations"]))
                page.wait_for_function("!document.querySelector('.stage-character-art-outgoing')", timeout=1000)
                page.emulate_media(reduced_motion="reduce")
                result = page.evaluate("""async character => {
                  const art = document.querySelector('#stage-character-art');
                  await window.__projectSnowTest.updateStageCharacterArt(art, character, 'neutral');
                  return {outgoing: !!document.querySelector('.stage-character-art-outgoing'),
                    transition: art.dataset.stageArtTransition || ''};
                }""", self.characters[CHARACTER_A])
                self.assertEqual(result, {"outgoing": False, "transition": ""})
            finally:
                browser.close()

    def _reply_timing(self, delay):
        with frontend_fixture.sync_playwright() as playwright:
            browser = frontend_fixture._launch_browser(playwright)
            try:
                page = browser.new_page(reduced_motion="reduce")
                self._install(page)
                self._enter_stage(page)
                self._gate(page, "happy", delay)
                reply = "同步短回复。"
                packet = {"communication_channel": "in_person", "content_blocks": [{"type": "speech", "text": reply}],
                          "expression_state": "happy", "stage_motion": "none", "truncated": False}
                page.route("**/public/v1/chat/stream", lambda route: route.fulfill(
                    content_type="text/event-stream", body="event: done\ndata: " + json.dumps(packet) + "\n\n"))
                page.evaluate("""reply => {
                  const speech = document.querySelector('#stage-speech'); window.__firstReply = null;
                  window.__replyObserver = new MutationObserver(() => {
                    if (!window.__firstReply && speech.textContent.includes(reply)) window.__firstReply = {
                      at: performance.now(), state: document.querySelector('#stage-character-art').dataset.expressionState};
                  }); window.__replyObserver.observe(speech, {childList:true, subtree:true, characterData:true});
                  window.__renderInterval = setInterval(() => window.__projectSnowTest.renderStage(), 25);
                }""", reply)
                page.locator("#message-input").fill("时序合成响应")
                page.locator("#send-message").click()
                page.wait_for_function("window.__gateStartedAt > 0", timeout=5000)
                page.wait_for_function("window.__firstReply !== null", timeout=1500)
                timing = page.evaluate("""() => ({...window.__firstReply, started: window.__gateStartedAt,
                  released: window.__gateReleasedAt})""")
                if delay is None:
                    self.assertEqual(timing["released"], 0)
                    self.assertNotEqual(timing["state"], "happy")
                    self.assertGreaterEqual(timing["at"] - timing["started"], 150)
                    self.assertLess(timing["at"] - timing["started"], 1000)
                    page.evaluate("window.__releaseDecode()")
                else:
                    self.assertEqual(timing["state"], "happy")
                    self.assertGreaterEqual(timing["at"], timing["released"])
                page.wait_for_function("document.querySelector('#stage-character-art').dataset.expressionState === 'happy'")
                self.assertEqual(page.locator("#stage-speech").inner_text(), reply)
                page.evaluate("clearInterval(window.__renderInterval); window.__replyObserver.disconnect()")
            finally:
                browser.close()

    def test_reply_waits_for_a_quick_expression_decode(self):
        self._reply_timing(100)

    def test_reply_wait_is_bounded_even_when_unrelated_renders_repeat(self):
        self._reply_timing(None)

    def test_character_warmup_does_not_paint_and_stale_character_work_cannot_commit(self):
        with frontend_fixture.sync_playwright() as playwright:
            browser = frontend_fixture._launch_browser(playwright)
            try:
                page = browser.new_page(reduced_motion="reduce")
                self._install(page)
                self._enter_stage(page)
                self._gate(page, "other")
                page.evaluate("""character => {
                  window.__beforeWarm = document.querySelector('#stage-character-art').src;
                  window.__warm = window.__projectSnowTest.warmCharacterStage(character);
                }""", self.characters[CHARACTER_B])
                page.wait_for_function("window.__gateStartedAt > 0")
                self.assertTrue(page.evaluate("document.querySelector('#stage-character-art').src === window.__beforeWarm"))
                page.evaluate("window.__releaseDecode()")
                page.evaluate("window.__warm")
                self.assertEqual(page.locator("#stage-character-art").get_attribute("data-expression-character-id"), CHARACTER_A)
                self._gate(page, "thinking")
                page.evaluate("""character => {
                  window.__staleUpdate = window.__projectSnowTest.updateStageCharacterArt(
                    document.querySelector('#stage-character-art'), character, 'thinking');
                }""", self.characters[CHARACTER_A])
                page.wait_for_function("window.__gateStartedAt > 0")
                if page.locator("#open-stage-contacts").get_attribute("aria-expanded") != "true":
                    page.locator("#open-stage-contacts").click()
                page.locator(f'[data-character="{CHARACTER_B}"]').click()
                page.wait_for_function("document.querySelector('[data-character=\"9f5804761c56\"]').getAttribute('aria-current') === 'true'")
                page.evaluate("window.__releaseDecode()")
                result = page.evaluate("""async () => {
                  const stale = await window.__staleUpdate; const art = document.querySelector('#stage-character-art');
                  return {stale, hidden: art.hidden, character: art.dataset.expressionCharacterId || ''};
                }""")
                self.assertFalse(result["stale"])
                self.assertTrue(result["hidden"] or result["character"] != CHARACTER_A)
            finally:
                browser.close()
