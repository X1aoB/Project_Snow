"""雪境 ADV 独立场景的专属背景与换场白雪转场。

背景和转场增强在按需加载的 modules/snow-scenes.js。这里检查：每个可去的地点都映射到自己的场景（前后端一致），
每个新场景在四个时段都画出自己的背景；「一起去」换场时先让她把这句话说完、再完整播放白雪转场，
不会再被回复打断成硬切；减少动态时直接换场；开关关闭时不加载新模块、旧界面背景照旧回退。
"""
from __future__ import annotations

import io
import json
import os
import threading
from http.server import ThreadingHTTPServer
from unittest import TestCase, skipUnless
from urllib.parse import urlsplit

from backend.snow_app.mvp_policy import MVP_CHARACTERS, scene_templates_for, scene_visual_key
from backend.snow_app.public_service import _CONTROLLED_JOINT_LOCATIONS
from tests import test_dialogue_v2_frontend as dialogue
from tests import test_public_frontend_e2e as fixture

LEGACY = {"generic", "quarters", "lounge", "training", "archive", "canteen", "observation", "medical", "corridor"}
PLACES = {"beach", "hot_spring", "arcade", "bar", "healing", "park", "street", "mall", "analyst"}
# 可邀请地点（含按角色解析的房间名）对应的舞台场景；基地食堂只为旧状态保留，同属食堂场景。
CATALOG = {"商业街": "street", "购物中心": "mall", "公园": "park", "基地餐厅": "canteen", "基地食堂": "canteen",
           "基地休息区": "lounge", "观景区": "observation", "训练区": "training", "资料室": "archive", "基地海滩": "beach",
           "基地游戏厅": "arcade", "基地温泉": "hot_spring", "基地疗愈中心": "healing", "基地酒吧": "bar",
           "肴的房间": "quarters", "她的房间": "quarters", "分析员的房间": "analyst", "我的房间": "analyst", "基地外": "generic"}
FEATURES = {"ui_snow_adv": True, "chat_latency_v2": True, "motion_v2": True, "dialogue_domains_v2": True,
            "scene_actions_v2": True, "session_memory_v2": True, "verified_block_replay": True}
LINE = "好啊，一起去海边走走吧。今天阳光正好，海风应该也不会太冷。"


class SceneKeyMappingTests(TestCase):
    def test_every_public_destination_has_its_own_scene(self) -> None:
        for location_id, definition in _CONTROLLED_JOINT_LOCATIONS.items():
            with self.subTest(location_id=location_id):
                self.assertEqual(scene_visual_key(definition["location"]), CATALOG[definition["location"]])
        for name, key in CATALOG.items():
            with self.subTest(name=name):
                self.assertEqual(scene_visual_key(name), key)
                self.assertIn(key, LEGACY | PLACES)

    def test_character_presence_locations_keep_their_base_rooms(self) -> None:
        for character in MVP_CHARACTERS:
            for location, _ in scene_templates_for(character.character_id):
                with self.subTest(character=character.display_name, location=location):
                    self.assertIn(scene_visual_key(location), LEGACY - {"generic"})


