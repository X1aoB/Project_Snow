"""Synthetic browser regressions for the immersive controls; no provider calls."""
from __future__ import annotations

import os
from pathlib import Path
from unittest import TestCase, skipUnless

from tests import test_snow_adv_ui as skin
from tests import test_dialogue_v2_frontend as dialogue


def capture(page, name):
    directory = os.getenv("PROJECT_SNOW_UI_QA_DIR")
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / name))


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class ImmersiveControlsTests(TestCase):
    setUpClass = classmethod(skin.SnowAdvStageTests.setUpClass.__func__)
    tearDownClass = classmethod(skin.SnowAdvStageTests.tearDownClass.__func__)
    setUp = skin.SnowAdvStageTests.setUp
    tearDown = skin.SnowAdvStageTests.tearDown
    _page = skin.SnowAdvStageTests._page
    _open = skin.SnowAdvStageTests._open
    _reach_in_person = skin.SnowAdvStageTests._reach_in_person

    def face(self, width=1280):
        page = self._page(width=width, height=850)
        self._open(page, True)
        self._reach_in_person(page)
        page.wait_for_function("!document.getElementById('toggle-action').disabled")
        return page

    def test_contacts_close_escape_and_focus_at_both_breakpoints(self):
        for width in (390, 820, 821, 1280):
            with self.subTest(width=width):
                page = self.face(width)
                page.locator("#open-stage-contacts").click()
                close = page.locator("#close-contacts")
                close.wait_for(state="visible")
                page.wait_for_function("document.activeElement.id==='close-contacts'")
                page.keyboard.press("Shift+Tab")
                self.assertTrue(page.evaluate("document.getElementById('contact-panel').contains(document.activeElement)"))
                page.keyboard.press("Escape")
                self.assertEqual(page.locator("#contact-panel").get_attribute("aria-hidden"), "true")
                self.assertTrue(page.locator("#contact-panel").evaluate("node=>node.inert"))
                self.assertEqual(page.evaluate("document.activeElement.id"), "open-stage-contacts")
                page.locator("#open-stage-contacts").click()
                close.click()
                self.assertEqual(page.locator("#open-stage-contacts").get_attribute("aria-expanded"), "false")
                self.assertEqual(page.evaluate("document.activeElement.id"), "open-stage-contacts")
                page.close()
        self.assertFalse(self.errors)

    def test_action_popover_does_not_resize_stage_and_closes_without_sending(self):
        for width in (390, 820, 821, 1280):
            with self.subTest(width=width):
                page = self.face(width)
                before = page.locator("#scene-stage").bounding_box()
                calls = len(skin.fixture.PublicFrontendHandler.chat_payloads)
                page.locator("#toggle-action").click()
                field = page.locator("#analyst-action-field")
                self.assertTrue(field.is_visible())
                self.assertEqual(page.locator("dialog[open]").count(), 0)
                after = page.locator("#scene-stage").bounding_box()
                self.assertAlmostEqual(before["height"], after["height"], delta=1)
                box = field.bounding_box()
                button = page.locator("#toggle-action").bounding_box()
                self.assertLessEqual(box["y"] + box["height"], button["y"] + 1)
                self.assertGreaterEqual(box["x"], 0)
                self.assertLessEqual(box["x"] + box["width"], width)
                page.locator("#action-input").fill("合成动作草稿")
                if width in (390, 1280):
                    capture(page, f"action-{width}.png")
                page.keyboard.press("Escape")
                self.assertTrue(field.is_hidden())
                self.assertEqual(page.evaluate("document.activeElement.id"), "toggle-action")
                self.assertEqual(page.locator("#toggle-action .composer-tool-label").inner_text(), "已有动作")
                self.assertTrue(page.locator(".action-draft-dot").is_visible())
                page.locator("#toggle-action").click()
                self.assertEqual(page.locator("#action-input").input_value(), "合成动作草稿")
                page.locator("#message-input").click()
                self.assertTrue(field.is_hidden())
                self.assertEqual(page.evaluate("document.activeElement.id"), "message-input")
                self.assertEqual(len(skin.fixture.PublicFrontendHandler.chat_payloads), calls)
                page.close()
        self.assertFalse(self.errors)

    def test_action_draft_survives_close_and_reload_without_enter_autosend(self):
        page = self.face()
        before = len(skin.fixture.PublicFrontendHandler.chat_payloads)
        page.locator("#toggle-action").click()
        page.locator("#action-input").fill("合成保留动作")
        page.keyboard.press("Enter")
        page.locator("#close-action").click()
        self.assertEqual(page.evaluate("document.activeElement.id"), "toggle-action")
        page.wait_for_function("""() => new Promise(resolve => {
          const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>{
            const db=r.result,q=db.transaction('app_state','readonly').objectStore('app_state').get('drafts');
            q.onsuccess=()=>{resolve(Object.values(q.result?.values||{}).some(v=>v.includes('合成保留动作')));db.close();};};
        })""")
        page.reload(wait_until="networkidle")
        page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person' && document.getElementById('connection-status').textContent==='服务已连接'")
        self.assertTrue(page.locator("#analyst-action-field").is_hidden())
        page.locator("#toggle-action").click()
        self.assertEqual(page.locator("#action-input").input_value(), "合成保留动作\n")
        self.assertEqual(len(skin.fixture.PublicFrontendHandler.chat_payloads), before)
        self.assertFalse(self.errors)

    def test_empty_avatar_uses_image_and_round_stage_avatar_keeps_fallback(self):
        def provide_avatar(page):
            def characters(route):
                payload = route.fetch().json()
                payload["characters"][0]["avatar"] = {
                    "thumbnail_src": "/shared/brand/xiaoji-icon-192.png",
                    "src": "/shared/brand/xiaoji-icon-192.png",
                }
                route.fulfill(json=payload)
            page.route("**/public/v1/characters", characters)
        page = self._page()
        provide_avatar(page)
        self._open(page, True)
        page.locator("#snow-boot").wait_for(state="detached")
        page.locator("#accept-experience-notice").click()
        avatar = page.locator(".snow-empty-avatar")
        avatar.wait_for(state="visible")
        image = avatar.locator("img")
        self.assertEqual(image.count(), 1)
        self.assertEqual(image.get_attribute("data-bound"), "1")
        image.evaluate("node=>node.src='/synthetic-missing-avatar.webp'")
        image.wait_for(state="detached")
        self.assertFalse("has-image" in avatar.get_attribute("class"))
        self.assertTrue(avatar.locator(".portrait-fallback").is_visible())
        self.assertEqual(avatar.evaluate("node=>getComputedStyle(node).borderRadius"), "50%")
        page.close()
        page = self._page()
        provide_avatar(page)
        self._open(page, True)
        self._reach_in_person(page)
        stage = page.locator("#stage-header-avatar")
        self.assertEqual(stage.evaluate("node=>getComputedStyle(node).borderRadius"), "50%")
        stage.locator("img").evaluate("node=>node.src='/synthetic-missing-stage-avatar.webp'")
        stage.locator("img").wait_for(state="detached")
        self.assertTrue(stage.locator(".portrait-fallback").is_visible())
        self.assertFalse(self.errors)


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class ImmersiveInvitationTests(TestCase):
    setUpClass = classmethod(dialogue.DialogueV2FrontendTests.setUpClass.__func__)
    tearDownClass = classmethod(dialogue.DialogueV2FrontendTests.tearDownClass.__func__)
    setUp = dialogue.DialogueV2FrontendTests.setUp
    tearDown = dialogue.DialogueV2FrontendTests.tearDown
    open = dialogue.DialogueV2FrontendTests.open
    send = dialogue.DialogueV2FrontendTests.send

    def face(self):
        self.open(snow=True)
        self.page.locator("#go-in-person").click()
        self.page.locator("#confirm-presence-transition").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='in_person' && !document.querySelector('#send-message').disabled")

    def test_face_invitation_chip_and_action_popover_are_exclusive_nonmodal(self):
        self.face()
        for width in (390, 820, 821, 1280):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 850})
                self.send("合成未来邀请 accepted 明天去餐厅")
                root = self.page.locator("#current-rendezvous")
                self.assertEqual(root.evaluate("node=>node.parentElement.id"), "scene-invitation-anchor")
                self.assertEqual(root.locator("#scene-invitation-details").count(), 0)
                before = self.page.locator("#scene-stage").bounding_box()
                root.locator('[data-current-rendezvous="open"]').click()
                details = self.page.locator("#scene-invitation-details")
                self.assertTrue(details.is_visible())
                hud = self.page.locator(".scene-hud").bounding_box()
                self.assertGreaterEqual(root.bounding_box()["y"], hud["y"] + hud["height"])
                self.assertIn("时间待定", details.inner_text())
                self.assertEqual(root.locator('[data-current-rendezvous="go"]').count(), 0)
                self.assertEqual(self.page.locator("dialog[open]").count(), 0)
                if width in (390, 1280):
                    capture(self.page, f"invitation-{width}.png")
                self.assertAlmostEqual(before["height"], self.page.locator("#scene-stage").bounding_box()["height"], delta=1)
                self.page.keyboard.press("Escape")
                self.assertEqual(self.page.evaluate("document.activeElement.dataset.currentRendezvous"), "open")
                self.page.locator("#toggle-action").click()
                self.assertTrue(self.page.locator("#analyst-action-field").is_visible())
                root.locator('[data-current-rendezvous="open"]').click()
                self.assertTrue(self.page.locator("#analyst-action-field").is_hidden())
                self.page.locator("#toggle-action").click()
                self.assertEqual(details.count(), 0)
                self.page.locator("#message-input").click()
                self.assertTrue(self.page.locator("#analyst-action-field").is_hidden())
        self.assertFalse(self.errors)

    def test_old_completed_invitation_requires_exact_event_and_current_location(self):
        def expose(route):
            response = route.fetch()
            route.fulfill(response=response, body=response.text() + "\nwindow.__uiProof={completed:alreadyCompletedJointInvitation, world:()=>state.worldPackage};")
        self.page.route("**/app.js*", expose)
        self.face()
        result = self.page.evaluate("""() => {
          const before=window.__uiProof.world();
          const part=before.split('.')[0];const world=JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(part.replaceAll('-','+').replaceAll('_','/')),c=>c.charCodeAt(0))));
          const characterId='25b23cb64398', location=world.presence[characterId].location;
          const invitation={status:'accepted',time_scope:'now',invitation_id:'invite:synthetic-event',location_id:'synthetic_place'};
          const context={character_id:characterId,scene_overlay:{source_event_id:'synthetic-event',location_id:'synthetic_place',communication_channel:'in_person',analyst_location:location,character_location:location,pending_rendezvous:null}};
          const check=(i=invitation,c=context)=>window.__uiProof.completed(i,c);
          const rejected=[check({...invitation,time_scope:'future'}),check({...invitation,status:'proposed'}),
            check({...invitation,invitation_id:'invite:other'}),check({...invitation,location_id:'other'}),
            check(invitation,{...context,scene_overlay:{...context.scene_overlay,source_event_id:''}}),
            check(invitation,{...context,scene_overlay:{...context.scene_overlay,analyst_location:'other'}}),
            check(invitation,{...context,scene_overlay:{...context.scene_overlay,analyst_location:'other',character_location:'other'}})];
          return {exact:check(),rejected,unchanged:before===window.__uiProof.world()};
        }""")
        self.assertTrue(result["exact"])
        self.assertEqual(result["rejected"], [False] * 7)
        self.assertTrue(result["unchanged"])
        self.assertFalse(self.errors)

    def test_action_drafts_remain_separate_when_switching_face_characters(self):
        self.face()
        for character_id, expected, value in (
            (dialogue.CHARACTER, "", "合成甲动作"),
            (dialogue.OTHER, "", "合成乙动作"),
            (dialogue.CHARACTER, "合成甲动作", None),
        ):
            if self.page.locator(f'[data-character="{character_id}"]').get_attribute("aria-current") != "true":
                self.page.locator("#open-stage-contacts").click()
                self.page.locator(f'[data-character="{character_id}"]').click()
                self.page.wait_for_function("id=>document.querySelector(`[data-character='${id}']`).getAttribute('aria-current')==='true' && document.getElementById('chat-app').dataset.channel==='in_person' && !document.getElementById('toggle-action').disabled", arg=character_id)
            self.assertTrue(self.page.locator("#analyst-action-field").is_hidden())
            self.page.locator("#toggle-action").click()
            self.assertEqual(self.page.locator("#action-input").input_value(), expected)
            if value is not None:
                self.page.locator("#action-input").fill(value)
            self.page.locator("#close-action").click()
        self.assertFalse(self.errors)

    def test_joint_notice_expires_once_and_does_not_follow_another_character(self):
        def expose(route):
            response = route.fetch()
            route.fulfill(response=response, body=response.text() + "\nwindow.__uiNotice=showJointMovementNotice;")
        self.page.route("**/app.js*", expose)
        self.face()
        payload = {"characterId": dialogue.CHARACTER, "requestId": "synthetic-joint-event", "locationName": "合成餐厅"}
        self.page.evaluate("value=>window.__uiNotice(value)", payload)
        notice = self.page.locator("#joint-movement-notice")
        self.assertTrue(notice.is_visible())
        self.assertIn("合成餐厅", notice.inner_text())
        notice.wait_for(state="hidden", timeout=5000)
        self.page.evaluate("value=>window.__uiNotice(value)", payload)
        self.assertTrue(notice.is_hidden())
        self.page.evaluate("value=>window.__uiNotice({...value,requestId:'synthetic-second-event'})", payload)
        self.page.locator("#open-communicator").click()
        self.page.wait_for_function("document.querySelector('#chat-app').dataset.channel==='text'")
        self.assertTrue(notice.is_hidden())
        self.assertFalse(self.errors)
