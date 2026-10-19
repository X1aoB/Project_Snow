"""Local-only map regressions; synthetic dialogue, no provider or user data."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from tests import test_dialogue_v2_frontend as dialogue
from tests import test_public_frontend_e2e as fixture

APP = Path(__file__).resolve().parents[1]
PUBLIC = APP / "public_frontend"
BASELINE = "c8de7873df92d8e013dd3472666cb1e92ffce269"
PIN_LIGHT_TONE = "localStorage.setItem('project-snow-public:tone','light')"
ROSTER = [{"character_id": cid, "display_name": name, "aliases": [], "search_tokens": [],
           "avatar": None, "expression_manifest_url": f"/media/fixture/expressions/{cid}/manifest.json",
           "expression_state_count": 18, "license": "synthetic"}
          for cid, name in [(dialogue.CHARACTER, "凯茜娅"), (dialogue.OTHER, "安卡希雅")]
          + [(f"{index:012x}", f"合成角色{index:02d}") for index in range(3, 23)]]


def roster():
    # Default-tier collection must not depend on generated browser assets.
    rows = json.loads(json.dumps(ROSTER))
    for row in rows:
        row["expression_manifest_sha256"] = fixture.PublicFrontendHandler._expression_digest(row["character_id"])
    return rows


def assert_identical_pixels(before, after):
    from PIL import ImageChops
    # RGBA getbbox defaults to alpha-only, which hides RGB changes when both
    # screenshots are opaque. Every channel must participate in this gate.
    bounds = ImageChops.difference(before.convert("RGBA"), after.convert("RGBA")).getbbox(alpha_only=False)
    if bounds is not None:
        raise AssertionError(f"Screenshot pixels changed: {bounds}")


class ScreenshotDifferenceContractTests(TestCase):
    def test_one_rgb_or_alpha_pixel_change_cannot_pass_zero_difference(self):
        from PIL import Image
        before = Image.new("RGBA", (2, 2), (80, 90, 100, 255))
        assert_identical_pixels(before, before.copy())
        for changed in ((81, 90, 100, 255), (80, 90, 100, 254)):
            with self.subTest(changed=changed):
                after = before.copy()
                after.putpixel((0, 0), changed)
                with self.assertRaises(AssertionError):
                    assert_identical_pixels(before, after)


def world():
    now = datetime.now(timezone.utc)
    return {"revision": 1, "schedule_date": now.astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(), "analyst_location": None,
            "pending_rendezvous": {}, "presence": {row["character_id"]: {
                "location": "资料室" if index % 2 else "观景区", "activity": f"合成活动{index + 1}"}
                for index, row in enumerate(ROSTER)}}


# Wording the schedules and invitations actually use, one per roster entry; the last entry has no presence.
PLACEMENTS = [("观景区", "observation"), ("资料阅览区", "archive"), ("医务室附近", "medical"), ("基地走廊", "corridor"),
              ("个人房间", "quarters"), ("她的房间", "quarters"), ("基地休息区", "lounge"), ("基地公共区", "lounge"),
              ("餐厅", "canteen"), ("基地食堂", "canteen"), ("训练区", "training"), ("基地温泉", "hot_spring"),
              ("基地游戏厅", "arcade"), ("基地酒吧", "bar"), ("基地疗愈中心", "healing"), ("基地海滩", "beach"),
              ("商业街", "outside"), ("购物中心", "outside"), ("公园", "outside"), ("宿舍", "quarters"), ("我的房间", "quarters")]


def spread_world():
    """Every plan zone occupied once or more, with the analyst in the training area."""
    state = world()
    state["analyst_location"] = "训练区"
    state["presence"] = {row["character_id"]: {"location": where, "activity": f"合成活动{index}"}
                         for index, (row, (where, _)) in enumerate(zip(ROSTER, PLACEMENTS))}
    return state


class MapHandler(dialogue.DialogueHandler):
    """Reuse the existing dialogue protocol, adding only synthetic roster data."""

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/public/v1/characters":
            return self._json({"count": len(ROSTER), "characters": roster()})
        return super().do_GET()

    def _json(self, payload, status=200):
        if self.path == "/public/v2/scene/action" and "state_package" in payload:
            state = dialogue.untoken(payload["state_package"])
            seed = world()
            state["presence"] = {**seed["presence"], **state.get("presence", {})}
            state["schedule_date"] = seed["schedule_date"]
            state["expires_at"] = seed["expires_at"]
            payload["state_package"] = dialogue.token(state)
            payload["model_called"] = False
        return super()._json(payload, status)


class MapBrowserFixture:
    """Shared harness, deliberately containing no collected test methods."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MapHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.pinned_app = (PUBLIC / "app.js").read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        dialogue.DialogueV2FrontendTests.setUp(self)
        self.page.close()
        self.page = None
        for name in ("requests", "results", "counter", "scenario", "scope_rejected", "memory_enabled"):
            setattr(MapHandler, name, getattr(dialogue.DialogueHandler, name))
        self.blocked = []
        self.http = []
        self.asset_phases = []
        self.evidence = []

    def tearDown(self):
        try:
            self.assertEqual(self.errors, [])
            self.assertEqual(self.blocked, [])
            self.assertEqual([r for r in MapHandler.requests if r["path"].endswith("/chat/stream")], [])
            directory = os.getenv("SNOW_BASE_MAP_EVIDENCE_DIR")
            if directory:
                directory = Path(directory)
                directory.mkdir(parents=True, exist_ok=True)
                data = {"schema": "project-snow-base-map-synthetic-1", "technical_qa_only": True,
                        "human_approval": False, "actual_provider_calls": 0, "paid_calls": 0,
                        "app_sha256": hashlib.sha256(self.pinned_app).hexdigest(),
                        "map_asset_phases": self.asset_phases, "evidence": self.evidence}
                with (directory / (self._testMethodName + ".json")).open("x", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
        finally:
            dialogue.DialogueV2FrontendTests.tearDown(self)

    def new_page(self, *, width=1200, height=850, enabled=True, tone="light", script=None,
                 avatars=None, css=None, css_route=None, motion="off", reduced_motion="reduce"):
        page = self.browser.new_page(viewport={"width": width, "height": height},
                                     timezone_id="Asia/Hong_Kong", reduced_motion=reduced_motion)
        page.set_default_timeout(10000)
        # The scripts' execution order is otherwise unspecified. Pin light first
        # and set the requested final preferences in this single ordered script.
        initial = PIN_LIGHT_TONE + f";localStorage.setItem('project-snow-public:motion',{json.dumps(motion)});"
        initial += f"localStorage.setItem('project-snow-public:tone',{json.dumps(tone)});"
        page.add_init_script(initial + """(() => {
          const set = () => { if (!document.documentElement) return false;
            document.documentElement.dataset.snowTime='dusk'; return true; };
          if (!set()) { const observer=new MutationObserver(() => { if(set()) observer.disconnect(); });
            observer.observe(document,{childList:true}); }
        })();""")
        page.on("pageerror", lambda error: self.errors.append(type(error).__name__))
        page.on("request", lambda request: self.http.append((request.method, urlsplit(request.url).path)))

        def guard(route):
            request = route.request
            path = urlsplit(request.url).path
            if not request.url.startswith(self.base_url + "/"):
                self.blocked.append("external_request")
                route.abort()
            elif request.method != "GET" and not (request.method == "POST" and path == "/public/v2/scene/action"):
                self.blocked.append("unexpected_business_request")
                route.abort()
            else:
                route.continue_()

        page.route("**/*", guard)
        def map_asset(route):
            self.asset_phases.append({"path": "/modules/snow-base-map.js",
                "first_screen_settled": page.evaluate("() => window.__projectSnowTest.firstScreenSettled()")})
            route.continue_()
        page.route(self.base_url + "/modules/snow-base-map.js", map_asset)
        probe = b"""\nwindow.__baseMapFixture={setWorld:value=>{state.worldPackage=value;},
          setBusy:value=>{if(value)state.chatRequestByCharacter.set('synthetic','synthetic');else state.chatRequestByCharacter.delete('synthetic');},
          bindImages:selector=>bindAvatarImages(document.querySelector(selector)),visits:[]};
          if(typeof visitFromMap==='function') {const syntheticVisit=visitFromMap;
            visitFromMap=async characterId=>{const result=await syntheticVisit(characterId);
              window.__baseMapFixture.visits.push({characterId,result});return result;};}\n"""
        page.route(self.base_url + "/app.js*", lambda route: route.fulfill(
            content_type="text/javascript", body=(self.pinned_app if script is None else script) + probe))

        def config(route):
            payload = route.fetch().json()
            payload["features"] = {"ui_snow_adv": enabled, "chat_latency_v2": True, "motion_v2": True,
                                   "dialogue_domains_v2": True, "scene_actions_v2": True,
                                   "session_memory_v2": True, "verified_block_replay": True}
            payload["capabilities"] = {"dialogue_protocol": "public-dialogue-v2",
                "scene_action_protocol": "public-scene-action-v2", "recovery_only": True}
            route.fulfill(json=payload)

        page.route(self.base_url + "/public/v1/config", config)
        if avatars is not None:
            def characters(route):
                rows = roster()
                for row in rows:
                    row["avatar"] = avatars(row["character_id"])
                route.fulfill(json={"count": len(rows), "characters": rows})
            page.route(self.base_url + "/public/v1/characters", characters)
        if css is not None:
            page.route(self.base_url + "/snow-adv.css*", lambda route: route.fulfill(content_type="text/css", body=css))
        if css_route is not None:
            page.route(self.base_url + "/modules/snow-base-map.css", css_route)
        self.page = page
        return page

    def open_page(self, page, *, wait_until="networkidle"):
        page.goto(self.base_url, wait_until=wait_until)
        # Config and IndexedDB finish after DOMContentLoaded. Wait for the real
        # opening handshake instead of treating an as-yet absent lock as done.
        # Desktop Snow and flag-off both reach the existing notice directly.
        page.wait_for_function("""() => Boolean(document.querySelector('#snow-lock') ||
          document.querySelector('#experience-notice-dialog')?.open)""")
        unlocked = page.locator("#snow-lock").count() == 1
        if unlocked:
            page.locator("#snow-lock").wait_for(state="visible")
            page.locator("#snow-lock").focus()
            page.keyboard.press("Enter")
        page.locator("#snow-boot").wait_for(state="detached")
        page.locator("#snow-lock").wait_for(state="detached")
        page.locator("#experience-notice-dialog[open]").wait_for(state="visible")
        page.locator("#accept-experience-notice").click()
        page.locator("#experience-notice-dialog").wait_for(state="hidden")
        page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
        page.locator("#onboarding-guide").wait_for(state="visible")
        page.locator("#skip-onboarding").click()
        page.locator("#onboarding-guide").wait_for(state="hidden")
        self.evidence.append({"opening_handshake": {"lock_unlocked_by_enter": unlocked,
                              "notice_accepted_and_closed": True, "guide_skipped_and_hidden": True},
                              "viewport": page.viewport_size})
        self.close_contacts(page)
        return page

    @staticmethod
    def paint_settled(page):
        page.evaluate("() => document.fonts.ready")
        page.wait_for_function("""() => document.getAnimations().every(animation => {
          const timing=animation.effect?.getComputedTiming();
          return !timing || !Number.isFinite(timing.endTime) || ['finished','idle'].includes(animation.playState);
        })""")
        page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")

    @staticmethod
    def close_contacts(page):
        if page.viewport_size["width"] <= 820:
            if page.locator("#contact-panel.open").count():
                page.locator("#close-contacts").click()
            page.wait_for_function("""() => {const panel=document.querySelector('#contact-panel');
              return panel.inert && panel.getAttribute('aria-hidden')==='true' && panel.getBoundingClientRect().right<=0;}""")

    def open_map(self, page):
        page.locator("#open-snow-base-map").wait_for(state="attached")
        # An offscreen transformed drawer still satisfies Playwright is_visible.
        # Follow the product's actual expanded state before clicking its entry.
        if page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
            button = "#open-stage-contacts" if page.locator('#chat-app[data-channel="in_person"]').count() else "#open-contacts"
            page.locator(button).click()
            page.wait_for_function("""() => {const panel=document.querySelector('#contact-panel');
              return !panel.inert && panel.getAttribute('aria-hidden')==='false' && panel.getBoundingClientRect().right>0;}""")
        page.locator("#open-snow-base-map").click()
        page.locator("#snow-base-map[open]").wait_for(state="visible")

    def joins(self):
        return [r for r in MapHandler.requests if r["payload"].get("action") in {"join_character", "join_rendezvous"}]

    def enter_stage(self, page):
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.wait_for_function("() => !document.querySelector('#go-in-person').disabled")

    @staticmethod
    def text_contrast(page, selector):
        return page.locator(selector).first.evaluate("""node => {
          const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
          const paint=canvas.getContext('2d',{willReadFrequently:true});
          const rgba=color=>{paint.clearRect(0,0,1,1);paint.fillStyle=color;paint.fillRect(0,0,1,1);
            const a=[...paint.getImageData(0,0,1,1).data];return [a[0],a[1],a[2],a[3]/255];};
          const over=(f,b)=>{const a=f[3]+b[3]*(1-f[3]);
            return a?[0,1,2].map(i=>(f[i]*f[3]+b[i]*b[3]*(1-f[3]))/a).concat(a):[0,0,0,0];};
          let background=[0,0,0,0],opacity=1;
          for(let ancestor=node;ancestor;ancestor=ancestor.parentElement){
            const css=getComputedStyle(ancestor);opacity*=Number(css.opacity);
            if(background[3]<1)background=over(background,rgba(css.backgroundColor));
          }
          background=over(background,[255,255,255,1]);
          const css=getComputedStyle(node),foreground=rgba(css.color);foreground[3]*=opacity;
          const effective=over(foreground,background);
          const luminance=rgb=>rgb.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;})
            .reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
          const f=luminance(effective),b=luminance(background);
          return {color:css.color,background:background,foreground:foreground,
            opacityProduct:opacity,fontSize:parseFloat(css.fontSize),
            contrast:(Math.max(f,b)+.05)/(Math.min(f,b)+.05)};
        }""")

    @staticmethod
    def plan_label_contrast(page):
        return page.evaluate("""() => {
          const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
          const paint=canvas.getContext('2d',{willReadFrequently:true});
          const rgba=color=>{paint.clearRect(0,0,1,1);paint.fillStyle='rgba(0,0,0,0)';paint.fillStyle=color;paint.fillRect(0,0,1,1);
            const a=[...paint.getImageData(0,0,1,1).data];return [a[0],a[1],a[2],a[3]/255];};
          const over=(f,b)=>{const a=f[3]+b[3]*(1-f[3]);
            return a?[0,1,2].map(i=>(f[i]*f[3]+b[i]*b[3]*(1-f[3]))/a).concat(a):[0,0,0,0];};
          const luminance=rgb=>rgb.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;})
            .reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
          const field=rgba(getComputedStyle(document.querySelector('.snow-map-art .field')).fill);
          const rows=[];
          for (const zone of document.querySelectorAll('.snow-map-zone')) {
            const fill=getComputedStyle(document.querySelector(`.snow-map-art [data-zone="${zone.dataset.zone}"]`)).fill;
            const parsed=rgba(fill);
            if (!parsed[3] && !/^(none|transparent|rgba\\(0, 0, 0, 0\\))$/.test(fill)) {
              rows.push({role:`plan-${zone.dataset.zone}-unparsed-fill`, background:fill, fontSize:0, contrast:0});
              continue;
            }
            const background=over(parsed,over(field,[255,255,255,1]));
            for (const role of ['.snow-map-code','.snow-map-name']) {
              const node=zone.querySelector(role), css=getComputedStyle(node);
              const f=luminance(over(rgba(css.color),background)), b=luminance(background);
              rows.push({role:`plan-${zone.dataset.zone}${zone.dataset.active==='true'?'-active':''}${role}`,
                color:css.color, background:fill, fontSize:parseFloat(css.fontSize),
                contrast:(Math.max(f,b)+.05)/(Math.min(f,b)+.05)});
            }
          }
          return rows;
        }""")

    @staticmethod
    def baseline_source(name):
        result = subprocess.run(["git", "--no-replace-objects", "show", BASELINE + ":App/public_frontend/" + name],
                                cwd=APP.parent, capture_output=True, timeout=20, check=True)
        return result.stdout

    def assert_legacy_pixels(self, avatars=None, image_route=None):
        from PIL import Image
        images = []
        for old in (True, False):
            page = self.new_page(enabled=False, avatars=avatars,
                script=self.baseline_source("app.js") if old else self.pinned_app,
                css=self.baseline_source("snow-adv.css") if old else None)
            if image_route:
                page.route(self.base_url + "/synthetic-avatar/**", image_route)
            self.open_page(page)
            page.wait_for_function("() => window.__projectSnowTest.firstScreenSettled()")
            page.wait_for_function("""() => [...document.images].every(image => {
              const r=image.getBoundingClientRect();
              return !r.width || !r.height || r.bottom<=0 || r.top>=innerHeight || image.complete;
            })""")
            # complete/naturalWidth do not imply async decoding has reached paint.
            page.evaluate("""async () => {
              const visible=[...document.images].filter(image=>{const r=image.getBoundingClientRect();
                return r.width && r.height && r.bottom>0 && r.top<innerHeight && image.naturalWidth>0;});
              await Promise.all(visible.map(image=>image.decode()));
            }""")
            self.paint_settled(page)
            self.assertEqual(page.locator("#snow-base-map,#open-snow-base-map,#snow-base-map-style,.snow-empty-greeting").count(), 0)
            self.assertEqual([r for r in self.http if "snow-base-map" in r[1]], [])
            screenshot = page.screenshot(animations="disabled")
            images.append(Image.open(io.BytesIO(screenshot)).convert("RGBA"))
            directory = os.getenv("SNOW_BASE_MAP_EVIDENCE_DIR")
            if directory:
                directory = Path(directory)
                directory.mkdir(parents=True, exist_ok=True)
                label = self._testMethodName + ("-baseline" if old else "-candidate")
                with (directory / (label + ".png")).open("xb") as handle:
                    handle.write(screenshot)
                observed = page.locator("img").evaluate_all("""images=>images.map(image=>({
                  src:image.getAttribute('src'),complete:image.complete,width:image.naturalWidth,
                  height:image.naturalHeight,bound:image.dataset.bound||null,
                  portraitClass:image.closest('.portrait')?.className||null,
                  display:getComputedStyle(image).display,rect:image.getBoundingClientRect().toJSON()}))""")
                with (directory / (label + ".json")).open("x", encoding="utf-8") as handle:
                    json.dump({"images": observed, "screenshot_sha256": hashlib.sha256(screenshot).hexdigest()}, handle, indent=2)
            page.close()
        assert_identical_pixels(*images)
        self.evidence.append({"legacy_baseline": BASELINE, "whole_viewport_rgba_difference": 0})


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvBaseMapTests(MapBrowserFixture, TestCase):
    def test_twenty_two_local_presences_stale_missing_and_reopen_are_readonly(self):
        page = self.open_page(self.new_page())
        page.locator("#open-snow-base-map").wait_for(state="attached")
        self.paint_settled(page)
        start = len(self.http)
        self.open_map(page)
        self.paint_settled(page)
        self.assertEqual(self.asset_phases, [{"path": "/modules/snow-base-map.js", "first_screen_settled": True}])
        self.assertEqual(self.http[start:], [])
        self.assertEqual(page.locator(".snow-base-map-card").count(), 22)
        self.assertEqual(page.locator('.snow-base-map-card[data-presence="known"]').count(), 22)
        known = page.locator(".snow-base-map-location").all_inner_texts()
        self.assertTrue(all(line.startswith("位置：") for line in known), known)
        self.assertEqual(page.locator("#snow-base-map img").count(), 0)
        # 11 in each of two rooms: capacity 8 leaves 7 pins plus a "+4" chip per room.
        self.assertEqual(page.locator("#snow-base-map-plan").get_attribute("data-presence"), "known")
        self.assertEqual(page.locator(".snow-map-pin").count(), 14)
        self.assertEqual(page.locator(".snow-map-more").all_inner_texts(), ["+4", "+4"])
        self.assertEqual(page.locator('.snow-map-zone[data-occupied="true"]').evaluate_all(
            "zones => zones.map(zone => [zone.dataset.zone, zone.querySelector('.snow-map-count').textContent])"),
            [["observation", "11"], ["archive", "11"]])
        for state in ({}, {"presence": world()["presence"]},
                      {**world(), "schedule_date": "2000-01-01"},
                      {**world(), "expires_at": "2000-01-01T00:00:00Z"},
                      {"world": {"presence": world()["presence"], "exp": 1}}):
            page.locator("#snow-base-map-close").click()
            page.evaluate("value => window.__baseMapFixture.setWorld(value)", dialogue.token(state))
            self.open_map(page)
            if state.get("presence") or state.get("world", {}).get("presence"):
                self.assertEqual(page.locator('.snow-base-map-card[data-presence="stale"]').count(), 22)
                lines = page.locator(".snow-base-map-location").all_inner_texts()
                self.assertTrue(all(line.startswith("上次位置：") for line in lines), lines)
                self.assertEqual(page.locator("#snow-base-map-plan").get_attribute("data-presence"), "stale")
                self.assertEqual(set(page.locator(".snow-map-pin").evaluate_all(
                    "pins => pins.map(pin => getComputedStyle(pin).borderTopStyle)")), {"dashed"})
            else:
                self.assertEqual(page.locator('.snow-base-map-card[data-presence="missing"]').count(), 22)
                self.assertEqual(page.locator("#snow-base-map-plan").get_attribute("data-presence"), "missing")
                self.assertEqual(page.locator(".snow-map-pin,.snow-map-zone[data-occupied='true']").count(), 0)
        state = world()
        state["presence"].pop(ROSTER[-1]["character_id"])
        state["presence"][dialogue.CHARACTER]["location"] = '<img src="/unexpected" onerror="alert(1)">'
        page.locator("#snow-base-map-close").click()
        page.evaluate("value => window.__baseMapFixture.setWorld(value)", dialogue.token(state))
        self.open_map(page)
        self.assertEqual(page.locator('.snow-base-map-card[data-presence="known"]').count(), 21)
        self.assertEqual(page.locator('.snow-base-map-card[data-presence="missing"]').count(), 1)
        self.assertIn('<img src="/unexpected"', page.locator("#snow-base-map-list").inner_text())
        self.assertEqual(page.locator("#snow-base-map img").count(), 0)
        page.keyboard.press("Escape")
        page.locator("#snow-base-map").wait_for(state="hidden")
        self.assertEqual(page.evaluate("() => document.activeElement.id"), "open-snow-base-map")
        self.assertEqual(self.http[start:], [])
        self.assertEqual(self.joins(), [])
        self.evidence.append({"roster": 22, "stale_cases": 4, "missing_cases": 2,
                              "first_and_subsequent_open_close_http_delta": 0,
                              "module_requested_after_first_screen": True, "model_calls": 0})

    def test_entry_waits_for_styles_and_style_failure_does_not_mount(self):
        pending = []
        page = self.new_page(css_route=lambda route: pending.append(route))
        self.open_page(page, wait_until="domcontentloaded")
        page.wait_for_function("() => window.__projectSnowTest.firstScreenSettled()")
        page.wait_for_function("() => Boolean(document.querySelector('#snow-base-map-style'))")
        self.assertEqual(page.locator("#open-snow-base-map,#snow-base-map").count(), 0)
        self.assertEqual(len(pending), 1)
        pending.pop().fulfill(content_type="text/css", body=(PUBLIC / "modules/snow-base-map.css").read_bytes())
        page.locator("#open-snow-base-map").wait_for(state="attached")
        page.close()
        failed = self.new_page(css_route=lambda route: route.abort("failed"))
        self.open_page(failed)
        failed.wait_for_function("() => window.__projectSnowTest.firstScreenSettled()")
        self.assertEqual(failed.locator("#open-snow-base-map,#snow-base-map,#snow-base-map-style").count(), 0)

    def test_flag_off_keeps_baseline_pixels_and_never_requests_map_assets(self):
        self.assert_legacy_pixels()

    def test_mobile_same_character_confirmation_then_stage_switch_joins_once(self):
        page = self.open_page(self.new_page(width=390, height=844))
        self.open_map(page)
        page.locator("#snow-base-map-visit").evaluate("button => {button.click();button.click();}")
        page.locator("#presence-dialog[open]").wait_for(state="visible")
        self.assertEqual(self.joins(), [])
        self.assertEqual(page.locator("#contact-panel").get_attribute("aria-hidden"), "true")
        page.locator("#confirm-presence-transition").click()
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.wait_for_function("() => window.__baseMapFixture.visits.length===1")
        self.assertEqual(len(self.joins()), 1)
        self.open_map(page)
        page.locator(f'[data-base-map-select="{dialogue.OTHER}"]').click()
        page.locator("#snow-base-map-visit").click()
        page.wait_for_function("() => window.__baseMapFixture.visits.length===2")
        self.assertEqual(page.locator("#active-character h1").inner_text(), "安卡希雅")
        self.assertEqual(page.locator("#chat-app").get_attribute("data-channel"), "in_person")
        self.assertFalse(page.locator("#presence-dialog").evaluate("node => node.open"))
        rows = self.joins()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["payload"]["character_id"] for r in rows], [dialogue.CHARACTER, dialogue.OTHER])
        self.assertEqual(len({r["payload"]["request_id"] for r in rows}), 2)
        self.evidence.append({"confirmed_joins": 2, "duplicate_join_requests": 0, "paid_calls": 0})

    def test_failed_stage_switch_reconciles_original_join_without_new_join(self):
        page = self.open_page(self.new_page())
        self.enter_stage(page)
        self.open_map(page)
        page.locator(f'[data-base-map-select="{dialogue.OTHER}"]').focus()
        page.keyboard.press("Enter")
        self.assertEqual(page.locator(f'[data-base-map-select="{dialogue.OTHER}"]').get_attribute("aria-pressed"), "true")
        failed = []

        def reject_join(route):
            payload = route.request.post_data_json
            if payload.get("action") != "join_character":
                route.continue_()
                return
            recovery = route.request.headers.get("x-snow-recovery-only") == "1"
            failed.append({"request_id": payload["request_id"], "recovery": recovery,
                           "body_sha256": hashlib.sha256(route.request.post_data.encode()).hexdigest()})
            route.fulfill(status=410 if recovery else 503, json={"detail": {
                "code": "request_result_unavailable" if recovery else "request_failed", "retryable": False}})

        page.route("**/public/v2/scene/action", reject_join)
        page.locator("#snow-base-map-visit").click()
        page.wait_for_function("() => window.__baseMapFixture.visits.length===1")
        self.assertFalse(page.evaluate("() => window.__baseMapFixture.visits[0].result"))
        self.assertFalse(page.locator("#presence-dialog").evaluate("node => node.open"))
        self.assertEqual(len(self.joins()), 1)  # Only the first character's confirmed join reached the fixture.
        self.assertEqual([row["recovery"] for row in failed], [False, True])
        self.assertEqual(len({row["request_id"] for row in failed}), 1)
        self.assertEqual(len({row["body_sha256"] for row in failed}), 1)
        self.evidence.append({"failed_join_requests": failed, "new_join_after_uncertain_result": False})

    def test_busy_recheck_and_failed_resolve_cannot_report_a_successful_visit(self):
        page = self.open_page(self.new_page())
        self.open_map(page)
        page.evaluate("() => window.__baseMapFixture.setBusy(true)")
        count = len(MapHandler.requests)
        page.locator("#snow-base-map-visit").click()
        self.assertTrue(page.locator("#snow-base-map-visit").is_disabled())
        self.assertEqual(len(MapHandler.requests), count)
        page.evaluate("() => window.__baseMapFixture.setBusy(false)")
        page.locator("#snow-base-map-close").click()
        self.open_map(page)
        page.locator(f'[data-base-map-select="{dialogue.OTHER}"]').click()
        failures = []
        def fail(route):
            value = route.request.post_data_json
            failures.append((value["action"], value["request_id"], hashlib.sha256(route.request.post_data.encode()).hexdigest()))
            route.abort("failed")
        page.route("**/public/v2/scene/action", fail)
        page.locator("#snow-base-map-visit").click()
        page.wait_for_function("() => window.__baseMapFixture.visits.length===1")
        self.assertFalse(page.evaluate("() => window.__baseMapFixture.visits[0].result"))
        self.assertFalse(page.locator("#presence-dialog").evaluate("node => node.open"))
        self.assertEqual(page.locator("#active-character h1").inner_text(), "凯茜娅")
        self.assertEqual(len(failures), 3)
        self.assertEqual({row[0] for row in failures}, {"resolve"})
        self.assertEqual(len({row[1] for row in failures}), 1)
        self.assertEqual(len({row[2] for row in failures}), 1)
        self.assertEqual(self.joins(), [])

    def test_three_tones_desktop_mobile_fit_readably_and_motion_off_has_no_loops(self):
        for width, height in ((1440, 900), (390, 844)):
            for tone in ("light", "dark", "auto"):
                with self.subTest(width=width, tone=tone):
                    page = self.open_page(self.new_page(width=width, height=height, tone=tone))
                    if tone == "dark":
                        # Light and dusk cover two crowded rooms with "+N"; dark covers every zone plus the analyst marker.
                        page.locator("#open-snow-base-map").wait_for(state="attached")
                        page.evaluate("value => window.__baseMapFixture.setWorld(value)", dialogue.token(spread_world()))
                    self.open_map(page)
                    self.paint_settled(page)
                    page.keyboard.press("Tab")
                    self.assertTrue(page.evaluate("() => document.querySelector('#snow-base-map').contains(document.activeElement)"))
                    measured = page.locator("#snow-base-map").evaluate("""dialog => {
                      const box=node=>node.getBoundingClientRect(), r=box(dialog), list=dialog.querySelector('#snow-base-map-list');
                      const scroller=dialog.querySelector('.snow-map-scroller'), plan=dialog.querySelector('#snow-base-map-plan');
                      const pr=box(plan), sr=box(scroller), lr=box(list);
                      const zones=[...dialog.querySelectorAll('.snow-map-zone')];
                      const outside=zones.flatMap(zone=>{const z=box(zone);
                        return [...zone.querySelectorAll('.snow-map-pin,.snow-map-more,.snow-map-you,.snow-map-label,.snow-map-count')]
                          .filter(n=>{const b=box(n);return b.left<z.left-1||b.top<z.top-1||b.right>z.right+1||b.bottom>z.bottom+1;})
                          .map(n=>zone.dataset.zone+':'+n.className);});
                      const tallies=zones.map(zone=>{const shown=zone.querySelectorAll('.snow-map-pin').length;
                        const more=Number((zone.querySelector('.snow-map-more')?.textContent||'+0').slice(1));
                        return [zone.dataset.zone, shown+more, Number(zone.querySelector('.snow-map-count')?.textContent||0)];});
                      return {fontSizes:[...dialog.querySelectorAll('h2,h3,p,button')].map(n=>parseFloat(getComputedStyle(n).fontSize)),
                        horizontalOverflow:dialog.scrollWidth>dialog.clientWidth || list.scrollWidth>list.clientWidth,
                        inside:r.left>=0 && r.top>=0 && r.right<=innerWidth && r.bottom<=innerHeight,
                        zones:zones.length, aspect:pr.width/pr.height, outside, tallies,
                        zonesInPlan:zones.every(zone=>{const z=box(zone);return z.left>=pr.left-1&&z.top>=pr.top-1&&z.right<=pr.right+1&&z.bottom<=pr.bottom+1;}),
                        listBeside:lr.left>=sr.right, listBelow:lr.top>=sr.bottom,
                        pannable:scroller.scrollWidth>scroller.clientWidth, planHeight:pr.height,
                        animations:dialog.getAnimations({subtree:true}).length,
                        motion:document.documentElement.dataset.motionLevel,
                        loops:window.__projectSnowTest.snowProbe().activeLoops}; }""")
                    self.assertGreaterEqual(min(measured["fontSizes"]), 12)
                    self.assertFalse(measured["horizontalOverflow"])
                    self.assertTrue(measured["inside"])
                    self.assertEqual(measured["zones"], 14)
                    self.assertAlmostEqual(measured["aspect"], 1000 / 640, delta=0.02)
                    self.assertTrue(measured["zonesInPlan"])
                    self.assertEqual(measured["outside"], [])
                    self.assertTrue(all(shown == count for _, shown, count in measured["tallies"]), measured["tallies"])
                    if width == 1440:
                        # Side by side; the whole plan is visible without panning.
                        self.assertTrue(measured["listBeside"])
                        self.assertFalse(measured["pannable"])
                        page.locator('[data-map-zone="archive"]').click()
                        self.assertEqual(page.locator('.snow-map-group[data-active="true"]').get_attribute("data-zone"), "archive")
                        self.assertEqual(page.locator('.snow-map-art [data-zone="archive"]').get_attribute("data-active"), "true")
                        self.assertIn("B-02 资料室", page.locator("#snow-base-map-status").inner_text())
                        self.assertTrue(page.evaluate("""() => {const g=document.querySelector('.snow-map-group[data-zone="archive"] h3').getBoundingClientRect(),
                          list=document.querySelector('#snow-base-map-list').getBoundingClientRect();
                          return g.top>=list.top-1 && g.bottom<=list.bottom;}"""))
                    else:
                        # The plan keeps its proportions and pans sideways; the list sits below it.
                        self.assertTrue(measured["listBelow"])
                        self.assertTrue(measured["pannable"])
                        self.assertGreaterEqual(measured["planHeight"], 300)
                        last = page.locator('.snow-map-group:not([data-zone="unknown"]) .snow-base-map-card').last
                        last.focus()
                        page.keyboard.press("Enter")
                        self.assertEqual(last.get_attribute("aria-pressed"), "true")
                        self.assertTrue(page.evaluate("""() => {const n=document.activeElement.getBoundingClientRect(),
                          list=document.querySelector('#snow-base-map-list').getBoundingClientRect();
                          return n.top>=list.top && n.bottom<=list.bottom;}"""))
                        # The selected character's room is scrolled into the visible part of the plan.
                        self.assertTrue(page.evaluate("""() => {const zone=document.querySelector('.snow-map-zone[data-active="true"]');
                          const s=document.querySelector('.snow-map-scroller').getBoundingClientRect(), z=zone.getBoundingClientRect();
                          const middle=(z.left+z.right)/2;return middle>=s.left && middle<=s.right;}"""))
                    self.assertEqual(measured["animations"], 0)
                    self.assertEqual(measured["motion"], "off")
                    self.assertEqual(measured["loops"], 0)
                    self.evidence.append({"width": width, "tone": tone, **measured})
                    page.close()

    def test_floor_plan_places_locations_like_stage_scenes_and_marks_the_analyst(self):
        placements = PLACEMENTS
        rooms = {"observation", "archive", "medical", "corridor", "quarters", "lounge", "canteen", "training"}
        state = spread_world()
        page = self.open_page(self.new_page())
        page.locator("#open-snow-base-map").wait_for(state="attached")
        page.evaluate("value => window.__baseMapFixture.setWorld(value)", dialogue.token(state))
        self.paint_settled(page)
        start = len(self.http)
        self.open_map(page)
        scene_keys = page.evaluate("""async places => {const {sceneVisualKey}=await import('/dialogue-context.js');
          return places.map(place => sceneVisualKey(place));}""", [where for where, _ in placements])
        for (where, zone), scene in zip(placements, scene_keys):
            with self.subTest(location=where):
                # Rooms follow the stage scene mapping; annexes and the outside are scenes the stage draws as generic.
                # The stage has no scene for "她的房间/我的房间", so the plan files them under 宿舍 by name.
                generic = zone not in rooms or where in {"她的房间", "我的房间"}
                self.assertEqual(scene, "generic" if generic else zone)
        for row, (where, zone) in zip(ROSTER, placements):
            cid = row["character_id"]
            with self.subTest(location=where, zone=zone):
                self.assertEqual(page.locator(f'.snow-map-group[data-zone="{zone}"] [data-base-map-select="{cid}"]').count(), 1)
                self.assertEqual(page.locator(f'.snow-map-zone[data-zone="{zone}"] [data-map-pin="{cid}"]').count(), 1)
        missing = ROSTER[-1]["character_id"]
        self.assertEqual(page.locator(f'.snow-map-group[data-zone="unknown"] [data-base-map-select="{missing}"]').count(), 1)
        self.assertEqual(page.locator(f'[data-map-pin="{missing}"]').count(), 0)
        self.assertIn("1 位还没有位置记录", page.locator(".snow-map-legend").inner_text())
        self.assertEqual(page.locator(".snow-map-you").count(), 1)
        self.assertEqual(page.locator('.snow-map-zone[data-zone="training"] .snow-map-you').count(), 1)
        self.assertTrue(page.locator('[data-map-zone="training"]').get_attribute("aria-label").endswith("你在这里"))
        self.assertEqual(page.locator('.snow-map-zone[data-occupied="true"]').count(), 14)
        rings = page.locator(".snow-map-pin").evaluate_all("""pins => pins.map(pin => {
          const probe=document.createElement('i');probe.style.color='var(--char)';pin.append(probe);
          const char=getComputedStyle(probe).color;probe.remove();
          return [getComputedStyle(pin).borderTopColor===char, pin.dataset.snowCharacter===pin.dataset.mapPin];})""")
        self.assertEqual(len(rings), 21)
        self.assertTrue(all(all(ring) for ring in rings), rings)
        # A location that only repeats its area name is not printed twice; other wording stays readable.
        place = {where: page.locator(f'[data-base-map-select="{row["character_id"]}"] .snow-base-map-location')
                 for row, (where, _) in zip(ROSTER, placements)}
        self.assertFalse(place["基地休息区"].is_visible())
        for where in ("个人房间", "医务室附近", "商业街"):
            self.assertTrue(place[where].is_visible(), where)
        lounge = ROSTER[placements.index(("基地休息区", "lounge"))]
        page.locator(f'[data-map-pin="{lounge["character_id"]}"]').click()
        self.assertEqual(page.locator(f'[data-base-map-select="{lounge["character_id"]}"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator('.snow-map-art [data-zone="lounge"]').get_attribute("data-active"), "true")
        self.assertTrue(page.locator("#snow-base-map-status").inner_text().startswith(lounge["display_name"]))
        self.assertFalse(page.locator("#snow-base-map-visit").is_disabled())
        self.assertEqual([r for r in self.http[start:] if not r[1].startswith("/dialogue-context.js")], [])
        self.assertEqual(self.joins(), [])
        self.evidence.append({"placements": len(placements), "analyst_zone": "training", "scene_mapping_agrees": True,
                              "http_during_open": 0, "model_calls": 0})

    def test_actual_three_tones_entry_states_and_map_text_have_contrast(self):
        for width, height in ((1440, 900), (390, 844)):
            for tone in ("light", "dark", "auto"):
                with self.subTest(width=width, tone=tone):
                    page = self.open_page(self.new_page(width=width, height=height, tone=tone))
                    page.locator("#open-snow-base-map").wait_for(state="attached")
                    if page.locator("#contact-panel").get_attribute("aria-hidden") == "true":
                        page.locator("#open-contacts").click()
                    self.paint_settled(page)
                    actual = page.evaluate("""() => ({tone:document.documentElement.dataset.tone,
                      time:document.documentElement.dataset.toneTime||null,
                      preference:localStorage.getItem('project-snow-public:tone')})""")
                    self.assertEqual(actual, {"tone": "light" if tone == "light" else "dark",
                                              "time": "dusk" if tone == "auto" else None, "preference": tone})
                    rows = []
                    entry = page.locator("#open-snow-base-map")
                    page.mouse.move(0, 0)
                    rows.append({"role": "entry-default", **self.text_contrast(page, "#open-snow-base-map")})
                    entry.hover()
                    rows.append({"role": "entry-hover", **self.text_contrast(page, "#open-snow-base-map")})
                    page.mouse.move(0, 0)
                    entry.focus()
                    page.keyboard.press("Shift+Tab")
                    page.keyboard.press("Tab")
                    self.assertTrue(entry.evaluate("node => node.matches(':focus-visible')"))
                    rows.append({"role": "entry-focus-visible", **self.text_contrast(page, "#open-snow-base-map")})
                    entry.click()
                    self.paint_settled(page)
                    skin = page.locator("#snow-base-map").evaluate("""dialog => {
                      const css=(node, pseudo)=>getComputedStyle(node, pseudo||null);
                      const visit=dialog.querySelector('#snow-base-map-visit'), close=dialog.querySelector('#snow-base-map-close');
                      const other=document.querySelector('.icon-button:not(.snow-base-map-close)');
                      const card=dialog.querySelector('.snow-base-map-card'), initial=card.querySelector('.snow-base-map-initial');
                      const probe=document.createElement('i');probe.style.color='var(--char)';card.append(probe);
                      const char=css(probe).color;probe.remove();
                      return {modal:dialog.classList.contains('modal'), backdrop:css(dialog,'::backdrop').backgroundColor,
                        visitBackground:css(visit).backgroundColor, visitColor:css(visit).color,
                        visitRadius:css(visit).borderTopLeftRadius, closeRadius:css(close).borderTopLeftRadius,
                        iconButtonRadius:css(other).borderTopLeftRadius, kicker:dialog.querySelector('header .panel-kicker')?.textContent,
                        char, initialBorder:css(initial).borderTopColor, initialBorderWidth:css(initial).borderTopWidth,
                        cardCharacter:card.dataset.snowCharacter===card.dataset.characterId}; }""")
                    # Same skins as the other dialogs and primary buttons in every tone.
                    self.assertEqual(skin["modal"], True)
                    self.assertEqual(skin["backdrop"], "rgba(31, 45, 58, 0.24)")
                    self.assertEqual((skin["visitBackground"], skin["visitColor"]), ("rgb(58, 124, 165)", "rgb(255, 255, 255)"))
                    self.assertEqual(skin["visitRadius"], "999px")
                    self.assertEqual(skin["closeRadius"], skin["iconButtonRadius"])
                    self.assertEqual(skin["kicker"], "本机记录")
                    self.assertEqual((skin["initialBorder"], skin["initialBorderWidth"], skin["cardCharacter"]), (skin["char"], "2px", True))
                    self.evidence.append({"width": width, "requested_tone": tone, "skin": skin})
                    page.locator("#snow-base-map-visit").hover()
                    self.paint_settled(page)
                    rows.append({"role": "visit-hover", **self.text_contrast(page, "#snow-base-map-visit")})
                    page.mouse.move(0, 0)
                    self.paint_settled(page)
                    for selector in ("#snow-base-map-title", "#snow-base-map-summary", "#snow-base-map-status", "#snow-base-map-visit"):
                        rows.append({"role": selector, **self.text_contrast(page, selector)})
                    for selected in ("true", "false"):
                        for role in ("name", "initial", "location", "activity"):
                            selector = f'.snow-base-map-card[aria-pressed="{selected}"] .snow-base-map-{role}'
                            rows.append({"role": f"card-{selected}-{role}", **self.text_contrast(page, selector)})
                    for selector in (".snow-map-group-title", ".snow-map-group-title .snow-map-code",
                                     ".snow-map-legend .snow-map-key", ".snow-map-key-note", ".snow-map-more", ".snow-map-count"):
                        rows.append({"role": selector, **self.text_contrast(page, selector)})
                    # Plan labels sit on SVG room fills, not on a DOM ancestor background; measure against the fill.
                    page.locator('[data-map-zone="archive"]').click()
                    self.paint_settled(page)
                    rows.extend(self.plan_label_contrast(page))
                    self.evidence.append({"width": width, "requested_tone": tone, "actual_tone": actual, "contrast": rows})
                    for row in rows:
                        self.assertGreaterEqual(row["fontSize"], 12, row)
                        self.assertGreaterEqual(row["contrast"], 4.5, row)
                    page.close()