class JointHandler(dialogue.DialogueHandler):
    """In person at 观景区, the message 合成一起去海滩 moves both to 基地海滩 like a real joint movement."""

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(raw or b"{}")
        chat = payload.get("chat", payload)
        if self.path != "/public/v2/chat/stream" or "合成一起去海滩" not in chat.get("message", ""):
            self.rfile = io.BytesIO(raw)
            return super().do_POST()
        type(self).requests.append({"path": self.path, "payload": payload, "recovery": False})
        world = dialogue.untoken(chat.get("state_package", ""))
        world["revision"] += 1
        world["presence"][chat["character_id"]].update(location="基地海滩", activity="和分析员一起在海滩散步")
        world["analyst_location"] = "基地海滩"
        context = dialogue.untoken(payload.get("session_context_package", ""))
        epoch = context.get("scene_epoch", 0) + 1
        context.update(schema_version="public-session-context-1", character_id=chat["character_id"], segment_id=payload["segment_id"],
                       revision=context.get("revision", 0) + 1, scene_epoch=epoch, local_day_key=world["schedule_date"],
                       scene_overlay={"communication_channel": "in_person", "scene_epoch": epoch})
        scene = {"character_location": "基地海滩", "character_activity": "和分析员一起在海滩散步", "analyst_location": "基地海滩",
                 "co_located": True, "visual_key": scene_visual_key("基地海滩")}
        result = {"request_id": chat["request_id"], "character_id": chat["character_id"], "state_package": dialogue.token(world),
                  "scene_state": scene, "state_event": {"event_type": "joint_movement", "location_name": "基地海滩"},
                  "session_context_package": dialogue.token(context), "session_memories": [], "scene_action": {"status": "not_requested"},
                  "movement_status": {"status": "moved", "location_name": "基地海滩"}, "communication_channel": "in_person",
                  "content_blocks": [{"type": "speech", "text": LINE}], "usage": {"provider_calls": 1, "total_tokens": 10}}
        type(self).results[chat["request_id"]] = result
        body = ("event: state\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n"
                + "event: done\ndata: " + json.dumps(result, ensure_ascii=False) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# 换场过程的记录：转场相位、舞台世界当前场景、地点胶囊、台词长度、提示是否出现。
RECORDER = """() => {
  const veil = document.getElementById('snow-scene-veil'), log = [];
  const far = () => document.querySelector('#scene-stage .snow-layer-far')?.dataset.scene;
  const take = what => log.push({ what, t: Math.round(performance.now()), phase: veil.dataset.phase, far: far(),
    pill: document.getElementById('stage-location').textContent, speech: document.getElementById('stage-speech').textContent.length,
    notice: !document.getElementById('joint-movement-notice').hidden });
  new MutationObserver(() => take('veil')).observe(veil, { attributes: true, attributeFilter: ['data-phase'] });
  new MutationObserver(() => take('far')).observe(document.querySelector('#scene-stage .snow-layer-room'), { childList: true });
  new MutationObserver(() => take('notice')).observe(document.getElementById('joint-movement-notice'), { attributes: true });
  const tick = setInterval(() => take('tick'), 60);
  window.__sceneLog = { log, stop: () => clearInterval(tick) };
}"""


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser regression tier")
class SnowAdvScenesTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), JointHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)

    def setUp(self):
        handler = JointHandler
        handler.requests = []; handler.results = {}; handler.counter = 0
        handler.scope_rejected = False; handler.memory_enabled = True; handler.scenario = "success"
        handler.join_started = threading.Event(); handler.join_release = threading.Event()
        self.playwright = fixture.sync_playwright().start()
        self.browser = fixture._launch_browser(self.playwright)
        self.errors, self.requested = [], []

    def tearDown(self):
        JointHandler.join_release.set()
        try:
            self.browser.close()
        finally:
            self.playwright.stop()

    def page(self, width=1280, height=820, tone="light", enabled=True, motion="auto"):
        page = self.browser.new_page(viewport={"width": width, "height": height}, timezone_id="Asia/Hong_Kong",
                                     reduced_motion="reduce" if motion == "off" else "no-preference")
        page.set_default_timeout(15000)
        stored = {"light": "light", "dark": "dark"}[tone]
        page.add_init_script(f"localStorage.setItem('project-snow-public:tone',{json.dumps(stored)});" + """(() => {
          const set = () => { if (!document.documentElement) return false; document.documentElement.dataset.snowTime='day'; return true; };
          if (!set()) { const o=new MutationObserver(() => { if (set()) o.disconnect(); }); o.observe(document,{childList:true}); } })();""")
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("request", lambda request: self.requested.append(urlsplit(request.url).path))

        def config(route):
            payload = route.fetch().json()
            payload["features"] = {**FEATURES, "ui_snow_adv": enabled}
            payload["capabilities"] = {"dialogue_protocol": "public-dialogue-v2", "scene_action_protocol": "public-scene-action-v2", "recovery_only": True}
            route.fulfill(json=payload)
        page.route("**/public/v1/config", config)
        return page

    def open(self, page, enabled=True):
        page.goto(self.base_url, wait_until="networkidle")
        if enabled:
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
        if enabled:
            page.wait_for_function("() => [...document.styleSheets].some(sheet => (sheet.href || '').includes('/modules/snow-scenes.css'))")
        return page

    def enter_stage(self, page, settle=True):
        page.locator("#go-in-person-label", has_text="观景区").wait_for(state="attached")
        page.wait_for_function("() => !document.querySelector('#go-in-person').disabled")
        page.locator("#go-in-person").click()
        page.locator("#confirm-presence-transition").click()
        if not settle:
            return
        page.locator('#chat-app[data-channel="in_person"]').wait_for(state="visible")
        page.locator("#presence-arrival-loading").wait_for(state="hidden")
        self.settle(page)

    def settle(self, page):
        # 首次入室的镜头推进、转场和到场台词都结束，舞台安静下来后再继续。
        page.wait_for_timeout(400)
        page.wait_for_function("""() => !document.getElementById('chat-app').dataset.snowPushing
          && ['idle', undefined].includes(document.getElementById('snow-scene-veil')?.dataset.phase)
          && !document.querySelector('#stage-speech.is-typing, #stage-speech.is-revealing')""", timeout=20000)
        page.wait_for_timeout(500)

    def go_to_beach(self, page):
        page.evaluate(RECORDER)
        page.locator("#message-input").fill("合成一起去海滩")
        page.locator("#send-message").click()
        page.wait_for_function("() => document.querySelector('#scene-stage .snow-layer-far')?.dataset.scene === 'beach'"
                               " && !document.getElementById('joint-movement-notice').hidden", timeout=20000)
        page.wait_for_function("() => ['idle', undefined].includes(document.getElementById('snow-scene-veil')?.dataset.phase)")
        page.wait_for_timeout(200)
        return page.evaluate("() => { window.__sceneLog.stop(); return window.__sceneLog.log; }")

    def test_frontend_scene_keys_match_the_backend(self):
        page = self.open(self.page())
        names = list(CATALOG) + sorted({where for c in MVP_CHARACTERS for where, _ in scene_templates_for(c.character_id)})
        frontend = page.evaluate("""async names => { const { sceneVisualKey } = await import('/dialogue-context.js');
          return names.map(name => sceneVisualKey(name)); }""", names)
        for name, key in zip(names, frontend):
            with self.subTest(name=name):
                self.assertEqual(key, scene_visual_key(name))
        self.assertEqual(self.errors, [])

    def test_every_place_draws_its_own_scene_at_every_time(self):
        page = self.open(self.page())
        result = page.evaluate("""async ([keys, legacy]) => {
          const { mountSnowWorld } = await import('/modules/snow-world.js');
          const { installSnowScenes } = await import('/modules/snow-scenes.js');
          installSnowScenes();
          const host = document.createElement('div');
          host.style.cssText = 'position:fixed;left:-2000px;top:0;width:800px;height:450px';
          document.body.append(host);
          const world = mountSnowWorld(host, { visualKey: 'generic', motionLevel: 'off' });
          const out = {};
          for (const time of ['dawn', 'day', 'dusk', 'night']) {
            document.documentElement.dataset.snowTime = time;
            for (const key of [...keys, ...legacy]) {
              world.setScene(key); world.refresh();
              out[`${key}/${time}`] = { scene: world.far.dataset.scene, room: world.room.innerHTML, far: world.far.innerHTML };
            }
          }
          world.destroy(); host.remove(); document.documentElement.dataset.snowTime = 'day';
          return out;
        }""", [sorted(PLACES), sorted(LEGACY)])
        for time in ("dawn", "day", "dusk", "night"):
            rooms = {key: result[f"{key}/{time}"]["room"] for key in PLACES | LEGACY}
            for key in sorted(PLACES):
                with self.subTest(key=key, time=time):
                    self.assertEqual(result[f"{key}/{time}"]["scene"], key, "新场景不应再回退成 generic")
                    self.assertGreater(len(rooms[key]), 2000)
                    self.assertEqual([other for other in rooms if other != key and rooms[other] == rooms[key]], [])
        for key in sorted(PLACES):
            with self.subTest(key=key):
                self.assertNotEqual(result[f"{key}/day"]["room"], result[f"{key}/night"]["room"], "背景要随时段换色")
        # 海滩的远景换成海面（雪岛上的世界树、海岬灯塔），其余场景仍是雪山远景。
        self.assertIn('id="b-sea"', result["beach/day"]["far"])
        self.assertNotIn('id="b-sea"', result["park/day"]["far"])
        self.assertEqual(self.errors, [])

    def test_joint_movement_finishes_the_line_then_plays_the_snow_transition(self):
        page = self.open(self.page(1440, 900))
        self.enter_stage(page)
        # 先完整播放一次普通换场，留下已触发的旧计时器，确认它们不会再被当成「正在播放」。
        page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'archive', character_location: '资料室',"
                      " character_activity: '合成活动', co_located: true })")
        page.wait_for_function("() => document.getElementById('snow-scene-veil').dataset.phase === 'down'")
        self.settle(page)
        log = self.go_to_beach(page)
        phases = [row for row in log if row["what"] == "veil"]
        order = [row["phase"] for row in phases]
        self.assertIn("up", order, log)
        up = phases[order.index("up")]
        self.assertEqual(order[order.index("up"):][:3], ["up", "down", "idle"], "转场应完整播放（盖住 -> 揭开 -> 结束），不能被回复打断")
        down = phases[order.index("up") + 1]
        self.assertGreaterEqual(down["t"] - up["t"], 1000)
        self.assertEqual(up["speech"], len(LINE), "等这句台词完整显示后才开始换场")
        before = [row for row in log if row["t"] < up["t"]]
        self.assertTrue(any(0 < row["speech"] < len(LINE) for row in before), "逐字显示期间应能看到台词在走")
        for row in before:
            self.assertEqual((row["far"], row["pill"], row["notice"]), ("archive", "资料室", False), row)
        switched = next(row for row in log if row["far"] == "beach")
        self.assertEqual(switched["phase"], "up", "背景和地点胶囊应在白雪盖住时才切换")
        self.assertEqual(switched["pill"], "基地海滩")
        first_notice = next(row for row in log if row["notice"])
        self.assertGreaterEqual(first_notice["t"], switched["t"])
        veil = page.evaluate("""() => { const v = document.getElementById('snow-scene-veil');
          return { scene: v.dataset.scene, kicker: v.querySelector('.snow-scene-veil-kicker').textContent,
            title: v.querySelector('.snow-scene-veil-title').textContent, emblem: Boolean(v.querySelector('.snow-scene-veil-emblem .snow-icon-waves')),
            fx: Boolean(v.querySelector(':scope > .snow-veil-fx')) }; }""")
        self.assertEqual(veil, {"scene": "beach", "kicker": "EXT", "title": "基地海滩", "emblem": True, "fx": True})
        self.assertEqual(page.locator("#stage-location").inner_text(), "基地海滩")
        self.assertEqual(page.locator("#in-person-surface").get_attribute("data-scene"), "beach")
        self.assertIn("已一起到达基地海滩", page.locator("#joint-movement-notice").inner_text())
        self.assertEqual(self.errors, [])

    def test_joint_movement_during_the_entry_camera_never_shows_the_beach_early(self):
        page = self.open(self.page(1440, 900))
        self.enter_stage(page, settle=False)
        page.locator("#chat-app[data-snow-pushing]").wait_for(state="attached")
        log = self.go_to_beach(page)
        phases = [row for row in log if row["what"] == "veil"]
        ups = [row for row in phases if row["phase"] == "up" and row["speech"] == len(LINE)]
        self.assertTrue(ups, log)
        for row in [row for row in log if row["t"] < ups[0]["t"]]:
            self.assertNotEqual(row["far"], "beach", row)
            self.assertNotEqual(row["pill"], "基地海滩", row)
        self.assertEqual(page.locator("#in-person-surface").get_attribute("data-scene"), "beach")
        self.assertEqual(self.errors, [])

    def test_reduced_motion_switches_the_scene_without_a_transition(self):
        page = self.open(self.page(1280, 820, motion="off"))
        self.enter_stage(page)
        log = self.go_to_beach(page)
        self.assertNotIn("up", [row["phase"] for row in log if row["what"] == "veil"])
        self.assertEqual(page.locator("#in-person-surface").get_attribute("data-scene"), "beach")
        self.assertEqual(page.locator("#stage-location").inner_text(), "基地海滩")
        self.assertEqual(self.errors, [])

    def test_area_codes_and_transition_title_stay_readable_in_both_tones(self):
        for tone in ("light", "dark"):
            page = self.open(self.page(1280, 820, tone=tone))
            self.enter_stage(page)
            codes = {}
            for key, place in (("analyst", "分析员的房间"), ("beach", "基地海滩"), ("hot_spring", "基地温泉")):
                page.evaluate("([key, place]) => window.__projectSnowTest.snowRenderSceneWith({ visual_key: key, character_location: place,"
                              " character_activity: '合成活动', co_located: true })", [key, place])
                page.wait_for_function("key => document.querySelector('#in-person-surface').dataset.scene === key", arg=key)
                codes[key] = page.evaluate("""() => getComputedStyle(document.querySelector('#in-person-surface .scene-location > span'), '::before')
                  .content.replace(/[\\s"]|\\/.*$/g, '')""")
                page.wait_for_function("() => document.getElementById('snow-scene-veil').dataset.phase !== 'up'")
            self.assertEqual(codes, {"analyst": "B-03", "beach": "EXT", "hot_spring": "EXT"}, tone)
            page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'park', character_location: '公园', co_located: true })")
            page.wait_for_function("() => document.getElementById('snow-scene-veil').dataset.phase === 'up'")
            page.wait_for_timeout(700)
            ratio = page.evaluate("""() => {
              const v = document.getElementById('snow-scene-veil'), style = getComputedStyle(v);
              const rgb = s => s.match(/[\\d.]+/g).slice(0, 3).map(Number);
              const lum = c => { const x = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
                return 0.2126 * x[0] + 0.7152 * x[1] + 0.0722 * x[2]; };
              const ratio = (a, b) => { const [h, l] = [lum(a), lum(b)].sort((x, y) => y - x); return (h + 0.05) / (l + 0.05); };
              const title = rgb(getComputedStyle(v.querySelector('.snow-scene-veil-title')).color);
              const sub = rgb(getComputedStyle(v.querySelector('.snow-scene-veil-sub')).color);
              const grounds = ['--veil-c', '--veil-c2'].map(name => style.getPropertyValue(name).trim().split(/\\s+/).map(Number));
              return Math.min(...grounds.flatMap(g => [ratio(title, g), ratio(sub, g)]));
            }""")
            self.assertGreaterEqual(ratio, 4.5, tone)
            self.assertEqual(self.errors, [], tone)
            page.close()

    def test_flag_off_loads_no_scene_module_and_legacy_backdrop_falls_back(self):
        page = self.open(self.page(enabled=False), enabled=False)
        page.evaluate("() => window.__projectSnowTest.snowRenderSceneWith({ visual_key: 'beach', character_location: '基地海滩' })")
        page.wait_for_timeout(300)
        self.assertTrue(page.locator("#scene-backdrop").get_attribute("src").startswith("/assets/immersive/scenes/generic"))
        self.assertEqual(page.locator("#in-person-surface").get_attribute("data-scene"), "generic")
        self.assertEqual([path for path in self.requested if "snow-scene" in path or "/scenes/beach" in path], [])
        self.assertEqual(self.errors, [])
