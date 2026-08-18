from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless
from urllib.parse import urlparse

RUN_PUBLIC_E2E = os.getenv("RUN_PUBLIC_E2E") == "1"
if RUN_PUBLIC_E2E:
    from playwright.sync_api import sync_playwright
else:
    sync_playwright = None


APP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = APP_ROOT / "public_frontend"
SHARED_ROOT = APP_ROOT / "frontend" / "shared"
IMMERSIVE_ROOT = APP_ROOT / "frontend" / "assets" / "immersive"


class PublicFrontendHandler(BaseHTTPRequestHandler):
    chat_stream_started: threading.Event | None = None
    chat_stream_release: threading.Event | None = None
    arrival_started: threading.Event | None = None
    arrival_release: threading.Event | None = None

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/public/v1/config":
            self._json(
                {
                    "app_version": "e2e",
                    "data_version": "fixture",
                    "turnstile_site_key": "",
                    "experience_notice_version": "0.8",
                    "analyst_avatar": {
                        "asset_id": "analyst-default",
                        "thumbnail_src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                        "src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                        "portrait_focus_x": 50,
                        "portrait_focus_y": 50,
                        "portrait_scale": 1,
                        "source_page": "https://wiki.biligame.com/sonw/%E6%96%87%E4%BB%B6:%E5%88%86%E6%9E%90%E5%91%98%E5%A4%B4%E5%83%8F.png",
                        "license": "fixture",
                        "license_version": "fixture",
                    },
                    "arrival_reaction_probability": 0.5,
                    "automatic_summary": {"default_enabled": True},
                    "providers": [{"provider_id": "openai", "display_name": "OpenAI"}],
                    "source_links": {
                        "project_snow": "https://github.com/X1aoB/Project_Snow",
                        "mywebsite": "https://github.com/X1aoB/MyWebsite",
                        "releases": "https://github.com/X1aoB/Project_Snow/releases",
                    },
                }
            )
            return
        if path == "/public/v1/characters":
            self._json(
                {
                    "count": 2,
                    "characters": [
                        {
                            "character_id": "25b23cb64398",
                            "display_name": "凯茜娅",
                            "aliases": ["凯茜娅", "凯西娅"],
                            "search_tokens": ["kxy", "kaixiya"],
                            "avatar": None,
                            "license": "fixture",
                        },
                        {
                            "character_id": "9f5804761c56",
                            "display_name": "安卡希雅",
                            "aliases": ["安卡希雅"],
                            "search_tokens": ["akxy", "ankaxiya"],
                            "avatar": None,
                            "license": "fixture",
                        },
                    ],
                }
            )
            return
        assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css", "/privacy/": "privacy/index.html", "/privacy/index.html": "privacy/index.html", "/privacy/privacy.js": "privacy/privacy.js"}
        if path.startswith("/shared/"):
            candidate = SHARED_ROOT / path.removeprefix("/shared/")
            if candidate.is_file():
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8" if candidate.suffix == ".css" else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if path.startswith("/assets/immersive/"):
            candidate = IMMERSIVE_ROOT / path.removeprefix("/assets/immersive/")
            if candidate.is_file():
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        filename = assets.get(path)
        if path.startswith("/assets/immersive/scenes/") and path.endswith(".svg"):
            candidate = PUBLIC_ROOT / path.lstrip("/")
            if candidate.is_file():
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if not filename:
            self.send_error(404)
            return
        body = (PUBLIC_ROOT / filename).read_bytes()
        content_type = "text/html" if filename.endswith(".html") else "text/javascript" if filename.endswith(".js") else "text/css"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/public/v1/byok/session":
            if not payload.get("api_key"):
                self._json({"detail": {"code": "invalid_request"}}, status=422)
                return
            self._json(
                {
                    "credential": "encrypted-e2e-credential",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
                }
            )
            return
        if self.path == "/public/v1/byok/models":
            if payload.get("credential") != "encrypted-e2e-credential":
                self._json({"detail": {"code": "credential_invalid"}}, status=401)
                return
            self._json({"models": ["gpt-e2e"]})
            return
        if self.path == "/public/v1/presence/resolve":
            self._json(
                {
                    "request_id": payload.get("request_id"),
                    "character_id": payload.get("character_id"),
                    "state_package": payload.get("state_package") or "fixture-state.signature",
                    "schema_version": "public-state-2",
                    "scene_state": {
                        "analyst_location": None,
                        "character_location": "观景区",
                        "character_activity": "正在看雪",
                        "visual_key": "observation",
                        "co_located": False,
                        "state_scope": "session_simulation",
                    },
                }
            )
            return
        if self.path == "/public/v1/presence/transition":
            in_person = payload.get("target_channel") == "in_person"
            self._json(
                {
                    "request_id": payload.get("request_id"),
                    "character_id": payload.get("character_id"),
                    "communication_channel": payload.get("target_channel"),
                    "state_package": "fixture-state.signature",
                    "scene_state": {
                        "analyst_location": "观景区" if in_person else None,
                        "character_location": "观景区",
                        "character_activity": "正在看雪",
                        "visual_key": "observation",
                        "co_located": in_person,
                        "state_scope": "session_simulation",
                    },
                    "model_called": False,
                }
            )
            return
        if self.path == "/public/v1/presence/arrival":
            if self.arrival_started is not None:
                self.arrival_started.set()
            if self.arrival_release is not None:
                self.arrival_release.wait(timeout=5)
            self._json(
                {
                    "arrival_id": payload.get("arrival_id"),
                    "character_id": payload.get("character_id"),
                    "communication_channel": "in_person",
                    "state_package": "fixture-state.signature",
                    "scene_state": {
                        "analyst_location": "观景区",
                        "character_location": "观景区",
                        "character_activity": "正在看雪",
                        "visual_key": "observation",
                        "co_located": True,
                        "state_scope": "session_simulation",
                    },
                    "decision": "noticed",
                    "status": "completed",
                    "terminal_error": "",
                    "model_called": True,
                    "reaction": {
                        "message_id": "arrival-e2e-message",
                        "content_blocks": [
                            {"type": "action", "text": "凯茜娅转过身看向你。"},
                            {"type": "speech", "text": "你来了。"},
                        ],
                    },
                }
            )
            return
        if self.path == "/public/v1/chat/stream":
            channel = payload.get("communication_channel") or "text"
            block_type = "message" if channel == "text" else "speech"
            meta_packet = (
                f'event: meta\ndata: {json.dumps({"request_id": payload.get("request_id"), "character_id": payload.get("character_id"), "provider": "openai", "model": "gpt-e2e", "communication_channel": channel}, ensure_ascii=False)}\n\n'
            ).encode()
            if payload.get("message") == "多段测试":
                blocks = [
                    {"type": "message", "text": "第一段"},
                    {"type": "message", "text": "第二段"},
                    {
                        "type": "sticker",
                        "asset_id": "fixture-sticker",
                        "caption": "收到",
                        "src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                        "thumbnail_src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                        "animated": False,
                    },
                ] if channel == "text" else [
                    {"type": "action", "text": "凯茜娅抬起手。"},
                    {"type": "speech", "text": "第一句。"},
                    {"type": "speech", "text": "第二句。"},
                ]
                delta_packets = []
                for index, block in enumerate(blocks):
                    if block["type"] == "sticker":
                        delta_packets.append(
                            "event: delta\ndata: "
                            + json.dumps({"block_index": index, "block_type": "sticker", **block}, ensure_ascii=False)
                            + "\n\n"
                        )
                    else:
                        delta_packets.append(
                            "event: delta\ndata: "
                            + json.dumps({"block_index": index, "block_type": block["type"], "text": block["text"]}, ensure_ascii=False)
                            + "\n\n"
                        )
                remaining_packets = "".join(
                    (*delta_packets,
                     'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                     f'event: done\ndata: {json.dumps({"truncated": False, "degraded_services": [], "communication_channel": channel, "content_blocks": blocks}, ensure_ascii=False)}\n\n')
                ).encode()
            else:
                remaining_packets = "".join(
                    (
                        'event: delta\ndata: {"text":"晚上好，分析员。"}\n\n',
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "degraded_services": [], "communication_channel": channel, "content_blocks": [{"type": block_type, "text": "晚上好，分析员。"}]}, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(meta_packet) + len(remaining_packets)))
            self.end_headers()
            self.wfile.write(meta_packet)
            self.wfile.flush()
            if self.chat_stream_started is not None:
                self.chat_stream_started.set()
            if self.chat_stream_release is not None:
                self.chat_stream_release.wait(timeout=5)
            self.wfile.write(remaining_packets)
            self.wfile.flush()
            return
        if self.path == "/public/v1/feedback":
            self._json({"feedback_code": "SNOW-E2E", "suppressed": False})
            return
        self._json({"detail": {"code": "not_found"}}, status=404)


