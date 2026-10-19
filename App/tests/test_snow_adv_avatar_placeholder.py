"""Real image events and cached-image placeholder contracts, entirely loopback."""
from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from tests import test_dialogue_v2_frontend as dialogue
from tests.test_snow_adv_base_map import BASELINE, PUBLIC, MapBrowserFixture, assert_identical_pixels


def avatar(character_id):
    return {"thumbnail_src": f"/synthetic-avatar/{character_id}/96.png",
            "src": f"/synthetic-avatar/{character_id}/200.png",
            "portrait_focus_x": 50, "portrait_focus_y": 50, "portrait_scale": 1}


def image_bytes(character_id):
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (200, 200), "#952b6d" if character_id == dialogue.CHARACTER else "#207e85")
    ImageDraw.Draw(image).rectangle((40, 30, 160, 170), fill="#f9dc9c")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvAvatarPlaceholderTests(MapBrowserFixture, TestCase):
    def serve_image(self, route):
        character_id = route.request.url.split("/")[-2]
        route.fulfill(content_type="image/png", body=image_bytes(character_id),
                      headers={"Cache-Control": "public, max-age=3600"})

    def portrait(self, page):
        return page.locator(".snow-empty-greeting .snow-empty-avatar")

    def assert_fallback(self, page, initial):
        # The 200px request can remain paused: settle finite UI animation, not
        # the held image or the world's continuous rendering loop.
        self.paint_settled(page)
        node = self.portrait(page)
        self.assertEqual(node.locator(".portrait-fallback").inner_text(), initial)
        self.assertTrue(node.locator(".portrait-fallback").is_visible())
        self.assertNotIn("has-image", node.get_attribute("class"))
        self.assertEqual(node.locator("img:visible").count(), 0)
        box = node.bounding_box()
        self.assertGreaterEqual(box["width"], 60)
        self.assertGreaterEqual(box["height"], 60)
        contrast = self.text_contrast(page, ".snow-empty-avatar .portrait-fallback")
        self.assertGreaterEqual(contrast["contrast"], 4.5, contrast)
        self.evidence.append({"fallback_initial": initial, "viewport": page.viewport_size,
                              "steady_contrast": contrast})

    def test_pending_initial_contrast_for_actual_tones_on_desktop_and_mobile(self):
        for width, height in ((1440, 900), (390, 844)):
            for tone in ("light", "auto", "dark"):
                with self.subTest(width=width, tone=tone):
                    pending = []
                    page = self.new_page(width=width, height=height, tone=tone, avatars=avatar,
                                         motion="auto", reduced_motion="no-preference")

                    def images(route):
                        if route.request.url.endswith(f"/{dialogue.CHARACTER}/200.png"):
                            pending.append(route)
                        else:
                            self.serve_image(route)

                    page.route(self.base_url + "/synthetic-avatar/**", images)
                    try:
                        self.open_page(page, wait_until="domcontentloaded")
                        self.assert_fallback(page, "凯")
                        self.assertEqual(len(pending), 1)
                        actual = page.evaluate("""() => ({tone:document.documentElement.dataset.tone,
                          toneTime:document.documentElement.dataset.toneTime||null,
                          motion:document.documentElement.dataset.motionLevel})""")
                        self.assertEqual(actual, {"tone": "light" if tone == "light" else "dark",
                                                 "toneTime": "dusk" if tone == "auto" else None, "motion": "auto"})
                        self.assertEqual(self.portrait(page).locator("img").evaluate("image=>image.naturalWidth"), 0)
                        self.evidence.append({"pending_tone": actual, "viewport": page.viewport_size,
                                              "held_200_request_count": 1, "initial": "凯"})
                    finally:
                        if pending:
                            self.serve_image(pending.pop())
                            page.wait_for_function("() => document.querySelector('.snow-empty-avatar img')?.naturalWidth===200")
                        page.close()

    def test_slow_first_frame_has_initial_and_success_or_failure_keeps_visible_content(self):
        for success in (True, False):
            with self.subTest(success=success):
                pending = []
                page = self.new_page(avatars=avatar)
                def images(route):
                    if route.request.url.endswith(f"/{dialogue.CHARACTER}/200.png"):
                        pending.append(route)
                    else:
                        self.serve_image(route)
                page.route(self.base_url + "/synthetic-avatar/**", images)
                self.open_page(page, wait_until="domcontentloaded")
                self.assert_fallback(page, "凯")
                self.assertEqual(len(pending), 1)
                if success:
                    self.serve_image(pending.pop())
                    page.wait_for_function("""() => {const n=document.querySelector('.snow-empty-avatar');
                      return n.classList.contains('has-image') && n.querySelector('img')?.naturalWidth===200;}""")
                    self.assertFalse(self.portrait(page).locator(".portrait-fallback").is_visible())
                    self.assertTrue(self.portrait(page).locator("img").is_visible())
                else:
                    pending.pop().fulfill(status=404, body="synthetic unavailable")
                    self.portrait(page).locator("img").wait_for(state="detached")
                    self.assert_fallback(page, "凯")
                self.evidence.append({"slow_image_success": success, "first_frame_initial_visible": True,
                                      "source_path_suffix": "/200.png", "paid_calls": 0})
                page.close()

    def test_complete_bound_success_and_failure_are_synchronized_before_early_return(self):
        page = self.new_page(avatars=avatar)
        page.route(self.base_url + "/synthetic-avatar/**", self.serve_image)
        self.open_page(page)
        page.wait_for_function("() => document.querySelector('.snow-empty-avatar img')?.naturalWidth===200")
        restored = page.evaluate("""() => {
          const node=document.querySelector('.snow-empty-avatar'), image=node.querySelector('img');
          if (!image.complete || image.dataset.bound!=='1') throw new Error('fixture image not cached and bound');
          node.classList.remove('has-image');
          window.__baseMapFixture.bindImages('#timeline');
          return node.classList.contains('has-image');
        }""")
        self.assertTrue(restored)
        self.assertFalse(self.portrait(page).locator(".portrait-fallback").is_visible())
        page.evaluate("""async () => {
          const image=new Image(); image.src='data:image/png;base64,aW52YWxpZA==';
          await new Promise(resolve=>{image.onerror=resolve;});
          if(!image.complete || image.naturalWidth!==0) throw new Error('fixture broken cache missing');
          image.dataset.bound='1';
          const node=document.querySelector('.snow-empty-avatar'); node.querySelector('img').replaceWith(image);
          node.classList.add('has-image'); window.__baseMapFixture.bindImages('#timeline');
        }""")
        self.assert_fallback(page, "凯")
        self.evidence.append({"cached_complete_bound_success_restored": True, "cached_complete_bound_failure_fallback": True})

    def test_late_previous_character_image_cannot_replace_current_character(self):
        pending = []
        page = self.new_page(avatars=avatar)
        def images(route):
            if route.request.url.endswith(f"/{dialogue.CHARACTER}/200.png"):
                pending.append(route)
            else:
                self.serve_image(route)
        page.route(self.base_url + "/synthetic-avatar/**", images)
        self.open_page(page, wait_until="domcontentloaded")
        self.assert_fallback(page, "凯")
        page.locator(f'[data-character="{dialogue.OTHER}"]').click()
        page.wait_for_function("""() => {const image=document.querySelector('.snow-empty-avatar img');
          return image?.src.includes('/9f5804761c56/200.png') && image.naturalWidth===200;}""")
        self.assertEqual(len(pending), 1)
        self.serve_image(pending.pop())
        self.paint_settled(page)
        self.assertEqual(page.locator("#active-character h1").inner_text(), "安卡希雅")
        self.assertIn(f"/{dialogue.OTHER}/200.png", self.portrait(page).locator("img").get_attribute("src"))
        self.assertEqual(self.portrait(page).locator(".portrait-fallback").inner_text(), "安")
        self.assertTrue(self.portrait(page).locator("img").is_visible())
        self.assertFalse(self.portrait(page).locator(".portrait-fallback").is_visible())

    def test_original_ui_avatar_css_pixels_and_cross_source_render_contract(self):
        """Compare JS render contracts across pages and CSS pixels on one page.

        Independent pages can rasterize rounded images differently even for
        c8-to-c8 controls. They must still render the same DOM/images/styles.
        The loaded candidate page separately requires exact A-to-A and A-to-B
        whole-viewport RGBA pixels when replacing the full controlled CSS.
        """
        from PIL import Image

        baseline_app = self.baseline_source("app.js")
        baseline_css = self.baseline_source("snow-adv.css")
        candidate_css = (PUBLIC / "snow-adv.css").read_bytes()
        self.assertEqual((PUBLIC / "app.css").read_bytes().replace(b"\r\n", b"\n"), self.baseline_source("app.css"))
        digest = lambda raw: hashlib.sha256(raw).hexdigest()
        directory = os.getenv("SNOW_BASE_MAP_EVIDENCE_DIR")

        def artifact(label, suffix, raw):
            if directory:
                target = Path(directory)
                target.mkdir(parents=True, exist_ok=True)
                with (target / (self._testMethodName + "-" + label + suffix)).open("xb") as handle:
                    handle.write(raw)

        def settled(page):
            page.wait_for_function("() => window.__projectSnowTest.firstScreenSettled()")
            page.wait_for_load_state("networkidle")
            page.evaluate("""async () => {
              const visible=[...document.images].filter(image=>{const r=image.getBoundingClientRect();
                return r.width&&r.height&&r.bottom>0&&r.top<innerHeight&&r.right>0&&r.left<innerWidth;});
              await Promise.all(visible.map(image=>image.decode()));
            }""")
            page.mouse.move(1195, 845)
            self.paint_settled(page)
            self.assertTrue(page.locator("#onboarding-guide").evaluate("node=>node.hidden"))
            self.assertEqual(page.locator("#snow-base-map,#open-snow-base-map,#snow-base-map-style,.snow-empty-greeting").count(), 0)

        def contract(page, served, http_start):
            value = page.evaluate("""() => {
              const rect=node=>node.getBoundingClientRect().toJSON();
              const properties=['display','position','box-sizing','width','height','min-width','min-height',
                'max-width','max-height','top','right','bottom','left','margin','padding','gap','align-items',
                'justify-content','place-items','grid-template-columns','flex','overflow','overflow-x','overflow-y',
                'color','background','border','border-radius','box-shadow','outline','opacity','filter','backdrop-filter',
                'transform','transform-origin','clip-path','mask-image','object-fit','object-position','visibility',
                'font-family','font-size','font-weight','line-height','letter-spacing','text-shadow','white-space',
                'content','animation-name','animation-duration','transition-property','transition-duration','z-index'];
              const style=(node,pseudo=null)=>{const css=getComputedStyle(node,pseudo);
                const keys=[...new Set([...properties,...[...css].filter(key=>key.startsWith('--'))])].sort();
                return Object.fromEntries(keys.map(key=>[key,css.getPropertyValue(key)]));};
              const nodeInfo=node=>({tag:node.tagName,id:node.id,className:node.className,rect:rect(node),
                style:style(node),before:style(node,'::before'),after:style(node,'::after')});
              const ancestors=node=>{const rows=[];for(let n=node.parentElement;n;n=n.parentElement){
                rows.push(nodeInfo(n));if(n.id==='contact-panel')break;}return rows;};
              return {ui:document.documentElement.dataset.ui||null,
                selected:document.querySelector('.character[aria-current="true"]')?.dataset.character,
                activeCharacter:document.querySelector('#active-character h1').textContent,
                channel:document.querySelector('#chat-app').dataset.channel,
                characterListDOM:document.querySelector('#character-list').innerHTML,
                scroll:{window:[scrollX,scrollY],list:document.querySelector('#character-list').scrollTop,
                  panel:document.querySelector('#contact-panel').scrollTop},
                hover:[...document.querySelectorAll(':hover')].map(n=>({tag:n.tagName,id:n.id,className:n.className})),
                focus:{tag:document.activeElement.tagName,id:document.activeElement.id,className:document.activeElement.className},
                portraits:[...document.querySelectorAll('.portrait')].map(node=>{
                  const image=node.querySelector('img'),fallback=node.querySelector('.portrait-fallback');
                  return {dom:node.outerHTML,node:nodeInfo(node),ancestors:ancestors(node),
                    fallback:fallback?{text:fallback.textContent,...nodeInfo(fallback)}:null,
                    image:image?{src:image.getAttribute('src'),currentSrc:new URL(image.currentSrc||image.src).pathname,
                      complete:image.complete,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,
                      decoding:image.decoding,loading:image.loading,bound:image.dataset.bound||null,...nodeInfo(image)}:null};})};
            }""")
            value["legacy_map_requests"] = [path for _, path in self.http[http_start:] if "snow-base-map" in path]
            self.assertEqual(value["legacy_map_requests"], [])
            self.assertIsNone(value["ui"])
            self.assertEqual(value["selected"], dialogue.CHARACTER)
            self.assertEqual(value["channel"], "text")
            visible_images = 0
            for row in value["portraits"]:
                image = row["image"]
                if image is None:  # The unused stage header keeps its real placeholder DOM.
                    continue
                image["response_bytes"] = served.get(image["currentSrc"])
                bounds = image["rect"]
                if bounds["width"] and bounds["height"] and bounds["bottom"] > 0 and bounds["top"] < 850:
                    visible_images += 1
                    self.assertTrue(image["complete"])
                    self.assertEqual((image["naturalWidth"], image["naturalHeight"]), (200, 200))
                    self.assertIsNotNone(image["response_bytes"])
            self.assertGreater(visible_images, 0)
            return value

        contracts = []
        candidate_page = None
        for label, script in (("baseline-js", baseline_app), ("candidate-js", self.pinned_app)):
            served = {}
            page = self.new_page(enabled=False, avatars=avatar, script=script, css=baseline_css)

            def images(route):
                path = urlsplit(route.request.url).path
                body = image_bytes(path.split("/")[-2])
                served[path] = {"sha256": digest(body), "bytes": len(body)}
                route.fulfill(content_type="image/png", body=body, headers={"Cache-Control": "public, max-age=3600"})

            page.route(self.base_url + "/synthetic-avatar/**", images)
            http_start = len(self.http)
            self.open_page(page)
            settled(page)
            observed = contract(page, served, http_start)
            contracts.append(observed)
            artifact(label, ".json", (json.dumps(observed, sort_keys=True, indent=2) + "\n").encode())
            if label == "baseline-js":
                page.close()
            else:
                candidate_page = page
        self.assertEqual(contracts[0], contracts[1], "legacy JS DOM/image/style contract changed")

        # Replace the sheet at its existing cascade position, never append a
        # candidate override that leaves old declarations active underneath it.
        page = candidate_page
        sheet = page.locator('link[rel="stylesheet"][href^="/snow-adv.css"]')
        self.assertEqual(sheet.count(), 1)
        sheet.evaluate("""(old,css)=>{const sheet=document.createElement('style');
          sheet.id='legacy-avatar-controlled-css';sheet.textContent=css;old.replaceWith(sheet);}""", baseline_css.decode())
        screenshots = []
        frame_rows = []
        for label, css in (("css-a-initial", baseline_css), ("css-a-noop", baseline_css), ("css-b-candidate", candidate_css)):
            if label != "css-a-initial":
                page.locator("#legacy-avatar-controlled-css").evaluate("""(old,css)=>{
                  const sheet=document.createElement('style');sheet.id=old.id;sheet.textContent=css;old.replaceWith(sheet);
                }""", css.decode())
            self.assertEqual(page.locator('link[rel="stylesheet"][href^="/snow-adv.css"]').count(), 0)
            self.assertEqual(page.locator("#legacy-avatar-controlled-css").count(), 1)
            self.assertEqual(page.locator("#legacy-avatar-controlled-css").text_content(), css.decode())
            settled(page)
            observed = contract(page, served, http_start)
            self.assertEqual(observed, contracts[1], "CSS changed the legacy render contract")
            raw = page.screenshot(animations="disabled")
            artifact(label, ".png", raw)
            screenshots.append(Image.open(io.BytesIO(raw)).convert("RGBA"))
            frame_rows.append({"label": label, "sha256": digest(raw), "bytes": len(raw),
                               "dimensions": list(screenshots[-1].size), "css_sha256": digest(css),
                               "hover": observed["hover"], "focus": observed["focus"]})
        provenance = {"baseline": BASELINE, "baseline_app_sha256": digest(baseline_app),
                      "candidate_app_sha256": digest(self.pinned_app), "frames": frame_rows,
                      "cross_source_check": "DOM/image bytes/natural size/fallback/computed styles/ancestors/state",
                      "pixel_scope": "one loaded candidate-JS page, full CSS A-to-A and A-to-B"}
        artifact("css-controls", ".json", (json.dumps(provenance, sort_keys=True, indent=2) + "\n").encode())
        assert_identical_pixels(screenshots[0], screenshots[1])
        assert_identical_pixels(screenshots[0], screenshots[2])
        self.assertEqual(self.joins(), [])
        self.evidence.append({**provenance, "cross_source_render_contract_equal": True,
                              "same_page_css_aa_rgba_difference": 0, "same_page_css_ab_rgba_difference": 0,
                              "cross_fresh_page_avatar_raster_equality_claimed": False})
        page.close()
