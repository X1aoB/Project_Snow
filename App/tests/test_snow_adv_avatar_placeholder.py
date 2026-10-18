"""Real image events and cached-image placeholder contracts, entirely loopback."""
from __future__ import annotations

import io
import os
from unittest import TestCase, skipUnless

from tests import test_dialogue_v2_frontend as dialogue
from tests.test_snow_adv_base_map import MapBrowserFixture


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
        node = self.portrait(page)
        self.assertEqual(node.locator(".portrait-fallback").inner_text(), initial)
        self.assertTrue(node.locator(".portrait-fallback").is_visible())
        self.assertNotIn("has-image", node.get_attribute("class"))
        self.assertEqual(node.locator("img:visible").count(), 0)
        box = node.bounding_box()
        self.assertGreaterEqual(box["width"], 60)
        self.assertGreaterEqual(box["height"], 60)

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

    def test_original_ui_avatar_pixels_remain_identical_to_c8(self):
        self.assert_legacy_pixels(avatars=avatar, image_route=self.serve_image)