@skipUnless(RUN_PUBLIC_E2E, "browser E2E is enabled only in the UI/full CI tier")
class PublicFrontendE2ETests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), PublicFrontendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        PublicFrontendHandler.chat_stream_started = None
        PublicFrontendHandler.chat_stream_release = None
        PublicFrontendHandler.arrival_started = None
        PublicFrontendHandler.arrival_release = None

    def tearDown(self) -> None:
        for gate in (
            PublicFrontendHandler.chat_stream_release,
            PublicFrontendHandler.arrival_release,
        ):
            if gate is not None:
                gate.set()
        self.setUp()

    @staticmethod
    def _configure_model(page) -> None:
        page.locator("#open-settings").click()
        page.locator("#api-key").fill("sk-e2e-only-not-real")
        page.locator("#toggle-advanced-model").click()
        page.locator("#model-id").fill("gpt-e2e")
        page.locator("#save-model").click()
        page.locator("#settings-dialog").wait_for(state="hidden")

    def _assert_no_horizontal_overflow(self, page) -> None:
        metrics = page.evaluate(
            """() => ({
                documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
            })"""
        )
        self.assertLessEqual(metrics["documentOverflow"], 1)
        self.assertLessEqual(metrics["bodyOverflow"], 1)

    def _assert_visible_controls_do_not_overlap(self, page, selector: str) -> None:
        boxes = page.locator(selector).evaluate_all(
            """elements => elements
                .filter(element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" && rect.width && rect.height;
                })
                .map(element => {
                    const rect = element.getBoundingClientRect();
                    return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom};
                })"""
        )
        for index, first in enumerate(boxes):
            for second in boxes[index + 1 :]:
                overlaps = not (
                    first["right"] <= second["left"]
                    or second["right"] <= first["left"]
                    or first["bottom"] <= second["top"]
                    or second["bottom"] <= first["top"]
                )
                self.assertFalse(overlaps, f"visible controls overlap: {first} and {second}")

    def test_model_discovery_keeps_the_issued_credential(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#open-settings").click()
            self.assertEqual(page.locator("#provider-select option").count(), 1)
            page.locator("#api-key").fill("sk-e2e-only-not-real")
            page.locator("#discover-models").click()
            page.locator("#discovered-models").wait_for(state="visible")
            page.locator("#discovered-models").select_option("gpt-e2e")
            self.assertTrue(page.locator("#credential-status").is_visible())
            self.assertEqual(page.locator("#api-key").input_value(), "")
            page.locator("#save-model").click()
            page.locator("#settings-dialog").wait_for(state="hidden")
            page.locator("#character-list").get_by_text("凯茜娅").wait_for(
                state="visible"
            )
            self.assertIn("凯茜娅", page.locator("#character-list").inner_text())
            self.assertEqual(page.locator("#stage-location").inner_text(), "观景区")
            self.assertEqual(page.locator(".analyst-portrait img").count(), 0)
            self.assertIsNotNone(page.locator("#toggle-action").get_attribute("hidden"))
            self.assertIsNone(page.locator("#toggle-sticker").get_attribute("hidden"))
            browser.close()

    def test_text_and_in_person_surfaces_share_local_continuity(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#open-settings").click()
            page.locator("#api-key").fill("sk-e2e-only-not-real")
            page.locator("#toggle-advanced-model").click()
            page.locator("#model-id").fill("gpt-e2e")
            page.locator("#save-model").click()
            page.locator("#settings-dialog").wait_for(state="hidden")
            page.locator("#message-input").fill("晚上好")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            self.assertEqual(page.locator(".message-avatar .portrait").count(), 2)
            self.assertEqual(page.locator(".analyst-portrait img").count(), 1)
            self.assertEqual(page.locator(".analyst-portrait").count(), 1)
            self.assertEqual(
                page.locator(".content-message").first.evaluate(
                    "(element) => getComputedStyle(element).borderRadius"
                ),
                "16px",
            )
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            self.assertIsNone(page.locator("#toggle-action").get_attribute("hidden"))
            self.assertIsNotNone(page.locator("#toggle-sticker").get_attribute("hidden"))
            # Face-to-face dialogue intentionally renders at roughly 24 ms per
            # character. Wait for the animated text rather than sampling the
            # initial, intentionally empty typewriter frame.
            page.locator("#stage-speech").get_by_text("你来了。").wait_for(state="visible")
            self.assertEqual(page.locator("#stage-speech").inner_text(), "你来了。")
            self.assertEqual(page.locator("#stage-speech .typing-indicator").count(), 0)
            page.locator("#toggle-action").click()
            page.locator("#action-input").fill("向她挥了挥手")
            page.locator("#send-message").click()
            page.locator("#stage-speech").get_by_text("晚上好，分析员。").wait_for(state="visible")
            page.locator("#open-transcript").click()
            transcript = page.locator("#transcript-content").inner_text()
            self.assertIn("晚上好", transcript)
            self.assertIn("向她挥了挥手", transcript)
            browser.close()

    def test_waiting_indicators_arrival_dedup_and_reduced_motion(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(reduced_motion="reduce")
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)

            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("晚上好")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            text_indicator = page.locator("#timeline .typing-indicator")
            text_indicator.wait_for(state="visible")
            self.assertEqual(text_indicator.count(), 1)
            self.assertEqual(text_indicator.get_attribute("aria-label"), "角色正在输入")
            self.assertEqual(page.locator("#request-status").get_attribute("aria-busy"), "true")
            self.assertTrue(page.locator("#go-in-person").is_disabled())
            self.assertEqual(
                page.locator("#timeline .typing-dot").first.evaluate(
                    "element => getComputedStyle(element).animationName"
                ),
                "none",
            )
            PublicFrontendHandler.chat_stream_release.set()
            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)

            PublicFrontendHandler.arrival_started = threading.Event()
            PublicFrontendHandler.arrival_release = threading.Event()
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            self.assertTrue(PublicFrontendHandler.arrival_started.wait(timeout=5))
            page.locator("#presence-arrival-loading").wait_for(state="visible")
            stage_indicator = page.locator("#stage-speech .typing-indicator")
            stage_indicator.wait_for(state="visible")
            self.assertEqual(stage_indicator.count(), 1)
            self.assertNotIn("你来了。", page.locator("#stage-speech").inner_text())
            self.assertTrue(page.locator("#open-communicator").is_disabled())
            PublicFrontendHandler.arrival_release.set()
            page.locator("#presence-arrival-loading").wait_for(state="hidden")
            page.locator("#stage-speech").get_by_text("你来了。").wait_for(state="visible")
            self.assertEqual(page.locator("#stage-speech .typing-indicator").count(), 0)
            self.assertEqual(page.locator("#stage-speech").inner_text(), "你来了。")
            self.assertEqual(page.locator("#stage-narration").inner_text(), "凯茜娅转过身看向你。")

            page.locator("#open-transcript").click()
            transcript = page.locator("#transcript-content").inner_text()
            self.assertEqual(transcript.count("你来了。"), 1)
            page.locator("#close-transcript").click()

            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("再说一句")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            page.locator("#stage-speech .typing-indicator").wait_for(state="visible")
            self.assertEqual(page.locator("#stage-speech .typing-indicator").count(), 1)
            PublicFrontendHandler.chat_stream_release.set()
            page.locator("#stage-speech").get_by_text("晚上好，分析员。").wait_for(state="visible")
            self.assertEqual(page.locator("#stage-speech .typing-indicator").count(), 0)
            browser.close()

    def test_desktop_mobile_and_narrow_layouts_do_not_overflow(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width, height in ((1280, 800), (390, 844), (320, 720)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(self.base_url, wait_until="networkidle")
                page.locator("#accept-experience-notice").click()
                self._assert_no_horizontal_overflow(page)
                self._assert_visible_controls_do_not_overlap(
                    page,
                    ".chat-header button:not([hidden])",
                )
                if width <= 820:
                    page.locator("#open-contacts").click()
                    page.locator("#contact-panel").wait_for(state="visible")
                    self._assert_no_horizontal_overflow(page)
                    self._assert_visible_controls_do_not_overlap(
                        page,
                        ".contact-footer > :not([hidden])",
                    )
                page.goto(f"{self.base_url}/privacy/", wait_until="networkidle")
                self._assert_no_horizontal_overflow(page)
                page.close()
            browser.close()

    def test_desktop_contacts_toggle_and_exact_pinyin_search(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#character-list").get_by_text("凯茜娅").wait_for(state="visible")
            page.locator("#character-search").fill("kxy")
            self.assertEqual(page.locator("#character-list [data-character]").count(), 1)
            self.assertIn("凯茜娅", page.locator("#character-list").inner_text())
            self._configure_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#open-stage-contacts").click()
            self.assertEqual(page.locator("#open-stage-contacts").get_attribute("aria-expanded"), "false")
            self.assertTrue(page.locator("#chat-app").evaluate("element => element.classList.contains('sidebar-collapsed')"))
            page.locator("#open-stage-contacts").click()
            self.assertEqual(page.locator("#open-stage-contacts").get_attribute("aria-expanded"), "true")
            browser.close()

    def test_presentation_queue_shows_between_text_and_face_to_face_segments(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#message-input").fill("多段测试")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("第一段").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 1)
            page.locator("#timeline").get_by_text("第二段").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 1)
            page.locator("#timeline").get_by_text("收到").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("多段测试")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            page.locator("#stage-speech .typing-indicator").wait_for(state="visible")
            PublicFrontendHandler.chat_stream_release.set()
            page.locator("#stage-speech").get_by_text("第一句。").wait_for(state="visible")
            page.locator("#stage-speech .typing-indicator").wait_for(state="visible")
            page.locator("#stage-speech").get_by_text("第二句。").wait_for(state="visible")
            self.assertEqual(page.locator("#stage-narration").inner_text(), "凯茜娅抬起手。")
            browser.close()

    def test_mobile_contacts_scroll_and_text_sticker_selection(self) -> None:
        characters = [
            {
                "character_id": f"fixture-{index:02d}",
                "display_name": f"角色{index:02d}",
                "aliases": [],
                "avatar": None,
                "license": "fixture",
            }
            for index in range(22)
        ]
        sticker = {
            "asset_id": "fixture-sticker",
            "caption": "收到",
            "category": "reaction",
            "thumbnail_src": "/media/fixture-sticker-96.webp",
            "src": "/media/fixture-sticker.webp",
            "animated": False,
        }
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})

            def fulfill_characters(route) -> None:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {"count": len(characters), "characters": characters},
                        ensure_ascii=False,
                    ),
                )

            def fulfill_stickers(route) -> None:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"count": 1, "stickers": [sticker]}, ensure_ascii=False),
                )

            page.route("**/public/v1/characters", fulfill_characters)
            page.route("**/public/v1/stickers**", fulfill_stickers)
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#open-contacts").click()
            contacts = page.locator("#character-list")
            contacts.wait_for(state="visible")
            before = contacts.evaluate(
                "(element) => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight})"
            )
            contacts.hover()
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(100)
            after = contacts.evaluate("(element) => element.scrollTop")
            self.assertGreater(before["scrollHeight"], before["clientHeight"])
            self.assertGreater(after, 0)
            page.locator('[data-character="fixture-21"]').click()
            self.assertIsNotNone(page.locator("#toggle-action").get_attribute("hidden"))
            self.assertIsNone(page.locator("#toggle-sticker").get_attribute("hidden"))
            page.locator("#toggle-sticker").click()
            page.locator('[data-sticker-id="fixture-sticker"]').click()
            self.assertTrue(page.locator("#selected-sticker").is_visible())
            self.assertIn("发送时会单独作为一条消息", page.locator("#selected-sticker").inner_text())
            browser.close()
