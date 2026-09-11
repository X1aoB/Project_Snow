from __future__ import annotations

import json
from hashlib import sha256
import mimetypes
import os
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

RUN_PUBLIC_E2E = os.getenv("RUN_PUBLIC_E2E") == "1"
if RUN_PUBLIC_E2E:
    from playwright.sync_api import sync_playwright
else:
    sync_playwright = None


APP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = APP_ROOT / "public_frontend"
SHARED_ROOT = APP_ROOT / "frontend" / "shared"
IMMERSIVE_ROOT = APP_ROOT / "frontend" / "assets" / "immersive"
COMPLEX_GRAPHEME_SAMPLE = "👨‍👩‍👧‍👦👍🏽🇭🇰e\u0301✈️"
LONG_IN_PERSON_REPLY = "她望着雪说这一段很长，但会在句界停一下。" * 60 + COMPLEX_GRAPHEME_SAMPLE
BOUNDARY_IN_PERSON_BLOCKS = [
    {"type": "speech", "text": "第一句。"},
    {"type": "speech", "text": "第二句。"},
    {"type": "speech", "text": "Hello."},
    {"type": "speech", "text": "World."},
    {"type": "speech", "text": "第一段"},
    {"type": "action", "text": "她停下来听雪。"},
    {"type": "speech", "text": "第二段\n\n第三段"},
]
BOUNDARY_IN_PERSON_REPLY = "第一句。第二句。 Hello. World. 第一段\n第二段\n\n第三段"
END_CARET_VISIBLE = """() => {
  const input=document.querySelector('#message-input');
  if (!input.value.endsWith('\\nx') || input.selectionStart !== input.value.length
      || input.selectionEnd !== input.value.length) return false;
  const style=getComputedStyle(input);
  const context=document.createElement('canvas').getContext('2d');
  context.font=style.font;
  const metrics=context.measureText('x');
  const fontHeight=metrics.fontBoundingBoxAscent + metrics.fontBoundingBoxDescent;
  // Native scrolling exposes the caret, not bottom padding or the font's
  // half-leading below it. Arial and CJK fonts have different bounding boxes
  // at the same 24px line-height; compare the caret box, with pixel rounding.
  const halfLeading=Math.max(0,(parseFloat(style.lineHeight)-fontHeight)/2);
  const bottom=input.scrollHeight-parseFloat(style.paddingBottom)-halfLeading;
  const top=bottom-fontHeight;
  return Number.isFinite(top) && top >= input.scrollTop-2
    && bottom <= input.scrollTop+input.clientHeight+2;
}"""


def _announcement_test_feed() -> dict[str, object]:
    """Fixed clock/calendar inputs, independent of published release notices.

    Return fresh nested values because revision and shared-birthday scenarios
    intentionally mutate their feed. The links are synthetic and never opened.
    """
    birthday_rows = (
        ("伊切尔", 11, 29), ("克罗瑞娜", 3, 13), ("凯茜娅", 11, 8),
        ("卜卜", 1, 31), ("奈莉德", 12, 31), ("妮塔", 5, 5),
        ("安卡希雅", 12, 20), ("恩雅", 1, 19), ("晴", 8, 20),
        ("猫汐尔", 10, 10), ("琴诺", 2, 27), ("瑟瑞斯", 7, 19),
        ("米娅", 8, 15), ("肴", 12, 23), ("胧嫣", 2, 1),
        ("芙提雅", 4, 23), ("芬妮", 7, 30), ("苔丝", 9, 16),
        ("茉莉安", 9, 5), ("薇蒂雅", 6, 14), ("辰星", 11, 5),
        ("里芙", 3, 21),
    )
    return {
        "schema_version": 1,
        "timezone": "Asia/Shanghai",
        "updates": [{
            "id": "fixture-announcement",
            "published_at": "2026-09-05T13:00:00+08:00",
            "title": "公告与生日提醒上线",
            "summary": "固定的公告行为测试内容。",
            "items": ["查看公告与角色生日。", "已读公告不重复自动提示。"],
        }],
        "birthdays": [
            {
                "character_id": f"fixture-birthday-{index}",
                "display_name": name,
                "month": month,
                "day": day,
                "source_url": f"https://example.test/birthdays/{index}?oldid=1",
            }
            for index, (name, month, day) in enumerate(birthday_rows)
        ],
    }


def _browser_launch_kwargs() -> dict[str, str]:
    """Select only an explicitly reviewed browser channel for CI.

    Local runs keep Playwright's bundled Chromium. GitHub's hosted runner can
    use its preinstalled stable Chrome when the browser download CDN is
    unavailable, without silently accepting arbitrary executable channels.
    """

    channel = os.getenv("PROJECT_SNOW_PLAYWRIGHT_CHANNEL", "").strip()
    if not channel:
        return {}
    if channel != "chrome":
        raise ValueError("PROJECT_SNOW_PLAYWRIGHT_CHANNEL must be 'chrome' or empty")
    return {"channel": channel}


def _launch_browser(playwright):
    return playwright.chromium.launch(**_browser_launch_kwargs())


class BrowserLaunchContractTests(TestCase):
    def test_local_browser_launch_uses_bundled_chromium(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_browser_launch_kwargs(), {})

    def test_ci_browser_launch_allows_only_preinstalled_chrome(self) -> None:
        with patch.dict(
            os.environ,
            {"PROJECT_SNOW_PLAYWRIGHT_CHANNEL": "chrome"},
            clear=True,
        ):
            self.assertEqual(_browser_launch_kwargs(), {"channel": "chrome"})
        with patch.dict(
            os.environ,
            {"PROJECT_SNOW_PLAYWRIGHT_CHANNEL": "chromium"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                _browser_launch_kwargs()


class PublicFrontendHandler(BaseHTTPRequestHandler):
    content_security_policy = ""
    announcement_payload: dict[str, object] | None = None
    announcement_failures = 0
    announcement_requests = 0
    chat_stream_started: threading.Event | None = None
    chat_stream_release: threading.Event | None = None
    arrival_started: threading.Event | None = None
    arrival_release: threading.Event | None = None
    feedback_payload: dict[str, object] | None = None
    feedback_attempts = 0
    feedback_mode = "success"
    turnstile_site_key = ""
    chat_payloads: list[dict[str, object]] = []
    chat_attempts: dict[str, int] = {}
    presence_resolve_count = 0
    arrival_mode = "success"
    transition_mode = "success"
    request_paths: list[str] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def end_headers(self) -> None:
        if self.content_security_policy:
            self.send_header("Content-Security-Policy", self.content_security_policy)
        super().end_headers()

    def _json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _expression_manifest(character_id):
        manifest = json.loads((PUBLIC_ROOT / "assets/expressions/mia/manifest.json").read_text(encoding="utf-8"))
        manifest["character_id"] = character_id
        manifest["display_name"] = character_id
        manifest["stage_layout"] = {
            "scale": 1.02 if character_id == "25b23cb64398" else 1,
            "focus_x": 48 if character_id == "25b23cb64398" else 50,
        }
        return manifest

    @classmethod
    def _expression_digest(cls, character_id):
        return sha256(json.dumps(cls._expression_manifest(character_id), ensure_ascii=False).encode()).hexdigest()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/announcements.json":
            type(self).announcement_requests += 1
            if type(self).announcement_failures:
                type(self).announcement_failures -= 1
                self._json({"error": "temporarily unavailable"}, status=503)
            else:
                self._json(type(self).announcement_payload or {
                    "schema_version": 1, "timezone": "Asia/Shanghai", "updates": [], "birthdays": [],
                })
            return
        if path == "/public/v1/config":
            self._json(
                {
                    "app_version": "e2e",
                    "data_version": "fixture",
                    "turnstile_site_key": type(self).turnstile_site_key,
                    "experience_notice_version": "0.9.2",
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
                    "movement_catalog": [
                        {"location_id": "commercial_street", "display_name": "商业街", "activity_name": "一起逛街", "invitation_text": "现在一起去商业街吗？"},
                        {"location_id": "shopping_mall", "display_name": "购物中心"},
                        {"location_id": "park", "display_name": "公园"},
                        {"location_id": "base_restaurant", "display_name": "基地餐厅"},
                        {"location_id": "base_lounge", "display_name": "基地休息区"},
                        {"location_id": "observation", "display_name": "观景区"},
                        {"location_id": "training", "display_name": "训练区"},
                        {"location_id": "archive", "display_name": "资料室"},
                        {"location_id": "base_beach", "display_name": "基地海滩"},
                        {"location_id": "base_arcade", "display_name": "基地游戏厅"},
                        {"location_id": "base_hot_spring", "display_name": "基地温泉"},
                        {"location_id": "base_healing_center", "display_name": "基地疗愈中心"},
                        {"location_id": "base_bar", "display_name": "基地酒吧"},
                        {"location_id": "character_room", "display_name": "她的房间", "invitation_text": "要不要一起去你的房间？"},
                        {"location_id": "analyst_room", "display_name": "我的房间", "invitation_text": "要不要一起去我的房间？"},
                    ],
                    "providers": [
                        {"provider_id": "openai", "display_name": "OpenAI"},
                        {"provider_id": "anthropic", "display_name": "Anthropic"},
                    ],
                    "source_links": {
                        "project_snow": "https://github.com/X1aoB/Project_Snow",
                        "mywebsite": "https://github.com/X1aoB/MyWebsite",
                        "releases": "https://github.com/X1aoB/Project_Snow/releases",
                    },
                }
            )
            return
        if path == "/public/v1/build-info":
            self._json({"app_version": "e2e", "revision": "fixture", "api_schema": "public-v1", "state_schema": "public-state-2"})
            return
        if path == "/public/v1/characters":
            self._json(
                {
                    "count": 4,
                    "characters": [
                        {
                            "character_id": "25b23cb64398",
                            "display_name": "凯茜娅",
                            "aliases": ["凯茜娅", "凯西娅"],
                            "search_tokens": ["kxy", "kaixiya"],
                            "avatar": None,
                            "expression_manifest_url": "/media/fixture/expressions/25b23cb64398/manifest.json",
                            "expression_manifest_sha256": self._expression_digest("25b23cb64398"),
                            "expression_state_count": 18,
                            "license": "fixture",
                        },
                        {
                            "character_id": "9f5804761c56",
                            "display_name": "安卡希雅",
                            "aliases": ["安卡希雅"],
                            "search_tokens": ["akxy", "ankaxiya"],
                            "avatar": None,
                            "expression_manifest_url": "/media/fixture/expressions/9f5804761c56/manifest.json",
                            "expression_manifest_sha256": self._expression_digest("9f5804761c56"),
                            "expression_state_count": 18,
                            "license": "fixture",
                        },
                        {
                            "character_id": "702f4375675b",
                            "display_name": "米娅",
                            "aliases": ["米娅"],
                            "search_tokens": ["my", "miya"],
                            "avatar": None,
                            "expression_manifest_url": "/media/fixture/expressions/702f4375675b/manifest.json",
                            "expression_manifest_sha256": self._expression_digest("702f4375675b"),
                            "expression_state_count": 18,
                            "license": "fixture",
                        },
                        {
                            "character_id": "78aa7ab99154",
                            "display_name": "伊切尔",
                            "aliases": ["伊切尔"],
                            "search_tokens": ["yqe", "yiqieer"],
                            "avatar": None,
                            "expression_manifest_url": "/media/fixture/expressions/78aa7ab99154/manifest.json",
                            "expression_manifest_sha256": self._expression_digest("78aa7ab99154"),
                            "expression_state_count": 18,
                            "license": "fixture",
                        },
                    ],
                }
            )
            return
        if path.startswith("/media/fixture/expressions/") and path.endswith("/manifest.json"):
            character_id = path.split("/")[-2]
            manifest = self._expression_manifest(character_id)
            self._json(manifest)
            return
        assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css", "/privacy/": "privacy/index.html", "/privacy/index.html": "privacy/index.html", "/privacy/privacy.js": "privacy/privacy.js"}
        if (path.startswith("/modules/") and path.endswith(".js")) or (path.startswith("/statistics/") and path.endswith(".mjs")):
            candidate = (PUBLIC_ROOT / path.lstrip("/")).resolve()
            if candidate.is_relative_to(PUBLIC_ROOT.resolve()) and candidate.is_file():
                assets[path] = candidate.relative_to(PUBLIC_ROOT.resolve()).as_posix()
        if path.startswith("/shared/"):
            candidate = SHARED_ROOT / path.removeprefix("/shared/")
            if candidate.is_file():
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
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
        if path.startswith("/assets/expressions/"):
            candidate = PUBLIC_ROOT / path.lstrip("/")
            if candidate.is_file():
                body = candidate.read_bytes()
                content_type = (
                    "application/json"
                    if candidate.suffix == ".json"
                    else "image/webp"
                )
                self.send_response(200)
                self.send_header("Content-Type", content_type)
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
        content_type = "text/html" if filename.endswith(".html") else "text/javascript" if filename.endswith((".js", ".mjs")) else "text/css"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).request_paths.append(self.path)
        if self.path == "/public/v1/byok/session":
            if not payload.get("api_key"):
                self._json({"detail": {"code": "invalid_request"}}, status=422)
                return
            self._json(
                {
                    "credential": "encrypted-e2e-credential",
                    "expires_at": (datetime.now(UTC) + timedelta(hours=12)).isoformat(),
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
            type(self).presence_resolve_count += 1
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
            if self.transition_mode == "http_error":
                self._json({"detail": {"code": "state_invalid"}}, status=409)
                return
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
            if self.arrival_mode == "http_error":
                self._json(
                    {"detail": {"code": "generation_queue_full"}},
                    status=429,
                )
                return
            terminal_error = (
                "upstream_invalid_response"
                if self.arrival_mode == "terminal_error"
                else ""
            )
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
                    "terminal_error": terminal_error,
                    "model_called": True,
                    "reaction": None
                    if terminal_error
                    else {
                        "message_id": "arrival-e2e-message",
                        "expression_state": "neutral",
                        "stage_motion": "none",
                        "content_blocks": [
                            {"type": "action", "text": "凯茜娅转过身看向你。"},
                            {"type": "speech", "text": "你来了。"},
                        ],
                    },
                }
            )
            return
        if self.path == "/public/v1/chat/stream":
            type(self).chat_payloads.append(payload)
            channel = payload.get("communication_channel") or "text"
            block_type = "message" if channel == "text" else "speech"
            meta_packet = (
                f'event: meta\ndata: {json.dumps({"request_id": payload.get("request_id"), "character_id": payload.get("character_id"), "provider": "openai", "model": "gpt-e2e", "communication_channel": channel}, ensure_ascii=False)}\n\n'
            ).encode()
            request_id = str(payload.get("request_id") or "")
            if payload.get("message") == "取消重连测试":
                attempts = type(self).chat_attempts
                attempts[request_id] = attempts.get(request_id, 0) + 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Content-Length", str(len(meta_packet)))
                self.end_headers()
                self.wfile.write(meta_packet)
                self.wfile.flush()
                return
            if payload.get("message") == "断流恢复测试":
                attempts = type(self).chat_attempts
                attempts[request_id] = attempts.get(request_id, 0) + 1
                if attempts[request_id] == 1:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(meta_packet)))
                    self.end_headers()
                    self.wfile.write(meta_packet)
                    self.wfile.flush()
                    return
            if payload.get("movement_location_id"):
                location_id = str(payload.get("movement_location_id"))
                location_name = "商业街" if location_id == "commercial_street" else "约定地点"
                blocks = [{"type": "message", "text": f"我先去{location_name}等你。"}]
                state_packet = {
                    "state_package": "fixture-state.signature",
                    "scene_state": {
                        "analyst_location": None,
                        "character_location": location_name,
                        "character_activity": f"在{location_name}等分析员",
                        "visual_key": "observation",
                        "co_located": False,
                        "state_scope": "subject_daily",
                    },
                    "state_event": {"event_type": "rendezvous_waiting"},
                }
                done_packet = {
                    "truncated": False,
                    "communication_channel": "text",
                    "content_blocks": blocks,
                    "movement_status": {
                        "status": "character_waiting",
                        "location_id": location_id,
                        "location_name": location_name,
                    },
                }
                remaining_packets = "".join(
                    (
                        f'event: delta\ndata: {json.dumps({"block_index": 0, "block_type": "message", "text": blocks[0]["text"]}, ensure_ascii=False)}\n\n',
                        f'event: state\ndata: {json.dumps(state_packet, ensure_ascii=False)}\n\n',
                        f'event: done\ndata: {json.dumps(done_packet, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            elif payload.get("message") in {
                "演出靠近测试",
                "动作无演出测试",
                "第二次演出测试",
                "颤动切换测试",
            }:
                motion = {
                    "演出靠近测试": "lean_in",
                    "动作无演出测试": "none",
                    "第二次演出测试": "startle",
                    "颤动切换测试": "tremble",
                }[payload["message"]]
                expression_state = {
                    "演出靠近测试": "surprised",
                    "动作无演出测试": "neutral",
                    "第二次演出测试": "happy",
                    "颤动切换测试": "concerned",
                }[payload["message"]]
                blocks = (
                    [
                        {"type": "action", "text": "米娅轻轻抬起手。"},
                        {"type": "speech", "text": "动作文字不会自动触发演出。"},
                    ]
                    if payload.get("message") == "动作无演出测试"
                    else [{
                        "type": "speech",
                        "text": {
                            "lean_in": "我想离你近一点说。",
                            "startle": "吓了一跳，不过已经没事了。",
                            "tremble": "我会认真听你说完。",
                        }[motion],
                    }]
                )
                remaining_packets = "".join(
                    (
                        *(
                            "event: delta\ndata: "
                            + json.dumps(
                                {
                                    "block_index": index,
                                    "block_type": block["type"],
                                    "text": block["text"],
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                            for index, block in enumerate(blocks)
                        ),
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": blocks, "expression_state": expression_state, "stage_motion": motion}, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            elif payload.get("message") == "多段测试":
                blocks = [
                    {"type": "message", "text": "第一段"},
                    {"type": "message", "text": "第二段"},
                    {
                        "type": "sticker",
                        "asset_id": "fixture-sticker",
                        "caption": "收到",
                        "src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                        "display_src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=#display",
                        "thumbnail_src": "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=",
                        "animated": True,
                        "display_animated": True,
                    },
                ] if channel == "text" else [
                    {"type": "speech", "text": "第一句。"},
                    {"type": "action", "text": "凯茜娅抬起手。"},
                    {"type": "speech", "text": "第二句。"},
                    {"type": "action", "text": "凯茜娅轻轻一笑。"},
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
                     f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": blocks}, ensure_ascii=False)}\n\n')
                ).encode()
            elif payload.get("message") == "句界测试":
                sentence_text = "第一句自然回应。第二句换个角度继续。第三句收住话题。"
                blocks = [{"type": block_type, "text": sentence_text}]
                remaining_packets = "".join(
                    (
                        f'event: delta\ndata: {json.dumps({"block_index": 0, "block_type": block_type, "text": sentence_text}, ensure_ascii=False)}\n\n',
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": blocks}, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            elif payload.get("message") == "长台词字素测试":
                blocks = [{"type": block_type, "text": LONG_IN_PERSON_REPLY}]
                remaining_packets = "".join(
                    (
                        f'event: delta\ndata: {json.dumps({"block_index": 0, "block_type": block_type, "text": LONG_IN_PERSON_REPLY}, ensure_ascii=False)}\n\n',
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": blocks}, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            elif payload.get("message") == "边界保真测试":
                blocks = [dict(block) for block in BOUNDARY_IN_PERSON_BLOCKS]
                delta_packets = [
                    "event: delta\ndata: "
                    + json.dumps(
                        {
                            "block_index": index,
                            "block_type": block["type"],
                            "text": block["text"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                    for index, block in enumerate(blocks)
                ]
                remaining_packets = "".join(
                    (
                        *delta_packets,
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": blocks}, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            elif payload.get("message") == "显式动作标签回归":
                blocks = [
                    {
                        "type": "action",
                        "text": "米娅眼睛一亮，头顶的耳朵轻轻抖了抖，随即又有些不好意思地抿了抿嘴。",
                    },
                    {
                        "type": "speech",
                        "text": "诶？去、去我房间吗......好啊！\n\n我正好想给你看我新贴的照片墙呢！",
                    },
                ]
                delta_packets = [
                    "event: delta\ndata: "
                    + json.dumps(
                        {
                            "block_index": index,
                            "block_type": block["type"],
                            "text": block["text"],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                    for index, block in enumerate(blocks)
                ]
                remaining_packets = "".join(
                    (
                        *delta_packets,
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": blocks}, ensure_ascii=False)}\n\n',
                    )
                ).encode()
            else:
                remaining_packets = "".join(
                    (
                        'event: delta\ndata: {"text":"晚上好，分析员。"}\n\n',
                        'event: state\ndata: {"state_package":"fixture-state.signature"}\n\n',
                        f'event: done\ndata: {json.dumps({"truncated": False, "communication_channel": channel, "content_blocks": [{"type": block_type, "text": "晚上好，分析员。"}]}, ensure_ascii=False)}\n\n',
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
            type(self).feedback_attempts += 1
            if type(self).feedback_mode == "http_error":
                self._json({"detail": {"code": "request_failed"}}, status=503)
                return
            type(self).feedback_payload = payload
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
        PublicFrontendHandler.content_security_policy = ""
        PublicFrontendHandler.announcement_payload = None
        PublicFrontendHandler.announcement_failures = 0
        PublicFrontendHandler.announcement_requests = 0
        PublicFrontendHandler.chat_stream_started = None
        PublicFrontendHandler.chat_stream_release = None
        PublicFrontendHandler.arrival_started = None
        PublicFrontendHandler.arrival_release = None
        PublicFrontendHandler.feedback_payload = None
        PublicFrontendHandler.feedback_attempts = 0
        PublicFrontendHandler.feedback_mode = "success"
        PublicFrontendHandler.turnstile_site_key = ""
        PublicFrontendHandler.chat_payloads = []
        PublicFrontendHandler.chat_attempts = {}
        PublicFrontendHandler.presence_resolve_count = 0
        PublicFrontendHandler.arrival_mode = "success"
        PublicFrontendHandler.transition_mode = "success"
        PublicFrontendHandler.request_paths = []

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

    def _announcement_page(self, page, now="2026-09-05T05:30:00Z"):
        page.add_init_script(f"Date.now = () => Date.parse({json.dumps(now)});")
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        page.wait_for_function("document.querySelector('#connection-status').textContent === '服务已连接'")
        page.wait_for_function("document.querySelector('#announcements-date').textContent.includes('北京时间')")

    def _statistics_chat(self, page):
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
        self._configure_model(page)
        page.locator("#message-input").fill("统计隔离测试正文")

    def test_statistics_disabled_leaves_chat_and_storage_independent(self):
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                page = browser.new_page()
                posts = []
                page.on("request", lambda request: posts.append(request.url) if "/analytics/" in request.url else None)
                self._statistics_chat(page)
                page.locator("#send-message").click()
                page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible")
                self.assertEqual(page.evaluate("typeof window.snowStatisticsRequest"), "undefined")
                self.assertEqual(page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('snow.statistics.'))"), [])
                self.assertEqual(posts, [])
                self.assertEqual(len(PublicFrontendHandler.chat_payloads), 1)
            finally:
                browser.close()

    def test_statistics_failures_allow_chat_under_production_csp(self):
        caddy = (APP_ROOT / "infra/Caddyfile").read_text(encoding="utf-8")
        PublicFrontendHandler.content_security_policy = next(
            line.strip().split('"', 2)[1] for line in caddy.splitlines()
            if line.strip().startswith('Content-Security-Policy "'))
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                for failure in ("unavailable", "disconnected", "timeout"):
                    with self.subTest(failure=failure):
                        page = browser.new_page()
                        page.add_init_script("""localStorage.setItem('snow.statistics.v1.project_snow.consent', String(Date.now()+60000));
                            window.__statisticsCsp = [];
                            document.addEventListener('securitypolicyviolation', e => window.__statisticsCsp.push(e.blockedURI));""")
                        config = {"enabled": True, "app": "project_snow", "endpoint": self.base_url + "/analytics/v1/events", "paths": ["/"]}
                        page.route("**/statistics/config.mjs", lambda route: route.fulfill(
                            content_type="text/javascript", body="export default " + json.dumps(config)))
                        pending = []

                        def fail_collection(route):
                            if failure == "unavailable":
                                route.fulfill(status=503, content_type="application/json", body='{"detail":"unavailable"}')
                            elif failure == "disconnected":
                                route.abort("connectionfailed")
                            else:
                                # Deliberately unresolved while chatting; release handlers before closing the page.
                                pending.append(route)

                        page.route("**/analytics/v1/events", fail_collection)
                        self._statistics_chat(page)
                        with page.expect_request(lambda request: "/analytics/v1/events" in request.url and
                                                 "request_observed" in (request.post_data or ""), timeout=8000) as sent:
                            page.locator("#send-message").click()
                            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible")
                        payload = sent.value.post_data
                        self.assertNotIn("统计隔离测试正文", payload)
                        self.assertNotIn("sk-e2e-only-not-real", payload)
                        self.assertNotIn("request_complete", payload)
                        self.assertEqual(page.evaluate("window.__statisticsCsp"), [])
                        page.get_by_role("button", name="关闭匿名访问统计", exact=True).click()
                        self.assertEqual(page.evaluate("typeof window.snowStatisticsRequest"), "undefined")
                        self.assertEqual(page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('snow.statistics.'))"), [])
                        self.assertEqual(page.locator("#timeline .message.user").count(), 1)
                        for route in pending:
                            route.abort("aborted")
                        page.close()
            finally:
                browser.close()

    @staticmethod
    def _announcement_screenshot(page, name):
        if directory := os.environ.get("SNOW_ANNOUNCEMENT_SCREENSHOTS"):
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / f"{name}.png"), full_page=True)

    def test_announcements_auto_once_reopen_and_notify_revised_content(self):
        PublicFrontendHandler.announcement_payload = _announcement_test_feed()
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 900}, timezone_id="America/Los_Angeles")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self._announcement_page(page)
            page.locator("#announcements-dialog[open]").wait_for()
            self.assertIn("公告与生日提醒上线", page.locator("#announcement-updates").inner_text())
            self.assertIn("茉莉安", page.locator("#announcement-birthday-highlight").inner_text())
            self.assertIn("有 2 条新消息", page.locator("#open-announcements").get_attribute("aria-label"))
            self._announcement_screenshot(page, "desktop-updates")
            page.locator("#announcement-tab-updates").focus()
            page.keyboard.press("ArrowRight")
            self.assertEqual(page.locator("#announcement-tab-birthdays").get_attribute("aria-selected"), "true")
            self.assertIn("茉莉安", page.locator("#announcement-birthdays-today").inner_text())
            self.assertIn("苔丝", page.locator("#announcement-birthdays-upcoming .birthday-row").first.inner_text())
            page.locator("#birthday-calendar-label").click()
            self.assertEqual(page.locator("#announcement-birthday-calendar .birthday-row").count(), 22)
            self.assertEqual(page.locator("#announcement-birthday-calendar a[href*='oldid=']").count(), 22)
            self._announcement_screenshot(page, "desktop-birthdays")
            source = page.locator("#announcement-birthday-calendar a").first
            source.focus()
            with page.expect_response("**/announcements.json"):
                page.evaluate("Date.now = () => Date.parse('2026-09-05T05:36:00Z'); window.dispatchEvent(new Event('focus'));")
            page.wait_for_function("document.querySelector('#announcement-status-text').textContent === ''")
            self.assertTrue(source.evaluate("element => element === document.activeElement"))
            page.locator("#acknowledge-announcements").click()
            page.reload(wait_until="networkidle")
            page.wait_for_function("document.querySelector('#announcements-date').textContent.includes('北京时间')")
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            self.assertFalse(page.locator("#open-announcements .announcement-unread-dot").is_visible())
            page.locator("#open-announcements").click()
            page.keyboard.press("Escape")
            page.wait_for_function("document.activeElement.id === 'open-announcements'")
            PublicFrontendHandler.announcement_payload["updates"][0]["updated_at"] = "2026-09-05T13:30:00+08:00"
            PublicFrontendHandler.announcement_payload["updates"][0]["title"] = "公告内容已修订"
            page.reload(wait_until="networkidle")
            page.locator("#announcements-dialog[open]").wait_for()
            self.assertIn("公告内容已修订", page.locator("#announcement-updates").inner_text())
            self.assertEqual(errors, [])
            browser.close()

    def test_announcements_birthday_changes_at_beijing_midnight_and_next_year(self):
        PublicFrontendHandler.announcement_payload = _announcement_test_feed()
        PublicFrontendHandler.announcement_payload["updates"] = []
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(timezone_id="America/Los_Angeles")
            self._announcement_page(page, "2026-09-04T15:59:59Z")
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            page.evaluate("Date.now = () => Date.parse('2026-09-04T16:00:00Z'); window.dispatchEvent(new Event('focus'));")
            page.locator("#announcements-dialog[open]").wait_for()
            self.assertEqual(page.locator("#announcement-tab-birthdays").get_attribute("aria-selected"), "true")
            self.assertIn("茉莉安", page.locator("#announcement-birthdays-today").inner_text())
            page.locator("#acknowledge-announcements").click()
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            page.evaluate("Date.now = () => Date.parse('2027-09-04T16:00:00Z'); window.dispatchEvent(new Event('focus'));")
            page.locator("#announcements-dialog[open]").wait_for()
            self.assertIn("2027年9月5日", page.locator("#announcements-date").inner_text())
            self.assertIn("茉莉安", page.locator("#announcement-birthdays-today").inner_text())
            browser.close()

    def test_announcements_calendar_handles_year_boundary_leap_year_and_shared_birthdays(self):
        feed = _announcement_test_feed()
        feed["updates"] = []
        PublicFrontendHandler.announcement_payload = feed
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self._announcement_page(page, "2026-12-31T16:00:00Z")
            page.locator("#open-announcements").click()
            page.locator("#announcement-tab-birthdays").click()
            first = page.locator("#announcement-birthdays-upcoming .birthday-row").first
            self.assertIn("恩雅", first.inner_text())
            self.assertIn("还有 18 天", first.inner_text())
            page.keyboard.press("Escape")
            page.evaluate("Date.now = () => Date.parse('2028-02-27T16:00:00Z'); window.dispatchEvent(new Event('focus'));")
            page.locator("#open-announcements").click()
            page.locator("#announcement-tab-birthdays").click()
            self.assertIn("克罗瑞娜", first.inner_text())
            self.assertIn("还有 14 天", first.inner_text())
            page.keyboard.press("Escape")
            # Two birthday notices on one day retain separate annual receipts.
            for item in feed["birthdays"][:2]:
                item["month"], item["day"] = 2, 28
            page.evaluate("Date.now = () => Date.parse('2028-02-28T01:00:00Z'); window.dispatchEvent(new Event('focus'));")
            page.locator("#announcements-dialog[open]").wait_for()
            self.assertEqual(page.locator("#announcement-birthdays-today .birthday-row").count(), 2)
            page.locator("#acknowledge-announcements").click()
            receipts = page.evaluate("JSON.parse(localStorage.getItem('project-snow-public:announcements:read:v1'))")
            self.assertEqual(len([key for key in receipts if key.startswith("birthday:2028-02-28:")]), 2)
            browser.close()

    def test_announcements_loading_retry_storage_fallback_and_safe_text(self):
        feed = _announcement_test_feed()
        feed["updates"][0]["title"] = '<img src=x onerror="window.injected=true">公告'
        PublicFrontendHandler.announcement_payload = feed
        PublicFrontendHandler.announcement_failures = 2
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("const originalSet = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) { if (key.includes('announcements:read')) throw new Error('storage unavailable'); return originalSet.call(this, key, value); };")
            self._announcement_page(page)
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            self.assertEqual(page.locator("#connection-status").inner_text(), "服务已连接")
            page.locator("#open-announcements").click()
            page.locator("#retry-announcements").wait_for()
            self.assertIn("暂时无法加载", page.locator("#announcement-status-text").inner_text())
            page.locator("#retry-announcements").click()
            page.locator("#announcement-updates h3").wait_for()
            self.assertIn("<img", page.locator("#announcement-updates h3").inner_text())
            self.assertEqual(page.locator("#announcement-updates img").count(), 0)
            page.locator("#acknowledge-announcements").click()
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            self.assertFalse(page.locator("#open-announcements .announcement-unread-dot").is_visible())
            self.assertEqual(errors, [])
            browser.close()

    def test_announcements_wait_for_settings_and_do_not_publish_future_updates(self):
        feed = _announcement_test_feed()
        feed["birthdays"] = []
        feed["updates"][0]["published_at"] = "2026-09-05T14:00:00+08:00"
        PublicFrontendHandler.announcement_payload = feed
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self._announcement_page(page)
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            page.locator("#open-announcements").click()
            self.assertEqual(page.locator("#announcement-updates article").count(), 0)
            page.keyboard.press("Escape")
            page.locator("#open-settings").click()
            page.evaluate("Date.now = () => Date.parse('2026-09-05T06:00:00Z'); window.dispatchEvent(new Event('focus'));")
            self.assertFalse(page.locator("#announcements-dialog").is_visible())
            self.assertTrue(page.locator("#settings-dialog").is_visible())
            page.keyboard.press("Escape")
            page.locator("#announcements-dialog[open]").wait_for()
            self.assertEqual(page.locator("dialog[open]").count(), 1)
            self.assertEqual(page.locator("#announcement-updates article").count(), 1)
            page.locator("#acknowledge-announcements").click()
            page.locator("#open-info").click()
            feed["updates"][0]["id"] = "update-while-reading-character-info"
            feed["updates"][0]["published_at"] = "2026-09-05T14:10:00+08:00"
            page.evaluate("Date.now = () => Date.parse('2026-09-05T06:10:00Z'); window.dispatchEvent(new Event('focus'));")
            page.locator("#announcements-dialog[open]").wait_for()
            page.keyboard.press("Escape")
            page.locator("#announcements-dialog").wait_for(state="hidden")
            self.assertIn("open", page.locator("#info-panel").get_attribute("class"))
            browser.close()

    def test_announcements_mobile_button_and_dialog_fit_both_surfaces(self):
        PublicFrontendHandler.announcement_payload = _announcement_test_feed()
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            for width, height in ((390, 844), (320, 568)):
                with self.subTest(width=width):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    self._announcement_page(page)
                    page.locator("#announcements-dialog[open]").wait_for()
                    self._assert_no_horizontal_overflow(page)
                    box = page.locator("#announcements-dialog").bounding_box()
                    self.assertGreaterEqual(box["x"], 0)
                    self.assertLessEqual(box["x"] + box["width"], width)
                    self._announcement_screenshot(page, f"mobile-{width}-updates")
                    page.locator("#announcement-tab-birthdays").click()
                    self._announcement_screenshot(page, f"mobile-{width}-birthdays")
                    page.locator("#acknowledge-announcements").click()
                    self._assert_visible_controls_do_not_overlap(page, ".chat-header button")
                    name_metrics = page.locator("#active-character h1").evaluate("element => ({visible: element.clientWidth, text: element.scrollWidth, font: getComputedStyle(element).font})")
                    self.assertLessEqual(name_metrics["text"], name_metrics["visible"] + 1, name_metrics)
                    entry = page.locator("#open-announcements").bounding_box()
                    self.assertLessEqual(entry["x"] + entry["width"], width)
                    self.assertLess(entry["y"], 100)
                    self._announcement_screenshot(page, f"mobile-{width}-text")
                    page.locator("#open-contacts").click()
                    self._configure_model(page)
                    page.locator("#close-contacts").click()
                    page.locator("#go-in-person").click()
                    page.locator("#confirm-presence-transition").click()
                    page.locator("#in-person-surface:not([hidden])").wait_for()
                    page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
                    self._assert_visible_controls_do_not_overlap(page, ".scene-hud button, .scene-menu > summary")
                    entry = page.locator("#stage-open-announcements").bounding_box()
                    self.assertLessEqual(entry["x"] + entry["width"], width)
                    page.locator("#stage-open-announcements").click()
                    page.locator("#announcements-dialog[open]").wait_for()
                    page.keyboard.press("Escape")
                    self._announcement_screenshot(page, f"mobile-{width}-stage")
                    page.close()
            browser.close()

    def _open_model_settings_with_interactive_verification(self, page, *, mobile=False) -> None:
        PublicFrontendHandler.turnstile_site_key = "e2e-site-key"
        page.route(
            "https://challenges.cloudflare.com/**",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=""),
        )
        page.add_init_script(
            """(() => {
                const widgets = new Map();
                window.__modelTurnstileOptions = [];
                window.__modelTurnstileOutcome = 'success';
                window.turnstile = {
                    render(container, options) {
                        if (window.__modelTurnstileOutcome === 'unavailable') throw new Error('load failed');
                        const widget = window.__modelTurnstileOptions.push(options);
                        const challenge = document.createElement('button');
                        challenge.type = 'button';
                        challenge.dataset.modelChallenge = String(widget);
                        challenge.textContent = '完成测试验证';
                        challenge.style.width = options.size === 'compact' ? '150px' : '300px';
                        challenge.style.height = options.size === 'compact' ? '140px' : '65px';
                        challenge.onclick = () => {
                            const outcome = window.__modelTurnstileOutcome;
                            if (outcome === 'success') options.callback('model-e2e-token');
                            else options[`${outcome}-callback`]?.();
                        };
                        widgets.set(widget, challenge);
                        container.append(challenge);
                        return widget;
                    },
                    execute() {},
                    remove(widget) { widgets.get(widget)?.remove(); widgets.delete(widget); },
                };
            })();"""
        )
        page.goto(self.base_url, wait_until="networkidle")
        page.locator("#accept-experience-notice").click()
        if mobile:
            page.locator("#open-contacts").click()
            page.locator("#open-settings").click()
        else:
            page.locator("#header-config-model").click()
        page.locator("#api-key").fill("sk-e2e-only-not-real")

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
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#open-settings").click()
            self.assertEqual(page.locator("#provider-select option").count(), 2)
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

    def test_model_setup_verification_is_actionable_inside_desktop_and_mobile_dialogs(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            for action, width, height in (("discover-models", 1280, 800), ("save-model", 390, 844)):
                with self.subTest(action=action):
                    page = browser.new_page(viewport={"width": width, "height": height})
                    errors = []
                    sessions = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda request: sessions.append(request.post_data_json)
                            if urlparse(request.url).path == "/public/v1/byok/session" else None)
                    self._open_model_settings_with_interactive_verification(page, mobile=width < 820)
                    if action == "save-model":
                        page.locator("#toggle-advanced-model").click()
                        page.locator("#model-id").fill("gpt-e2e")
                    page.locator(f"#{action}").click()
                    challenge = page.locator("[data-model-challenge]")
                    challenge.wait_for(state="attached", timeout=3000)
                    self.assertTrue(challenge.evaluate("element => Boolean(element.closest('#settings-dialog[open]'))"))
                    self.assertFalse(page.locator("#discover-models").is_enabled())
                    self.assertFalse(page.locator("#save-model").is_enabled())
                    self.assertEqual(sessions, [])
                    self.assertEqual(page.locator(".provider-form").get_attribute("aria-busy"), "true")
                    self._assert_no_horizontal_overflow(page)
                    self.assertLessEqual(page.locator("#settings-dialog").evaluate(
                        "element => element.scrollWidth - element.clientWidth"
                    ), 1)
                    # A real click checks top-layer hit testing, not just display/visibility.
                    challenge.click()
                    if action == "discover-models":
                        page.locator("#discovered-models").wait_for(state="visible")
                        page.locator("#discovered-models").select_option("gpt-e2e")
                        page.locator("#save-model").click()
                    page.locator("#settings-dialog").wait_for(state="hidden")
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["turnstile_token"], "model-e2e-token")
                    self.assertEqual(sessions[0]["provider"], "openai")
                    self.assertEqual(page.locator("#api-key").input_value(), "")
                    self.assertEqual(page.locator("[data-model-challenge]").count(), 0)
                    self.assertEqual(page.evaluate("() => window.__modelTurnstileOptions[0].action"), "byok-session")
                    self.assertEqual(errors, [])
                    self._assert_no_horizontal_overflow(page)
                    page.close()
            browser.close()

    def test_model_setup_verification_errors_preserve_inputs_and_allow_retry(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self._open_model_settings_with_interactive_verification(page)
            page.locator("#toggle-advanced-model").click()
            page.locator("#model-id").fill("gpt-e2e")
            for outcome, message in (("error", "人机验证未完成"), ("expired", "人机验证已过期"),
                                     ("timeout", "人机验证超时"), ("unavailable", "验证组件加载失败")):
                with self.subTest(outcome=outcome):
                    page.evaluate("value => { window.__modelTurnstileOutcome = value; }", outcome)
                    page.locator("#save-model").click()
                    if outcome != "unavailable":
                        page.locator("[data-model-challenge]").click()
                    page.locator("#setup-error").get_by_text(message).wait_for(state="visible")
                    self.assertTrue(page.locator("#save-model").is_enabled())
                    self.assertTrue(page.locator("#discover-models").is_enabled())
                    self.assertEqual(page.locator("#api-key").input_value(), "sk-e2e-only-not-real")
                    self.assertEqual(page.locator("#model-id").input_value(), "gpt-e2e")
                    self.assertEqual(page.locator("[data-model-challenge]").count(), 0)
                    self.assertNotIn("/public/v1/byok/session", PublicFrontendHandler.request_paths)
            page.evaluate("() => { window.__modelTurnstileOutcome = 'success'; }")
            page.locator("#save-model").click()
            page.locator("[data-model-challenge]").click()
            page.locator("#settings-dialog").wait_for(state="hidden")
            self.assertEqual(PublicFrontendHandler.request_paths.count("/public/v1/byok/session"), 1)
            browser.close()

    def test_model_setup_cancel_discards_late_verification_and_prevents_duplicate_submissions(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self._open_model_settings_with_interactive_verification(page)
            for dismissal in ("close", "escape", "tab"):
                with self.subTest(dismissal=dismissal):
                    page.locator("#discover-models").click()
                    page.locator("[data-model-challenge]").wait_for(state="visible")
                    # The guard must also reject queued/reentrant handlers, beyond disabled controls.
                    widget_count = page.evaluate("""() => {
                        document.querySelector('#discover-models').onclick();
                        document.querySelector('#save-model').onclick();
                        return window.__modelTurnstileOptions.length;
                    }""")
                    self.assertEqual(widget_count, ("close", "escape", "tab").index(dismissal) + 1)
                    if dismissal == "close":
                        page.locator('[data-close-dialog="settings-dialog"]').click()
                    elif dismissal == "escape":
                        page.keyboard.press("Escape")
                    else:
                        page.locator('[data-settings-tab="history"]').click()
                    page.wait_for_function("() => !document.querySelector('#discover-models').disabled")
                    page.evaluate("() => window.__modelTurnstileOptions.at(-1).callback('late-e2e-token')")
                    if dismissal == "tab":
                        page.locator('[data-settings-tab="models"]').click()
                    else:
                        page.locator("#header-config-model").click()
                    self.assertEqual(page.locator("[data-model-challenge]").count(), 0)
                    self.assertEqual(page.locator("#setup-error").inner_text(), "")
                    self.assertEqual(page.locator("#api-key").input_value(), "sk-e2e-only-not-real")
                    self.assertNotIn("/public/v1/byok/session", PublicFrontendHandler.request_paths)
            page.locator("#provider-select").select_option("anthropic")
            page.locator("#discover-models").click()
            page.locator("[data-model-challenge]").click()
            page.locator("#discovered-models").wait_for(state="visible")
            self.assertEqual(PublicFrontendHandler.request_paths.count("/public/v1/byok/session"), 1)
            self.assertEqual(errors, [])
            browser.close()

    def test_model_setup_missing_verification_callback_times_out_and_can_retry(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self._open_model_settings_with_interactive_verification(page)
            page.clock.install()
            page.locator("#discover-models").click()
            page.locator("[data-model-challenge]").wait_for(state="visible")
            page.clock.fast_forward(120001)
            page.locator("#setup-error").get_by_text("人机验证超时").wait_for(state="visible")
            self.assertTrue(page.locator("#discover-models").is_enabled())
            self.assertEqual(page.locator("[data-model-challenge]").count(), 0)
            self.assertNotIn("/public/v1/byok/session", PublicFrontendHandler.request_paths)
            page.locator("#discover-models").click()
            page.locator("[data-model-challenge]").click()
            page.locator("#discovered-models").wait_for(state="visible")
            self.assertEqual(PublicFrontendHandler.request_paths.count("/public/v1/byok/session"), 1)
            browser.close()

    def test_model_setup_can_cancel_while_verification_script_is_loading(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            self._open_model_settings_with_interactive_verification(page)
            page.evaluate("() => { window.__savedTurnstile = window.turnstile; delete window.turnstile; }")
            page.locator("#discover-models").click()
            page.locator("#model-verification-status").get_by_text("正在加载人机验证").wait_for(state="visible")
            page.locator('[data-close-dialog="settings-dialog"]').click()
            page.locator("#header-config-model").click()
            page.wait_for_function("() => !document.querySelector('#discover-models').disabled", timeout=1500)
            page.evaluate("() => { window.turnstile = window.__savedTurnstile; }")
            self.assertEqual(page.locator("[data-model-challenge]").count(), 0)
            self.assertNotIn("/public/v1/byok/session", PublicFrontendHandler.request_paths)
            page.locator("#discover-models").click()
            page.locator("[data-model-challenge]").click()
            page.locator("#discovered-models").wait_for(state="visible")
            self.assertEqual(PublicFrontendHandler.request_paths.count("/public/v1/byok/session"), 1)
            browser.close()

    def test_expression_metadata_is_explicit_and_mia_assets_cover_all_states(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            result = page.evaluate(
                """async () => {
                    const hooks = window.__projectSnowTest;
                    const character = {character_id: '702f4375675b', display_name: '米娅'};
                    const manifestUrl = hooks.expressionManifestUrlForCharacter(character);
                    const manifest = await (await fetch(manifestUrl)).json();
                    const v2Expressions = {};
                    const presentations = {};
                    for (const [state, item] of Object.entries(manifest.expressions)) {
                        const presentationId = `expression.${state}`;
                        v2Expressions[state] = {
                            ...item,
                            presentation_id: presentationId,
                            render_strategy: 'single_sprite',
                            asset_scope: 'complete_character_sprite',
                        };
                        presentations[presentationId] = {
                            render_strategy: 'single_sprite',
                            asset_scope: 'complete_character_sprite',
                            stage_asset_path: item.stage_asset_path,
                            stage_asset_sha256: item.stage_asset_sha256,
                            stage_layout: {scale: 1.08, focus_x: 47},
                        };
                    }
                    const normalizedV2 = hooks.normalizeExpressionManifest({
                        ...manifest,
                        schema_version: 'project-snow-character-expression-runtime-2',
                        expressions: v2Expressions,
                        presentations,
                    }, character, manifestUrl);
                    const futureFallbackExpressions = structuredClone(v2Expressions);
                    const futureFallbackPresentations = structuredClone(presentations);
                    futureFallbackExpressions.happy.render_strategy = 'layered_sprite';
                    futureFallbackPresentations['expression.happy'].render_strategy = 'layered_sprite';
                    const normalizedFutureFallback = hooks.normalizeExpressionManifest({
                        ...manifest,
                        expressions: futureFallbackExpressions,
                        presentations: futureFallbackPresentations,
                    }, character, manifestUrl);
                    const assetResponses = await Promise.all(
                        Object.entries(manifest.expressions).flatMap(([state, item]) => [
                            [state, 'face', item.face_asset_path],
                            [state, 'stage', item.stage_asset_path],
                        ]).map(async ([state, kind, url]) => {
                            const response = await fetch(url);
                            return [state, kind, response.status, response.headers.get('content-type') || ''];
                        }),
                    );
                    const explicit = hooks.expressionStateForMessage({
                        expressionState: 'surprised',
                        communicationChannel: 'in_person',
                        contentBlocks: [{type: 'action', text: '她生气地咬牙。'}],
                    });
                    const noSemanticFallback = hooks.expressionStateForMessage({
                        communicationChannel: 'in_person',
                        contentBlocks: [{type: 'action', text: '她生气地咬牙。'}],
                    });
                    const wrongCase = hooks.normalizeExpressionState('HAPPY', 'in_person');
                    const textForcedNeutral = hooks.normalizeExpressionState('happy', 'text');
                    return {
                        states: Object.keys(manifest.expressions), assetResponses, explicit,
                        noSemanticFallback, wrongCase, textForcedNeutral,
                        v2Happy: normalizedV2.expressions.happy.presentation,
                        futureFallback: normalizedFutureFallback.expressions.happy.presentation,
                    };
                }""",
            )
            self.assertEqual(len(result["states"]), 18)
            self.assertEqual(result["explicit"], "surprised")
            self.assertEqual(result["noSemanticFallback"], "neutral")
            self.assertEqual(result["wrongCase"], "neutral")
            self.assertEqual(result["textForcedNeutral"], "neutral")
            self.assertEqual(result["v2Happy"]["id"], "expression.happy")
            self.assertEqual(result["v2Happy"]["renderStrategy"], "single_sprite")
            self.assertEqual(result["v2Happy"]["layout"], {"scale": 1.08, "focusX": 47})
            self.assertFalse(result["v2Happy"]["usesLegacyFallback"])
            self.assertEqual(result["futureFallback"]["declaredRenderStrategy"], "layered_sprite")
            self.assertEqual(result["futureFallback"]["renderStrategy"], "single_sprite")
            self.assertTrue(result["futureFallback"]["usesLegacyFallback"])
            self.assertEqual(len(result["assetResponses"]), 36)
            for state, kind, status, content_type in result["assetResponses"]:
                self.assertIn(state, result["states"])
                self.assertIn(kind, {"face", "stage"})
                self.assertEqual(status, 200, f"{state}/{kind}")
                self.assertEqual(content_type, "image/webp", f"{state}/{kind}")
            browser.close()

    def test_production_csp_stage_displays_single_layered_and_verified_fallback(self) -> None:
        from io import BytesIO
        from PIL import Image

        # Use the shipping response policy verbatim, including connect-src.
        # This must exercise the application document, not a policy-free canvas page.
        caddy = (APP_ROOT / "infra/Caddyfile").read_text(encoding="utf-8")
        policy = next(line.strip().split('"', 2)[1] for line in caddy.splitlines()
                      if line.strip().startswith('Content-Security-Policy "'))
        self.assertIn("img-src 'self' data:;", policy)
        self.assertNotIn("blob:", policy)
        PublicFrontendHandler.content_security_policy = policy
        images = {}
        for name, size, color, file_format in (
            ("flat.webp", (6, 8), (30, 60, 90, 255), "WEBP"),
            ("body.png", (6, 8), (40, 60, 80, 255), "PNG"),
            ("head.png", (2, 2), (200, 160, 120, 255), "PNG"),
            ("substitute.png", (2, 2), (240, 10, 30, 255), "PNG"),
        ):
            buffer = BytesIO()
            Image.new("RGBA", size, color).save(buffer, format=file_format, lossless=True, compress_level=0)
            images[name] = buffer.getvalue()
        self.assertEqual(len(images["head.png"]), len(images["substitute.png"]))

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                for strategy in ("single", "layered", "fallback", "tampered"):
                    with self.subTest(strategy=strategy):
                        payload = PublicFrontendHandler._expression_manifest("25b23cb64398")
                        payload["expressions"]["neutral"]["stage_asset_path"] = "/csp-stage/flat.webp"
                        payload["expressions"]["neutral"]["stage_asset_sha256"] = sha256(images["flat.webp"]).hexdigest()
                        if strategy != "single":
                            payload["expressions"]["neutral"]["presentation_id"] = "expression.neutral"
                            payload["presentations"] = {"expression.neutral": {
                                "render_strategy": "layered_sprite", "canvas": {"width": 6, "height": 8},
                                "layers": [
                                    {"asset_path": "/csp-stage/body.png", "asset_sha256": sha256(images["body.png"]).hexdigest(),
                                     "dimensions": {"width": 6, "height": 8}, "position": {"x": 0, "y": 0}, "composite": "source-over"},
                                    {"asset_path": "/csp-stage/" + ("missing.png" if strategy == "fallback" else "head.png"),
                                     "asset_sha256": sha256(images["head.png"]).hexdigest(),
                                     "dimensions": {"width": 2, "height": 2}, "position": {"x": 2, "y": 1}, "composite": "source-over"},
                                ],
                            }}
                        manifest = json.dumps(payload).encode()
                        requested = []
                        forbidden = []
                        page = browser.new_page(viewport={"width": 1280, "height": 800})
                        page.add_init_script("""window.__cspViolations = []; window.__decodedImageHashes = [];
                            document.addEventListener('securitypolicyviolation', event =>
                                window.__cspViolations.push({directive:event.effectiveDirective, blocked:event.blockedURI}));
                            const decode = HTMLImageElement.prototype.decode;
                            HTMLImageElement.prototype.decode = async function() {
                                if (this.src.startsWith('data:image/')) {
                                    // No fetch(data:): production connect-src intentionally forbids it.
                                    const bytes = Uint8Array.from(atob(this.src.split(',')[1]), value => value.charCodeAt(0));
                                    window.__decodedImageHashes.push([...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
                                        .map(value => value.toString(16).padStart(2,'0')).join(''));
                                }
                                return decode.call(this);
                            };""")

                        def route_local(route):
                            path = urlparse(route.request.url).path
                            if not route.request.url.startswith(self.base_url + "/"):
                                forbidden.append("external")
                                route.abort()
                            elif route.request.method == "POST":
                                if path != "/public/v1/presence/resolve":
                                    forbidden.append(path)
                                    route.abort()
                                    return
                                result = route.fetch().json()
                                result["scene_state"].update(analyst_location="观景区", co_located=True)
                                route.fulfill(json=result)
                            elif path == "/public/v1/characters":
                                result = route.fetch().json()
                                for character in result["characters"]:
                                    if character["character_id"] == "25b23cb64398":
                                        character.update(expression_manifest_url="/csp-stage/manifest.json",
                                                         expression_manifest_sha256=sha256(manifest).hexdigest())
                                route.fulfill(json=result)
                            elif path.startswith("/csp-stage/"):
                                requested.append(path)
                                name = path.rsplit("/", 1)[-1]
                                if name == "manifest.json":
                                    route.fulfill(content_type="application/json", body=manifest)
                                elif name in images:
                                    actual = "substitute.png" if strategy == "tampered" and name == "head.png" else name
                                    route.fulfill(content_type="image/png" if name.endswith(".png") else "image/webp", body=images[actual])
                                else:
                                    route.fulfill(status=404, body="missing fixture")
                            else:
                                route.continue_()

                        page.route("**/*", route_local)
                        response = page.goto(self.base_url, wait_until="networkidle")
                        self.assertEqual(response.header_value("content-security-policy"), policy)
                        page.locator("#accept-experience-notice").click()
                        page.locator('[data-character="25b23cb64398"]').click()
                        page.wait_for_function("document.querySelector('#connection-status').textContent === '服务已连接'")
                        # Restore a real persisted in-person thread without BYOK or generation.
                        page.evaluate("""async () => {
                            const open = indexedDB.open('project-snow-public');
                            const db = await new Promise((resolve, reject) => {
                                open.onsuccess = () => resolve(open.result); open.onerror = () => reject(open.error);
                            });
                            const tx = db.transaction('threads', 'readwrite'), store = tx.objectStore('threads');
                            const read = store.get('25b23cb64398');
                            read.onsuccess = () => store.put({...read.result, characterId:'25b23cb64398', channel:'in_person'});
                            await new Promise((resolve, reject) => {tx.oncomplete=resolve; tx.onerror=() => reject(tx.error);});
                            db.close();
                        }""")
                        page.reload(wait_until="networkidle")
                        page.locator("#in-person-surface").wait_for(state="visible")
                        page.wait_for_function("""() => {
                            const art=document.querySelector('#stage-character-art');
                            return Boolean(art.dataset.stageArtFailedKey) || (!art.hidden && art.complete && art.naturalWidth > 0);
                        }""", timeout=10000)
                        art = page.locator("#stage-character-art")
                        diagnostics = art.evaluate("""node => ({hidden:node.hidden, width:node.naturalWidth,
                            height:node.naturalHeight, strategy:node.dataset.renderStrategy,
                            violations:window.__cspViolations})""")
                        self.assertTrue(art.is_visible(), diagnostics)
                        self.assertEqual([diagnostics["width"], diagnostics["height"]], [6, 8])
                        self.assertEqual(diagnostics["strategy"], "layered_sprite" if strategy == "layered" else "single_sprite")
                        pixel = art.evaluate("""node => {
                            const canvas=document.createElement('canvas');canvas.width=6;canvas.height=8;
                            const context=canvas.getContext('2d');context.drawImage(node,0,0);
                            return [...context.getImageData(2,1,1,1).data];
                        }""")
                        self.assertEqual(pixel, [200, 160, 120, 255] if strategy == "layered" else [30, 60, 90, 255])
                        self.assertEqual(diagnostics["violations"], [])
                        self.assertEqual(forbidden, [])
                        self.assertIn("/csp-stage/manifest.json", requested)
                        if strategy == "tampered":
                            decoded = page.evaluate("window.__decodedImageHashes")
                            self.assertIn("/csp-stage/head.png", requested)
                            self.assertNotIn(sha256(images["substitute.png"]).hexdigest(), decoded)
                            self.assertIn(sha256(images["flat.webp"]).hexdigest(), decoded)
                        if strategy in ("fallback", "tampered"):
                            self.assertIn("/csp-stage/flat.webp", requested)
                        if strategy == "fallback":
                            self.assertIn("/csp-stage/missing.png", requested)
                        page.close()
            finally:
                browser.close()

    def test_layered_stage_uses_native_pixels_complete_fallback_and_latest_request(self) -> None:
        from io import BytesIO
        from PIL import Image

        images = {}
        for name, size, color in (
            ("underlay", (6, 8), (40, 60, 80, 255)),
            ("head", (2, 2), (200, 160, 120, 255)),
            ("slow", (2, 2), (230, 50, 40, 255)),
            ("happy", (2, 2), (30, 130, 220, 255)),
        ):
            image = Image.new("RGBA", size, color)
            if name == "underlay":
                image.putpixel((1, 5), (0, 0, 0, 0))
                image.putpixel((1, 4), (90, 40, 210, 81))
            elif name == "head":
                image.putpixel((1, 1), (140, 200, 180, 71))
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            images[f"{name}.png"] = buffer.getvalue()
        payload = json.loads((PUBLIC_ROOT / "assets/expressions/mia/manifest.json").read_text(encoding="utf-8"))
        payload["presentations"] = {}
        for state, head in (("neutral", "head"), ("thinking", "slow"), ("happy", "happy")):
            presentation_id = f"expression.{state}"
            payload["expressions"][state]["presentation_id"] = presentation_id
            payload["presentations"][presentation_id] = {
                "render_strategy": "layered_sprite",
                "canvas": {"width": 6, "height": 8},
                "layers": [
                    {"asset_path": "/native-layers/underlay.png", "asset_sha256": sha256(images["underlay.png"]).hexdigest(), "dimensions": {"width": 6, "height": 8},
                     "position": {"x": 0, "y": 0}, "composite": "source-over"},
                    {"asset_path": f"/native-layers/{head}.png", "asset_sha256": sha256(images[f"{head}.png"]).hexdigest(), "dimensions": {"width": 2, "height": 2},
                     "position": {"x": 2, "y": 1}, "composite": "source-over"},
                ],
            }

        def route_layer(route):
            name = urlparse(route.request.url).path.rsplit("/", 1)[-1]
            if name == "manifest.json":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            elif name in images:
                route.fulfill(status=200, content_type="image/png", body=images[name])
            else:
                route.fulfill(status=503, body="unavailable")

        payload["performance_contract"] = {
            "schema_version": "project-snow-character-performance-1", "character_scoped": True,
            "base_expression_states_unchanged": True,
        }
        payload["performance_count"] = 3
        payload["performances"] = {}
        for cue in ("friendly_greeting", "quiet_resolve", "shared_memory"):
            payload["performances"][cue] = {**payload["expressions"]["happy"],
                "presentation_id": f"performance.{cue}", "fallback_expression": "happy"}
            payload["presentations"][f"performance.{cue}"] = dict(payload["presentations"]["expression.happy"])

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.route("**/native-layers/*", route_layer)
            page.add_init_script("window.__slowLayerHash = " + json.dumps(sha256(images["slow.png"]).hexdigest()))
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator('[data-character="702f4375675b"]').click()
            result = page.evaluate("""async (payload) => {
                const hooks = window.__projectSnowTest;
                const character = {character_id: '702f4375675b', expression_manifest_url: '/native-layers/manifest.json'};
                const manifest = hooks.normalizeExpressionManifest(payload, character, character.expression_manifest_url);
                const retainedCues = [0, 1, 2, 3].map((count) => {
                    const trimmed = {...payload, performance_count: count,
                        performances: Object.fromEntries(Object.entries(payload.performances).slice(0, count))};
                    return Object.keys(hooks.normalizeExpressionManifest(trimmed, character, character.expression_manifest_url).performances);
                });
                const presentation = manifest.expressions.neutral.presentation;
                const ready = await hooks.preloadStagePresentation(presentation);
                const image = new Image();
                ready.renderer.commit(image, presentation, ready.prepared);
                await image.decode();
                const canvas = document.createElement('canvas');
                canvas.width = image.naturalWidth;
                canvas.height = image.naturalHeight;
                const context = canvas.getContext('2d');
                context.drawImage(image, 0, 0);
                const pixel = (x, y) => [...context.getImageData(x, y, 1, 1).data];
                const reference = document.createElement('canvas');
                reference.width = 6; reference.height = 8;
                const referenceContext = reference.getContext('2d');
                const layerContentTypes = [];
                for (const layer of presentation.layers) {
                    const response = await fetch(layer.assetPath);
                    layerContentTypes.push(response.headers.get('content-type'));
                    const original = await createImageBitmap(await response.blob());
                    referenceContext.drawImage(original, layer.x, layer.y);
                    original.close();
                }
                const referencePixels = referenceContext.getImageData(0, 0, 6, 8).data;
                const renderedPixels = context.getImageData(0, 0, 6, 8).data;
                const changedChannels = renderedPixels.reduce((count, value, index) =>
                    count + Number(value !== referencePixels[index]), 0);
                const missing = structuredClone(presentation);
                missing.layers[1].assetPath = '/native-layers/missing.png';
                const fallback = await hooks.preloadStagePresentation(missing);
                const incorrectSize = structuredClone(presentation);
                incorrectSize.layers[1].width = 3;
                const sizeFallback = await hooks.preloadStagePresentation(incorrectSize);
                const controller = new AbortController();
                controller.abort();
                let abortName = '';
                try { await hooks.preloadStagePresentation(presentation, controller.signal); }
                catch (error) { abortName = error.name; }
                window.__nativeTestCharacter = character;
                return {size: [image.naturalWidth, image.naturalHeight], base: pixel(0, 0),
                    head: pixel(2, 1), edge: pixel(4, 1), removedArm: pixel(1, 5),
                    strategy: ready.prepared.renderStrategy, fallback: fallback.prepared,
                    sizeFallback: sizeFallback.prepared.renderStrategy, abortName, retainedCues, changedChannels, layerContentTypes};
            }""", payload)
            self.assertEqual(result["size"], [6, 8])
            self.assertEqual(result["layerContentTypes"], ["image/png", "image/png"])
            self.assertEqual(result["changedChannels"], 0)
            self.assertEqual(result["base"], [40, 60, 80, 255])
            self.assertEqual(result["head"], [200, 160, 120, 255])
            self.assertEqual(result["edge"], [40, 60, 80, 255])
            self.assertEqual(result["removedArm"], [0, 0, 0, 0])
            self.assertEqual(result["strategy"], "layered_sprite")
            self.assertEqual(result["fallback"]["renderStrategy"], "single_sprite")
            self.assertTrue(result["fallback"]["src"].startswith("data:image/"))
            self.assertEqual(result["sizeFallback"], "single_sprite")
            self.assertEqual(result["abortName"], "AbortError")
            self.assertEqual(result["retainedCues"], [[], ["friendly_greeting"],
                ["friendly_greeting", "quiet_resolve"],
                ["friendly_greeting", "quiet_resolve", "shared_memory"]])

            # Simulate the release after two cues were removed by review.
            payload["performance_count"] = 1
            payload["performances"] = {"friendly_greeting": payload["performances"]["friendly_greeting"]}
            for cue in ("quiet_resolve", "shared_memory"):
                del payload["presentations"][f"performance.{cue}"]

            page.evaluate("hash => { window.__nativeTestCharacter.expression_manifest_sha256 = hash; }", sha256(json.dumps(payload).encode()).hexdigest())
            page.evaluate("""async () => {
                const hooks = window.__projectSnowTest;
                const art = document.querySelector('#stage-character-art');
                await hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'neutral');
                window.__neutralSurface = art.src;
                const decode = HTMLImageElement.prototype.decode;
                const gate = new Promise((resolve) => { window.__releaseSlowLayer = resolve; });
                HTMLImageElement.prototype.decode = async function() {
                    if (this.src.startsWith('data:image/') && [...new Uint8Array(await crypto.subtle.digest('SHA-256', await (await fetch(this.src)).arrayBuffer()))].map(value => value.toString(16).padStart(2,'0')).join('') === window.__slowLayerHash) {
                        window.__slowLayerWaiting = true;
                        await gate;
                    }
                    return decode.call(this);
                };
                window.__restoreImageDecode = () => { HTMLImageElement.prototype.decode = decode; };
                window.__slowStageUpdate = hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'thinking');
            }""")
            page.wait_for_function("() => window.__slowLayerWaiting === true")
            self.assertTrue(page.evaluate("() => document.querySelector('#stage-character-art').src === window.__neutralSurface"))
            raced = page.evaluate("""async () => {
                const art = document.querySelector('#stage-character-art');
                const latest = await window.__projectSnowTest.updateStageCharacterArt(art, window.__nativeTestCharacter, 'happy');
                const happySurface = art.src;
                window.__releaseSlowLayer();
                const stale = await window.__slowStageUpdate;
                window.__restoreImageDecode();
                return {latest, stale, state: art.dataset.expressionState,
                    strategy: art.dataset.renderStrategy, same: art.src === happySurface};
            }""")
            self.assertTrue(raced["latest"])
            self.assertFalse(raced["stale"])
            self.assertEqual(raced["state"], "happy")
            self.assertEqual(raced["strategy"], "layered_sprite")
            self.assertTrue(raced["same"])
            restored = page.evaluate("""async () => {
                const art = document.querySelector('#stage-character-art');
                const hooks = window.__projectSnowTest;
                const previousSurface = art.src;
                const decode = HTMLImageElement.prototype.decode;
                let release, waiting;
                const gate = new Promise((resolve) => { release = resolve; });
                const started = new Promise((resolve) => { waiting = resolve; });
                HTMLImageElement.prototype.decode = async function() {
                    if (this.src.startsWith('data:image/') && [...new Uint8Array(await crypto.subtle.digest('SHA-256', await (await fetch(this.src)).arrayBuffer()))].map(value => value.toString(16).padStart(2,'0')).join('') === window.__slowLayerHash) {
                        waiting();
                        await gate;
                    }
                    return decode.call(this);
                };
                try {
                    const slow = hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'thinking');
                    await started;
                    const restored = await hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'happy');
                    release();
                    const stale = await slow;
                    return {restored, stale, state: art.dataset.expressionState, same: art.src === previousSurface};
                } finally {
                    release();
                    HTMLImageElement.prototype.decode = decode;
                }
            }""")
            self.assertEqual(restored, {"restored": True, "stale": False, "state": "happy", "same": True})
            performed = page.evaluate("""async () => {
                const art = document.querySelector('#stage-character-art');
                const hooks = window.__projectSnowTest;
                await hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'neutral', 'friendly_greeting');
                const selected = {id: art.dataset.presentationId, cue: art.dataset.performanceId,
                    state: art.dataset.expressionState, width: art.naturalWidth};
                await hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'happy', 'quiet_resolve');
                const removedCue = art.dataset.performanceId;
                await hooks.updateStageCharacterArt(art, window.__nativeTestCharacter, 'happy', 'another_characters_cue');
                const result = {selected, removedCue, fallbackId: art.dataset.presentationId, fallbackCue: art.dataset.performanceId};
                // Rendering the inactive stage must not restart a cancelled image load.
                hooks.renderStage();
                result.textStageHidden = art.hidden && !art.getAttribute('src');
                return result;
            }""")
            self.assertEqual(performed["selected"], {"id": "performance.friendly_greeting", "cue": "friendly_greeting", "state": "neutral", "width": 6})
            self.assertEqual(performed["fallbackId"], "expression.happy")
            self.assertEqual(performed["fallbackCue"], "")
            self.assertEqual(performed["removedCue"], "")
            self.assertTrue(performed["textStageHidden"])
            browser.close()

    def test_character_media_integrity_rejects_valid_same_size_replacements_and_times_out(self) -> None:
        from io import BytesIO
        from PIL import Image

        images = {}
        for name, size, color, file_format in (
            ("neutral.webp", (6, 8), (30, 60, 90, 255), "WEBP"),
            ("angry.webp", (6, 8), (60, 160, 80, 255), "WEBP"),
            ("substitute.webp", (6, 8), (200, 30, 40, 255), "WEBP"),
            ("flat.webp", (6, 8), (90, 100, 120, 255), "WEBP"),
            ("underlay.png", (6, 8), (20, 30, 40, 255), "PNG"),
            ("head.png", (2, 2), (160, 150, 140, 255), "PNG"),
            ("substitute.png", (2, 2), (240, 10, 30, 255), "PNG"),
        ):
            buffer = BytesIO()
            Image.new("RGBA", size, color).save(buffer, format=file_format, lossless=True, compress_level=0)
            images[name] = buffer.getvalue()
        self.assertEqual(len(images["head.png"]), len(images["substitute.png"]))
        payload = PublicFrontendHandler._expression_manifest("25b23cb64398")
        for state, name in (("neutral", "neutral.webp"), ("angry", "angry.webp"), ("happy", "flat.webp")):
            payload["expressions"][state]["stage_asset_path"] = "/integrity/" + name
            payload["expressions"][state]["stage_asset_sha256"] = sha256(images[name]).hexdigest()
        payload["expressions"]["happy"]["presentation_id"] = "expression.happy"
        payload["presentations"] = {"expression.happy": {
            "render_strategy": "layered_sprite", "canvas": {"width": 6, "height": 8},
            "layers": [
                {"asset_path": "/integrity/underlay.png", "asset_sha256": sha256(images["underlay.png"]).hexdigest(),
                 "dimensions": {"width": 6, "height": 8}, "position": {"x": 0, "y": 0}, "composite": "source-over"},
                {"asset_path": "/integrity/head.png", "asset_sha256": sha256(images["head.png"]).hexdigest(),
                 "dimensions": {"width": 2, "height": 2}, "position": {"x": 2, "y": 1}, "composite": "source-over"},
            ],
        }}
        original_manifest = json.dumps(payload).encode()
        changed = json.loads(original_manifest)
        changed["expressions"]["neutral"]["stage_asset_path"] = "/integrity/substitute.webp"
        changed["expressions"]["neutral"]["stage_asset_sha256"] = sha256(images["substitute.webp"]).hexdigest()
        requests = []

        def intercept(route):
            path = urlparse(route.request.url).path
            requests.append(path)
            name = path.rsplit("/", 1)[-1]
            if name == "manifest.json":
                body, content_type = original_manifest, "application/json"
            elif name == "changed.json":
                body, content_type = json.dumps(changed).encode(), "application/json"
            else:
                # Both substitutions remain decodable at the declared dimensions.
                actual = {"angry.webp": "substitute.webp", "head.png": "substitute.png"}.get(name, name)
                body = images[actual]
                content_type = "image/png" if name.endswith(".png") else "image/webp"
            route.fulfill(status=200, content_type=content_type, body=body)

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.route("**/integrity/*", intercept)
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator('[data-character="25b23cb64398"]').click()
            result = page.evaluate("""async hash => {
                const hooks = window.__projectSnowTest;
                const character = {character_id:'25b23cb64398', expression_manifest_url:'/integrity/manifest.json', expression_manifest_sha256:hash};
                const art = document.querySelector('#stage-character-art');
                const digest = async () => [...new Uint8Array(await crypto.subtle.digest('SHA-256', await (await fetch(art.src)).arrayBuffer()))].map(value => value.toString(16).padStart(2,'0')).join('');
                const nativeDecode = HTMLImageElement.prototype.decode;
                const decoded = [];
                HTMLImageElement.prototype.decode = async function() {
                    if(this.src.startsWith('data:image/')) decoded.push([...new Uint8Array(await crypto.subtle.digest('SHA-256', await (await fetch(this.src)).arrayBuffer()))].map(value => value.toString(16).padStart(2,'0')).join(''));
                    return nativeDecode.call(this);
                };
                try {
                    const webpReady = await hooks.updateStageCharacterArt(art, character, 'angry');
                    const webp = {ready:webpReady,state:art.dataset.expressionState,sha256:await digest()};
                    const pngReady = await hooks.updateStageCharacterArt(art, character, 'happy');
                    const png = {ready:pngReady,state:art.dataset.expressionState,sha256:await digest(),strategy:art.dataset.renderStrategy};
                    const tampered = await hooks.updateStageCharacterArt(art,{...character,expression_manifest_url:'/integrity/changed.json'},'neutral');
                    const tamperedHidden = art.hidden && !art.hasAttribute('src');
                    const absent = await hooks.updateStageCharacterArt(art,{...character,expression_manifest_sha256:''},'neutral');
                    const absentHidden = art.hidden && !art.hasAttribute('src');
                    window.__integrityCharacter = character;
                    return {webp,png,tampered,tamperedHidden,absent,absentHidden,decoded};
                } finally { HTMLImageElement.prototype.decode = nativeDecode; }
            }""", sha256(original_manifest).hexdigest())
            self.assertEqual(result["webp"], {"ready": True, "state": "neutral", "sha256": sha256(images["neutral.webp"]).hexdigest()})
            self.assertEqual(result["png"], {"ready": True, "state": "happy", "sha256": sha256(images["flat.webp"]).hexdigest(), "strategy": "single_sprite"})
            self.assertNotIn(sha256(images["substitute.webp"]).hexdigest(), result["decoded"])
            self.assertNotIn(sha256(images["substitute.png"]).hexdigest(), result["decoded"])
            self.assertIn(sha256(images["neutral.webp"]).hexdigest(), result["decoded"])
            self.assertIn(sha256(images["flat.webp"]).hexdigest(), result["decoded"])
            self.assertFalse(result["tampered"])
            self.assertTrue(result["tamperedHidden"])
            self.assertFalse(result["absent"])
            self.assertTrue(result["absentHidden"])
            self.assertNotIn("/integrity/substitute.webp", requests)
            self.assertEqual(requests.count("/integrity/manifest.json"), 1)
            deadline = page.evaluate("""async () => {
                const hooks = window.__projectSnowTest;
                const art = document.querySelector('#stage-character-art');
                const character = window.__integrityCharacter;
                await hooks.updateStageCharacterArt(art, character, 'neutral');
                art.dataset.expressionCharacterId = 'another-character';
                const nativeFetch = window.fetch, later = window.setTimeout;
                let aborted = false;
                window.fetch = (url, options) => String(url).endsWith('/integrity/hanging.json')
                    ? new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {
                        aborted = true; reject(new DOMException('Aborted', 'AbortError'));
                    }, {once:true})) : nativeFetch(url, options);
                window.setTimeout = (callback, ms, ...args) => later(callback, ms === 20000 ? 50 : ms, ...args);
                try {
                    const ready = await hooks.updateStageCharacterArt(art, {...character,expression_manifest_url:'/integrity/hanging.json'}, 'neutral');
                    return {ready,aborted,hidden:art.hidden&&!art.hasAttribute('src')};
                } finally { window.fetch = nativeFetch; window.setTimeout = later; }
            }""")
            self.assertEqual(deadline, {"ready": False, "aborted": True, "hidden": True})
            decode_deadline = page.evaluate("""async () => {
                const hooks = window.__projectSnowTest, art = document.querySelector('#stage-character-art');
                hooks.clearStagePresentationCache();
                const decode = HTMLImageElement.prototype.decode, later = window.setTimeout;
                const preloaded = [];
                HTMLImageElement.prototype.decode = function() {
                    if (this.src.startsWith('data:image/')) preloaded.push(this);
                    return new Promise(() => {});
                };
                window.setTimeout = (callback, ms, ...args) => later(callback, ms === 8000 ? 30 : ms, ...args);
                try {
                    const ready = await hooks.updateStageCharacterArt(art, window.__integrityCharacter, 'happy');
                    return {ready,hidden:art.hidden&&!art.hasAttribute('src'),attempted:preloaded.length,
                        unreleased:preloaded.filter(image => Boolean(image.getAttribute('src'))).length};
                } finally {
                    HTMLImageElement.prototype.decode = decode; window.setTimeout = later;
                }
            }""")
            self.assertGreater(decode_deadline.pop("attempted"), 0)
            self.assertEqual(decode_deadline, {"ready": False, "hidden": True, "unreleased": 0})
            browser.close()

    def test_mia_stage_art_syncs_all_states_and_actions_stay_out_of_dialogue(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator('[data-character="702f4375675b"]').click()
            self._configure_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
            page.wait_for_function(
                "() => document.querySelector('#stage-speech').textContent === '你来了。'"
            )
            page.locator("#stage-character-art").wait_for(state="visible")

            states = page.evaluate(
                """async () => {
                    const hooks = window.__projectSnowTest;
                    const character = {character_id: '702f4375675b', display_name: '米娅', avatar: null};
                    const manifest = await (await fetch(hooks.expressionManifestUrlForCharacter(character))).json();
                    const art = document.querySelector('#stage-character-art');
                    const result = {};
                    for (const state of Object.keys(manifest.expressions)) {
                        await hooks.updateStageCharacterArt(art, character, state);
                        result[state] = {
                            artState: art.dataset.expressionState,
                            artSrc: art.getAttribute('src') || '',
                            sha256: [...new Uint8Array(await crypto.subtle.digest('SHA-256', await (await fetch(art.src)).arrayBuffer()))].map(value => value.toString(16).padStart(2,'0')).join(''),
                            hidden: art.hidden,
                        };
                    }
                    return result;
                }"""
            )
            self.assertEqual(len(states), 18)
            for state, values in states.items():
                self.assertEqual(values["artState"], state)
                self.assertTrue(values["artSrc"].startswith("data:image/"))
                manifest = json.loads((PUBLIC_ROOT / "assets/expressions/mia/manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(values["sha256"], manifest["expressions"][state]["stage_asset_sha256"])
                self.assertFalse(values["hidden"])
            self.assertEqual(page.locator("#stage-portrait").count(), 0)
            self.assertEqual(page.locator("#stage-portrait-avatar").count(), 0)

            page.evaluate(
                """() => {
                    window.__stageArtTransitions = [];
                    window.__stageArtTransitionObserver?.disconnect();
                    window.__stageArtTransitionObserver = new MutationObserver((records) => {
                        for (const record of records) {
                            for (const added of record.addedNodes) {
                                if (!(added instanceof HTMLImageElement) || !added.classList.contains('stage-character-art-outgoing')) continue;
                                const incoming = document.querySelector('#stage-character-art');
                                window.__stageArtTransitions.push({
                                    outgoingSrc: added.getAttribute('src') || '',
                                    outgoingCharacterId: added.dataset.expressionCharacterId || '',
                                    incomingCharacterId: incoming?.dataset.expressionCharacterId || '',
                                });
                            }
                        }
                    });
                    window.__stageArtTransitionObserver.observe(
                        document.querySelector('#scene-stage'),
                        {childList: true},
                    );
                }"""
            )
            page.locator('[data-character="78aa7ab99154"]').click()
            page.locator("#stage-character-name", has_text="伊切尔").wait_for(state="visible")
            page.wait_for_function(
                "() => document.querySelector('#stage-character-art')?.dataset.expressionCharacterId === '78aa7ab99154'"
            )
            non_mia_loaded = page.evaluate(
                """() => {
                    const art = document.querySelector('#stage-character-art');
                    return {
                        visible: !art.hidden && art.hasAttribute('src'),
                        characterId: art.dataset.expressionCharacterId,
                        scale: art.style.getPropertyValue('--stage-character-scale'),
                        focus: art.style.getPropertyValue('--stage-character-focus-x'),
                    };
                }"""
            )
            self.assertTrue(non_mia_loaded["visible"])
            self.assertEqual(non_mia_loaded["characterId"], "78aa7ab99154")
            self.assertEqual(non_mia_loaded["scale"], "1")
            self.assertEqual(non_mia_loaded["focus"], "50%")
            page.wait_for_function(
                "() => window.__stageArtTransitions.some((row) => row.incomingCharacterId === '78aa7ab99154')"
            )
            transition = page.evaluate(
                "() => window.__stageArtTransitions.find((row) => row.incomingCharacterId === '78aa7ab99154')"
            )
            self.assertEqual(transition["outgoingCharacterId"], "702f4375675b")
            self.assertTrue(transition["outgoingSrc"].startswith("data:image/"))
            page.wait_for_function(
                "() => document.querySelectorAll('.stage-character-art-outgoing').length === 0"
            )
            page.evaluate("() => document.querySelector('#presence-dialog')?.close()")
            page.locator('[data-character="702f4375675b"]').click()
            page.locator("#stage-character-name", has_text="米娅").wait_for(state="visible")
            page.evaluate("() => document.querySelector('#presence-dialog')?.close()")
            page.evaluate(
                """async () => window.__projectSnowTest.updateStageCharacterArt(
                    document.querySelector('#stage-character-art'),
                    {character_id: '702f4375675b', display_name: '米娅', avatar: null},
                    'neutral',
                )"""
            )
            fallback_state = page.evaluate(
                """async () => {
                    window.__projectSnowTest.clearStagePresentationCache();
                    const NativeImage = window.Image;
                    let decodeFailures = 1;
                    class FallbackProbe {
                        constructor() { this.complete = false; this.naturalWidth = 0; }
                        set src(value) {
                            queueMicrotask(() => {
                                if (decodeFailures-- > 0) this.onerror?.();
                                else { this.naturalWidth = 620; this.onload?.(); }
                            });
                        }
                    }
                    window.Image = FallbackProbe;
                    try {
                        const art = document.querySelector('#stage-character-art');
                        await window.__projectSnowTest.updateStageCharacterArt(
                            art,
                            {character_id: '702f4375675b', display_name: '米娅', avatar: null},
                            'angry',
                        );
                        return {state: art.dataset.expressionState, src: art.getAttribute('src') || ''};
                    } finally {
                        window.Image = NativeImage;
                    }
                }"""
            )
            self.assertEqual(fallback_state["state"], "neutral")
            self.assertTrue(fallback_state["src"].startswith("data:image/"))
            page.locator("#toggle-stage-ui").click()
            self.assertTrue(page.locator("#stage-character-art").is_visible())
            page.locator("#restore-stage-ui").click()

            page.locator("#message-input").fill("显式动作标签回归")
            page.locator("#send-message").click()
            page.wait_for_function(
                "() => document.querySelector('#stage-narration').textContent.includes('米娅眼睛一亮')"
            )
            page.wait_for_function(
                "() => document.querySelector('#stage-speech').textContent.includes('我正好想给你看我新贴的照片墙呢！')"
            )
            self.assertNotIn("〔动作〕", page.locator("#stage-speech").inner_text())
            self.assertNotIn("米娅眼睛一亮", page.locator("#stage-speech").inner_text())

            for viewport in (
                {"width": 1440, "height": 900},
                {"width": 1280, "height": 800},
                {"width": 390, "height": 844},
                {"width": 320, "height": 720},
            ):
                page.set_viewport_size(viewport)
                bounds = page.evaluate(
                    """() => {
                        const stage = document.querySelector('#scene-stage').getBoundingClientRect();
                        const art = document.querySelector('#stage-character-art').getBoundingClientRect();
                        const dialogue = document.querySelector('#stage-dialogue').getBoundingClientRect();
                        const narration = document.querySelector('#stage-narration').getBoundingClientRect();
                        const composer = document.querySelector('.composer-row').getBoundingClientRect();
                        return {
                            stage, art, dialogue, narration, composer,
                            overflow: document.documentElement.scrollWidth - innerWidth,
                        };
                    }"""
                )
                expected_dialogue_height = min(
                    210 if viewport["width"] > 560 else 168,
                    max(
                        148 if viewport["width"] > 560 else 116,
                        viewport["height"] * (0.23 if viewport["width"] > 560 else 0.21),
                    ),
                )
                self.assertAlmostEqual(bounds["dialogue"]["height"], expected_dialogue_height, delta=2)
                self.assertGreaterEqual(bounds["art"]["top"], bounds["stage"]["top"] - 1)
                self.assertAlmostEqual(bounds["art"]["bottom"], bounds["stage"]["bottom"], delta=1)
                self.assertGreater(bounds["art"]["bottom"], bounds["dialogue"]["top"] + 20)
                self.assertLessEqual(bounds["narration"]["bottom"], bounds["dialogue"]["top"] + 1)
                self.assertAlmostEqual(bounds["dialogue"]["width"], bounds["composer"]["width"], delta=1)
                self.assertLessEqual(bounds["overflow"], 1)
            browser.close()

    def test_model_stage_motion_is_decoupled_one_shot_and_reduced_motion_safe(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator('[data-character="702f4375675b"]').click()
            self._configure_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
            page.locator("#stage-character-art").wait_for(state="visible")

            art = page.locator("#stage-character-art")
            page.locator("#message-input").fill("演出靠近测试")
            page.locator("#send-message").click()
            page.wait_for_function(
                "() => document.querySelector('#stage-character-art')?.dataset.stageMotionPlayCount === '1'",
                timeout=12000,
            )
            self.assertTrue((art.get_attribute("data-stage-motion-key") or "").endswith(":lean_in"))
            self.assertEqual(art.get_attribute("data-expression-state"), "surprised")
            self.assertEqual(page.locator("#stage-narration").inner_text(), "正在看雪")
            self.assertNotIn("stage_motion", page.locator("#stage-speech").inner_text())
            page.evaluate("() => { window.__projectSnowTest.renderStage(); window.__projectSnowTest.renderStage(); }")
            page.wait_for_timeout(700)
            self.assertEqual(art.get_attribute("data-stage-motion-play-count"), "1")

            page.wait_for_function("() => !document.querySelector('#send-message').disabled")
            page.locator("#message-input").fill("动作无演出测试")
            page.locator("#send-message").click()
            page.locator("#stage-speech").get_by_text("动作文字不会自动触发演出。").wait_for(state="visible", timeout=8000)
            self.assertIn("米娅轻轻抬起手。", page.locator("#stage-narration").inner_text())
            self.assertEqual(art.get_attribute("data-expression-state"), "neutral")
            self.assertEqual(art.get_attribute("data-stage-motion-play-count"), "1")

            page.wait_for_function("() => !document.querySelector('#send-message').disabled")
            page.locator("#message-input").fill("第二次演出测试")
            page.locator("#send-message").click()
            page.wait_for_function(
                "() => document.querySelector('#stage-character-art')?.dataset.stageMotionPlayCount === '2'",
                timeout=12000,
            )
            self.assertTrue((art.get_attribute("data-stage-motion-key") or "").endswith(":startle"))

            page.wait_for_function("() => !document.querySelector('#send-message').disabled")
            page.locator("#message-input").fill("颤动切换测试")
            page.locator("#send-message").click()
            page.wait_for_function(
                "() => document.querySelector('#stage-character-art')?.dataset.stageMotionPlayCount === '3'",
                timeout=12000,
            )
            page.locator('[data-character="25b23cb64398"]').click()
            page.locator("#stage-character-name", has_text="凯茜娅").wait_for(state="visible")
            art.wait_for(state="visible")
            self.assertEqual(art.get_attribute("data-expression-character-id"), "25b23cb64398")
            self.assertIsNone(art.get_attribute("data-stage-motion"))
            page.wait_for_function(
                """() => {
                    const art = document.querySelector('#stage-character-art');
                    return art.getAnimations().length === 0
                        && !art.dataset.stageArtTransition
                        && document.querySelectorAll('.stage-character-art-outgoing').length === 0;
                }"""
            )
            page.reload(wait_until="networkidle")
            # Startup selects the first contact; explicitly reopen Mia's saved
            # in-person history before checking that old cues remain static.
            page.locator('[data-character="702f4375675b"]').click()
            page.wait_for_function("() => document.querySelector('#stage-character-art').dataset.expressionCharacterId === '702f4375675b'")
            art.wait_for(state="visible")
            self.assertIsNone(art.get_attribute("data-stage-motion-play-count"))
            self.assertIsNone(art.get_attribute("data-stage-motion"))
            self.assertEqual(art.evaluate("element => element.getAnimations().length"), 0)
            browser.close()

            reduced_browser = _launch_browser(playwright)
            reduced_page = reduced_browser.new_page(
                viewport={"width": 390, "height": 844},
                reduced_motion="reduce",
            )
            reduced_page.goto(self.base_url, wait_until="networkidle")
            reduced_page.locator("#accept-experience-notice").click()
            reduced_page.locator("#open-contacts").click()
            self._configure_model(reduced_page)
            reduced_page.locator('[data-character="702f4375675b"]').click()
            reduced_page.locator("#go-in-person").click()
            reduced_page.locator("#confirm-presence-transition").click()
            reduced_page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
            reduced_page.locator("#stage-character-art").wait_for(state="visible", timeout=8000)
            reduced_page.locator("#message-input").fill("演出靠近测试")
            with reduced_page.expect_request(lambda request: request.url.endswith("/chat/stream"), timeout=12000):
                reduced_page.locator("#send-message").click()
            reduced_page.wait_for_function(
                "() => document.querySelector('#stage-character-art')?.dataset.stageMotionKey?.endsWith(':lean_in')",
                timeout=12000,
            )
            reduced_art = reduced_page.locator("#stage-character-art")
            self.assertIsNone(reduced_art.get_attribute("data-stage-motion-play-count"))
            self.assertEqual(reduced_art.evaluate("element => element.getAnimations().length"), 0)
            reduced_browser.close()

    def test_arrival_storage_completion_preserves_newly_typed_draft(self) -> None:
        PublicFrontendHandler.arrival_started = threading.Event()
        PublicFrontendHandler.arrival_release = threading.Event()
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(self.base_url, wait_until="networkidle")
                page.locator("#accept-experience-notice").click()
                page.locator("#open-contacts").click()
                self._configure_model(page)
                page.locator('[data-character="702f4375675b"]').click()
                page.locator("#active-character h1", has_text="米娅").wait_for()
                page.locator("#message-input").fill("米娅的文字通讯草稿")
                page.locator("#go-in-person").click()
                page.locator("#confirm-presence-transition").click()
                self.assertTrue(PublicFrontendHandler.arrival_started.wait(timeout=5))
                # Hold only the completion notification of the next real draft
                # transaction. This is the same-channel save after arrival;
                # typing must remain safe while persistence is still awaited.
                page.evaluate("""() => {
                    const put = IDBObjectStore.prototype.put;
                    const listen = IDBTransaction.prototype.addEventListener;
                    const later = window.setTimeout;
                    let draftTransaction = null;
                    IDBObjectStore.prototype.put = function(value, ...args) {
                        if (!draftTransaction && this.name === 'app_state' && value?.key === 'drafts') {
                            draftTransaction = this.transaction;
                        }
                        return put.call(this, value, ...args);
                    };
                    IDBTransaction.prototype.addEventListener = function(type, callback, options) {
                        if (type !== 'complete') return listen.call(this, type, callback, options);
                        return listen.call(this, type, event => {
                            const complete = () => typeof callback === 'function'
                                ? callback.call(this, event) : callback.handleEvent(event);
                            if (this === draftTransaction) {
                                window.__releaseDraftCommit = complete;
                                window.__draftCommitHeld = true;
                            } else complete();
                        }, options);
                    };
                    // Keep the 220ms autosave from refreshing the draft cache
                    // before the suspended restore; no wall-clock race remains.
                    window.setTimeout = (callback, delay, ...args) => delay === 220
                        ? 0 : later(callback, delay, ...args);
                    window.__restoreDraftGate = () => {
                        IDBObjectStore.prototype.put = put;
                        IDBTransaction.prototype.addEventListener = listen;
                        window.setTimeout = later;
                    };
                }""")
                PublicFrontendHandler.arrival_release.set()
                page.wait_for_function("window.__draftCommitHeld === true")
                page.locator("#message-input").fill("演出靠近测试")
                self.assertTrue(page.locator("#send-message").is_disabled())
                page.evaluate("window.__releaseDraftCommit()")
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                self.assertEqual(page.locator("#message-input").input_value(), "演出靠近测试")
                page.evaluate("window.__restoreDraftGate()")
                with page.expect_request(lambda request: request.url.endswith("/chat/stream")):
                    page.locator("#send-message").click()
                page.wait_for_function(
                    "document.querySelector('#stage-character-art').dataset.stageMotionKey?.endsWith(':lean_in')"
                )
                self.assertEqual(PublicFrontendHandler.chat_payloads[-1]["message"], "演出靠近测试")
                self.assertEqual(PublicFrontendHandler.chat_payloads[-1]["communication_channel"], "in_person")
                self.assertEqual(page.locator("#stage-character-art").evaluate("node => node.getAnimations().length"), 0)
                self.assertEqual(errors, [])
                page.wait_for_function("!document.querySelector('#send-message').disabled")
                page.locator("#message-input").fill("米娅的面对面草稿")
                page.locator("#open-communicator").click()
                page.locator("#text-surface").wait_for(state="visible")
                self.assertEqual(page.locator("#message-input").input_value(), "米娅的文字通讯草稿")
                page.locator("#open-contacts").click()
                page.locator('[data-character="25b23cb64398"]').click()
                page.locator("#active-character h1", has_text="凯茜娅").wait_for()
                self.assertEqual(page.locator("#message-input").input_value(), "")
                page.locator("#open-contacts").click()
                page.locator('[data-character="702f4375675b"]').click()
                page.locator("#active-character h1", has_text="米娅").wait_for()
                self.assertEqual(page.locator("#message-input").input_value(), "米娅的文字通讯草稿")
            finally:
                browser.close()

    def test_feedback_turnstile_is_visible_retryable_and_preserves_the_form(self) -> None:
        PublicFrontendHandler.turnstile_site_key = "e2e-site-key"
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.route(
                "https://challenges.cloudflare.com/**",
                lambda route: route.fulfill(status=200, content_type="application/javascript", body=""),
            )
            page.add_init_script(
                """
                window.__turnstileMode = 'error';
                window.__turnstileInsideFeedbackDialog = false;
                window.__turnstileOptions = null;
                window.turnstile = {
                    render(container, options) {
                        if (window.__turnstileMode === 'unavailable') throw new Error('load failed');
                        window.__turnstileInsideFeedbackDialog = Boolean(container.closest('#feedback-dialog'));
                        window.__turnstileOptions = options;
                        const challenge = document.createElement('div');
                        challenge.dataset.mockTurnstile = 'true';
                        challenge.textContent = '人机验证测试';
                        container.append(challenge);
                        return 17;
                    },
                    execute() {
                        queueMicrotask(() => {
                            if (window.__turnstileMode === 'error') window.__turnstileOptions['error-callback']();
                            else if (window.__turnstileMode === 'expired') window.__turnstileOptions['expired-callback']();
                            else window.__turnstileOptions.callback('turnstile-e2e-token');
                        });
                    },
                    remove() {},
                };
                """
            )
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#open-global-feedback").click()
            page.locator("#feedback-body").fill("验证失败后必须保留的反馈正文")
            submit = page.locator("#feedback-form button[type=submit]")
            retry = page.locator("#feedback-retry-verification")

            submit.click()
            retry.wait_for(state="visible")
            self.assertTrue(page.evaluate("() => window.__turnstileInsideFeedbackDialog"))
            self.assertIn("验证", page.locator("#feedback-error").inner_text())
            self.assertEqual(page.locator("#feedback-body").input_value(), "验证失败后必须保留的反馈正文")
            self.assertTrue(submit.is_enabled())
            self.assertEqual(PublicFrontendHandler.feedback_attempts, 0)

            page.evaluate("() => { window.__turnstileMode = 'expired'; }")
            retry.click()
            page.locator("#feedback-error").get_by_text("人机验证已过期").wait_for(state="visible")
            self.assertEqual(PublicFrontendHandler.feedback_attempts, 0)

            page.evaluate("() => { window.__turnstileMode = 'unavailable'; }")
            retry.click()
            page.locator("#feedback-error").get_by_text("验证组件加载失败").wait_for(state="visible")
            self.assertEqual(page.locator("#feedback-body").input_value(), "验证失败后必须保留的反馈正文")
            self.assertEqual(PublicFrontendHandler.feedback_attempts, 0)

            PublicFrontendHandler.feedback_mode = "http_error"
            page.evaluate("() => { window.__turnstileMode = 'success'; }")
            retry.click()
            page.locator("#feedback-error").get_by_text("请求失败").wait_for(state="visible")
            self.assertEqual(PublicFrontendHandler.feedback_attempts, 1)
            self.assertEqual(page.locator("#feedback-body").input_value(), "验证失败后必须保留的反馈正文")

            PublicFrontendHandler.feedback_mode = "success"
            retry.click()
            page.locator("#feedback-dialog").wait_for(state="hidden")
            self.assertEqual(PublicFrontendHandler.feedback_attempts, 2)
            payload = PublicFrontendHandler.feedback_payload or {}
            self.assertEqual(payload.get("turnstile_token"), "turnstile-e2e-token")
            self.assertEqual(payload.get("body"), "验证失败后必须保留的反馈正文")
            browser.close()

    def test_text_and_in_person_surfaces_share_local_continuity(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
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
            page.locator("#toggle-action").click()
            self.assertFalse(page.locator("#analyst-action-field").is_visible())
            page.locator("#toggle-action").click()
            self.assertEqual(page.locator("#action-input").input_value(), "向她挥了挥手")
            page.locator("#send-message").click()
            page.locator("#stage-speech").get_by_text("晚上好，分析员。").wait_for(state="visible")
            page.locator("#open-transcript").click()
            transcript = page.locator("#transcript-content").inner_text()
            self.assertIn("晚上好", transcript)
            self.assertIn("向她挥了挥手", transcript)
            browser.close()

    def test_waiting_indicators_arrival_dedup_and_reduced_motion(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
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
            page.locator('[data-character="9f5804761c56"]').click()
            page.locator("#active-character h1", has_text="安卡希雅").wait_for(
                state="visible"
            )
            PublicFrontendHandler.arrival_release.set()
            page.wait_for_function(
                "() => document.querySelector('#presence-arrival-loading').hidden"
            )
            page.locator('[data-character="25b23cb64398"]').click()
            page.locator("#stage-character-name", has_text="凯茜娅").wait_for(
                state="visible"
            )
            self.assertTrue(
                page.locator("#presence-arrival-loading").evaluate(
                    "element => element.hidden"
                )
            )
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

    def test_switching_character_keeps_prior_request_in_background_and_defers_presence(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
            self.assertEqual(PublicFrontendHandler.presence_resolve_count, 1)
            self._configure_model(page)

            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("后台完成这一轮")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            page.locator('[data-character="9f5804761c56"]').click()
            page.locator("#active-character h1", has_text="安卡希雅").wait_for(
                state="visible"
            )

            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            self.assertTrue(page.locator("#send-message").is_disabled())
            self.assertTrue(page.locator("#go-in-person").is_disabled())
            self.assertTrue(page.locator("#open-movement-shortcuts").is_disabled())
            self.assertEqual(PublicFrontendHandler.presence_resolve_count, 1)

            PublicFrontendHandler.chat_stream_release.set()
            page.locator("#request-status").get_by_text("场景已重新读取").wait_for(
                state="visible"
            )
            self.assertEqual(PublicFrontendHandler.presence_resolve_count, 2)
            self.assertFalse(page.locator("#send-message").is_disabled())
            self.assertNotIn("晚上好，分析员。", page.locator("#timeline").inner_text())
            browser.close()

    def test_desktop_200_percent_zoom_mobile_and_narrow_layouts_do_not_overflow(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            # A 1280 CSS-pixel desktop viewport at 200% browser zoom exposes
            # roughly 640 CSS pixels to layout. Cover that reflow width once,
            # alongside the physical mobile and narrow-screen breakpoints.
            for width, height in ((1280, 800), (640, 400), (390, 844), (320, 720)):
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
                scroll = page.evaluate(
                    """() => {
                        const root = document.scrollingElement;
                        const before = root.scrollTop;
                        window.scrollTo(0, root.scrollHeight);
                        return {
                            before,
                            after: root.scrollTop,
                            scrollHeight: root.scrollHeight,
                            clientHeight: root.clientHeight,
                            overflowY: getComputedStyle(document.documentElement).overflowY,
                        };
                    }"""
                )
                self.assertGreater(scroll["scrollHeight"], scroll["clientHeight"])
                self.assertGreater(scroll["after"], scroll["before"])
                self.assertEqual(scroll["overflowY"], "auto")
                page.close()
            browser.close()

    def test_narrow_presence_label_ellipsizes_without_clipping_action_prefix(self) -> None:
        location = "基地公共休息室与观景走廊"
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                for width in (390, 320):
                    with self.subTest(width=width):
                        page = browser.new_page(viewport={"width": width, "height": 844})

                        def presence(route):
                            payload = route.fetch().json()
                            payload["scene_state"]["character_location"] = location
                            route.fulfill(json=payload)

                        page.route("**/public/v1/presence/resolve", presence)
                        page.goto(self.base_url, wait_until="networkidle")
                        page.locator("#accept-experience-notice").click()
                        page.wait_for_function(
                            "document.querySelector('#connection-status').textContent === '服务已连接'"
                        )
                        label = page.locator("#go-in-person-label")
                        self.assertEqual(label.inner_text(), f"去见她 · {location}")
                        self.assertEqual(
                            page.locator("#go-in-person").get_attribute("aria-label"), label.inner_text()
                        )
                        bounds = label.evaluate("""node => {
                          const range = document.createRange();
                          range.setStart(node.firstChild,0);range.setEnd(node.firstChild,3);
                          const prefix=range.getBoundingClientRect(), rect=node.getBoundingClientRect();
                          const button=node.parentElement.getBoundingClientRect();
                          const style=getComputedStyle(node);
                          const measure=document.createElement('canvas').getContext('2d');
                          measure.font=style.font;
                          return {prefixLeft:prefix.left,prefixRight:prefix.right,left:rect.left,
                            right:rect.right,buttonLeft:button.left,buttonRight:button.right,
                            ellipsis:measure.measureText('…').width,
                            overflow:style.textOverflow,scroll:node.scrollWidth,client:node.clientWidth};
                        }""")
                        self.assertEqual(bounds["overflow"], "ellipsis")
                        self.assertGreater(bounds["scroll"], bounds["client"])
                        self.assertGreaterEqual(bounds["left"], bounds["buttonLeft"])
                        self.assertLessEqual(bounds["right"], bounds["buttonRight"])
                        self.assertGreaterEqual(bounds["prefixLeft"], bounds["left"] - 1)
                        self.assertLessEqual(bounds["prefixRight"], bounds["right"] - bounds["ellipsis"] + 1)
                        self._assert_no_horizontal_overflow(page)
                        page.close()
            finally:
                browser.close()

    def test_composer_multiline_height_scroll_and_draft_reload_at_narrow_and_zoom_widths(self) -> None:
        from tests.test_public_frontend_reliability import PublicFrontendReliabilityTests

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            try:
                # 640x400 CSS px at DPR 2 models the layout/raster of a
                # 1280x800 desktop at 200% browser zoom, not a CSS zoom override.
                for width, height, scale, font in (
                    (390, 844, 1, None), (320, 720, 1, None), (640, 400, 2, None),
                    (390, 844, 1, "Arial"),
                ):
                    with self.subTest(width=width, scale=scale, font=font):
                        page = browser.new_page(
                            viewport={"width": width, "height": height}, device_scale_factor=scale
                        )
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(self.base_url, wait_until="networkidle")
                        page.locator("#accept-experience-notice").click()
                        page.wait_for_function(
                            "document.querySelector('#connection-status').textContent === '服务已连接'"
                        )
                        page.locator("#skip-onboarding").click()
                        composer = page.locator("#message-input")
                        initial_height = composer.evaluate("node => node.getBoundingClientRect().height")
                        draft = "第一行草稿\n第二行草稿\n第三行草稿"
                        composer.fill(draft)
                        page.wait_for_function(
                            "document.querySelector('#message-input').scrollHeight"
                            " <= document.querySelector('#message-input').clientHeight + 1",
                            timeout=3000,
                        )
                        self.assertGreater(composer.evaluate("node=>node.clientHeight"), initial_height + 30)
                        PublicFrontendReliabilityTests.wait_saved_draft(page, draft)
                        page.reload(wait_until="networkidle")
                        page.wait_for_function(
                            "document.querySelector('#connection-status').textContent === '服务已连接'"
                        )
                        self.assertEqual(composer.input_value(), draft)
                        page.wait_for_function(
                            "document.querySelector('#message-input').scrollHeight"
                            " <= document.querySelector('#message-input').clientHeight + 1",
                            timeout=3000,
                        )
                        self._assert_no_horizontal_overflow(page)
                        self.assertLessEqual(
                            composer.evaluate("node=>node.getBoundingClientRect().bottom"), height
                        )
                        if directory := os.environ.get("SNOW_COMPOSER_SCREENSHOTS"):
                            path = Path(directory)
                            path.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(path / f"composer-{width}-{scale}x.png"))
                        if font:
                            composer.evaluate("(node,font)=>node.style.fontFamily=font", font)
                        composer.fill("\n".join(f"较长草稿第{index}行" for index in range(20)))
                        metrics = composer.evaluate("""node => ({
                          height:node.getBoundingClientRect().height,
                          max:parseFloat(getComputedStyle(node).maxHeight),
                          scroll:node.scrollHeight,client:node.clientHeight,
                          overflow:getComputedStyle(node).overflowY
                        })""")
                        self.assertLessEqual(metrics["height"], metrics["max"] + 1)
                        self.assertGreater(metrics["scroll"], metrics["client"])
                        self.assertEqual(metrics["overflow"], "auto")
                        self.assertGreater(
                            composer.evaluate(
                                "node=>{node.scrollTop=node.scrollHeight;return node.scrollTop}"
                            ), 0
                        )
                        composer.press("Control+End")
                        composer.press("Shift+Enter")
                        composer.press("x")
                        page.wait_for_function(END_CARET_VISIBLE, timeout=3000)
                        # A resize implementation that undoes native caret
                        # scrolling must still fail this font-aware check.
                        composer.evaluate("node=>{node.scrollTop=0}")
                        self.assertFalse(page.evaluate(END_CARET_VISIBLE))
                        self.assertLessEqual(
                            composer.evaluate("node=>node.getBoundingClientRect().bottom"), height
                        )
                        composer.fill("")
                        self.assertAlmostEqual(
                            composer.evaluate("node=>node.getBoundingClientRect().height"),
                            initial_height, delta=1
                        )
                        if width == 390:
                            wrapped = "宽度变化应重新计算输入高度" * 2
                            composer.fill(wrapped)
                            wide_height = composer.evaluate("node=>node.getBoundingClientRect().height")
                            composer.evaluate("node=>node.setSelectionRange(2,5)")
                            page.set_viewport_size({"width": 320, "height": 844})
                            page.wait_for_function(
                                "previous=>document.querySelector('#message-input').getBoundingClientRect().height"
                                " > previous", arg=wide_height, timeout=3000,
                            )
                            self.assertEqual(composer.input_value(), wrapped)
                            self.assertEqual(
                                composer.evaluate("node=>[node.selectionStart,node.selectionEnd]"), [2, 5]
                            )
                            page.set_viewport_size({"width": 390, "height": 844})
                            page.wait_for_function(
                                "previous=>document.querySelector('#message-input').getBoundingClientRect().height"
                                " <= previous + 1", arg=wide_height, timeout=3000,
                            )
                        self.assertEqual(errors, [])
                        page.close()
                self.assertEqual(PublicFrontendHandler.chat_payloads, [])
            finally:
                browser.close()

    def test_desktop_contacts_toggle_and_exact_pinyin_search(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
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
            self.assertEqual(page.locator("#open-stage-contacts svg").count(), 1)
            self.assertNotIn("⌄", page.locator("#open-stage-contacts").inner_text())
            page.locator("#open-communicator").click()
            page.locator("#text-surface").wait_for(state="visible")
            self.assertTrue(page.locator("#open-contacts").is_visible())
            page.wait_for_function("() => document.activeElement?.id === 'open-contacts'")
            self.assertTrue(page.locator("#open-contacts").evaluate("element => element === document.activeElement"))
            self.assertEqual(page.locator("#open-contacts").get_attribute("aria-expanded"), "false")
            page.locator("#open-contacts").click()
            self.assertEqual(page.locator("#open-contacts").get_attribute("aria-expanded"), "true")
            self.assertFalse(page.locator("#chat-app").evaluate("element => element.classList.contains('sidebar-collapsed')"))
            browser.close()

    def test_mobile_provider_buttons_and_page_wide_privacy_wheel(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#open-contacts").click()
            page.locator("#open-settings").click()
            self.assertTrue(page.locator("#provider-select").is_hidden())
            self.assertTrue(page.locator("#provider-options-mobile").is_visible())
            page.locator("#toggle-advanced-model").click()
            page.locator("#model-id").fill("old-provider-model")
            page.evaluate("""() => {
                const select = document.querySelector('#discovered-models');
                select.innerHTML = '<option value="old-provider-model">old-provider-model</option>';
                select.hidden = false;
            }""")
            page.locator('[data-provider-option="anthropic"]').click()
            self.assertEqual(page.locator("#provider-select").input_value(), "anthropic")
            self.assertEqual(page.locator('[data-provider-option="anthropic"]').get_attribute("aria-checked"), "true")
            self.assertEqual(page.locator("#model-id").input_value(), "")
            self.assertTrue(page.locator("#discovered-models").is_hidden())
            page.locator("#save-model").click()
            self.assertIn("填写或选择模型", page.locator("#setup-error").inner_text())
            page.locator('[data-provider-option="openai"]').click()
            self.assertEqual(page.locator("#provider-select").input_value(), "openai")

            page.set_viewport_size({"width": 1280, "height": 800})
            page.goto(f"{self.base_url}/privacy/", wait_until="networkidle")
            scroll_root = page.evaluate("() => document.scrollingElement === document.documentElement")
            self.assertTrue(scroll_root)
            page.locator(".privacy-hero h1").hover()
            page.mouse.wheel(0, 650)
            page.wait_for_function("() => document.scrollingElement.scrollTop > 0")
            first_scroll = page.evaluate("() => document.scrollingElement.scrollTop")
            self.assertGreater(first_scroll, 0)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.mouse.move(1270, 500)
            page.mouse.wheel(0, 650)
            page.wait_for_function("() => document.scrollingElement.scrollTop > 0")
            self.assertGreater(page.evaluate("() => document.scrollingElement.scrollTop"), 0)
            browser.close()

    def test_indexeddb_unavailable_uses_memory_and_chat_still_works(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.add_init_script(
                """
                Object.defineProperty(window, "indexedDB", {
                    configurable: true,
                    get() {
                        throw new DOMException("IndexedDB disabled for E2E", "SecurityError");
                    },
                });
                """
            )
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#system-banner").wait_for(state="visible")
            self.assertIn("本次聊天不会保存", page.locator("#system-banner").inner_text())
            page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
            self._configure_model(page)
            page.locator("#message-input").fill("内存会话仍可聊天")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(
                state="visible"
            )
            self.assertEqual(page.locator("#timeline .message.user").count(), 1)
            self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
            browser.close()

    def test_v092_frontend_contracts_are_non_blocking_and_opt_out_feedback(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()

            self.assertEqual(page.locator(".stage-dialogue-hint").count(), 0)
            self.assertIsNone(page.locator("#stage-dialogue").get_attribute("tabindex"))
            self.assertEqual(
                page.evaluate("""() => window.__projectSnowTest.escapeHtml(`模型" title="注入'`)"""),
                "模型&quot; title=&quot;注入&#39;",
            )
            mixed_blocks = page.evaluate(
                """() => window.__projectSnowTest.deriveDisplayBlocks([
                    {type: 'action', text: '动作一'},
                    {type: 'speech', text: '对白一。'},
                    {type: 'action', text: '动作二'},
                    {type: 'speech', text: '对白二。'},
                    {type: 'action', text: '动作三'},
                ], 'in_person')"""
            )
            self.assertEqual(
                [block["type"] for block in mixed_blocks],
                ["action", "speech", "action", "speech", "action"],
            )
            request_budget = page.evaluate(
                """() => {
                    const requestId = 'de305d54-75b4-431b-adb2-eb6b9e546014';
                    const message = '当前消息'.repeat(1024);
                    const statePackage = 's'.repeat(32 * 1024);
                    const credential = 'k'.repeat(4096);
                    const history = Array.from({length: 24}, (_, index) => ({
                        role: index % 2 ? 'assistant' : 'user',
                        content_blocks: [{type: 'speech', text: '历史消息'.repeat(500)}],
                    }));
                    const fitted = window.__projectSnowTest.fitPublicRequestPayload({
                        request_id: requestId,
                        credential,
                        character_id: '25b23cb64398',
                        message,
                        content_blocks: [{type: 'speech', text: message}],
                        recent_history: history,
                        history_summary: '摘要'.repeat(12000),
                        state_package: statePackage,
                    }, {
                        arrays: [{key: 'recent_history', minimum: 0}],
                        texts: ['history_summary'],
                    });
                    const summarize = window.__projectSnowTest.fitPublicRequestPayload({
                        request_id: requestId,
                        credential,
                        character_id: '25b23cb64398',
                        turns: history,
                        previous_summary: '旧摘要'.repeat(12000),
                        state_package: statePackage,
                    }, {
                        arrays: [{key: 'turns', minimum: 2}],
                        texts: ['previous_summary'],
                    });
                    return {
                        bytes: new TextEncoder().encode(JSON.stringify(fitted)).byteLength,
                        summarizeBytes: new TextEncoder().encode(JSON.stringify(summarize)).byteLength,
                        requestId: fitted.request_id,
                        messagePreserved: fitted.message === message,
                        statePreserved: fitted.state_package === statePackage,
                        credentialPreserved: fitted.credential === credential,
                        historyLength: fitted.recent_history.length,
                        summarizeTurns: summarize.turns.length,
                    };
                }"""
            )
            self.assertLessEqual(request_budget["bytes"], 63 * 1024)
            self.assertLessEqual(request_budget["summarizeBytes"], 63 * 1024)
            self.assertEqual(request_budget["requestId"], "de305d54-75b4-431b-adb2-eb6b9e546014")
            self.assertTrue(request_budget["messagePreserved"])
            self.assertTrue(request_budget["statePreserved"])
            self.assertTrue(request_budget["credentialPreserved"])
            self.assertLess(request_budget["historyLength"], 24)
            self.assertGreaterEqual(request_budget["summarizeTurns"], 2)
            database = page.evaluate(
                """async () => {
                    const request = indexedDB.open('project-snow-public');
                    const db = await new Promise((resolve, reject) => {
                        request.onsuccess = () => resolve(request.result);
                        request.onerror = () => reject(request.error);
                    });
                    const tx = db.transaction('messages', 'readonly');
                    return {
                        version: db.version,
                        stores: [...db.objectStoreNames],
                        indexes: [...tx.objectStore('messages').indexNames],
                    };
                }"""
            )
            self.assertEqual(database["version"], 4)
            self.assertIn("messages", database["stores"])
            self.assertIn("by_character_created", database["indexes"])
            self.assertIn("by_character_segment_created", database["indexes"])

            page.locator("#message-input").fill("这段主输入框草稿必须保留")
            page.locator("#open-movement-shortcuts").click()
            self.assertEqual(page.locator("#movement-options [role=radio]").count(), 15)
            page.locator('[data-movement-id="character_room"]').click()
            self.assertEqual(page.locator("#movement-invitation").input_value(), "要不要一起去你的房间？")
            page.locator('[data-movement-id="commercial_street"]').click()
            self.assertEqual(page.locator("#movement-invitation").input_value(), "现在一起去商业街吗？")
            self.assertEqual(page.locator('[data-movement-id="character_room"]').get_attribute("aria-checked"), "false")
            self.assertEqual(page.locator('[data-movement-id="commercial_street"]').get_attribute("aria-checked"), "true")
            self.assertEqual(page.locator("#movement-invitation").get_attribute("readonly"), "")
            self.assertEqual(page.locator("#message-input").input_value(), "这段主输入框草稿必须保留")
            self.assertTrue(page.locator("#send-movement-invitation").is_enabled())
            page.locator('[data-close-dialog="movement-dialog"]').last.click()

            page.locator("#open-info").click()
            self.assertEqual(page.locator("#info-panel").get_attribute("aria-hidden"), "false")
            self.assertTrue(page.locator(".chat-panel").evaluate("element => element.inert"))
            page.locator("#close-info").click()
            self.assertEqual(page.locator("#info-panel").get_attribute("aria-hidden"), "true")
            self.assertFalse(page.locator(".chat-panel").evaluate("element => element.inert"))

            page.locator("#open-global-feedback").click()
            self.assertTrue(page.locator("#feedback-include-context").is_checked())
            page.locator("#feedback-include-context").uncheck()
            self.assertTrue(page.locator("#feedback-context-preview").is_hidden())
            page.locator("#feedback-body").fill("不附带对话的测试反馈")
            page.locator("#feedback-form button[type=submit]").click()
            page.locator("#feedback-dialog").wait_for(state="hidden")
            payload = PublicFrontendHandler.feedback_payload or {}
            self.assertFalse(payload.get("include_conversation_context"))
            self.assertNotIn("user_message", payload)
            self.assertNotIn("assistant_answer", payload)
            self.assertNotIn("chat_request_id", payload)

            self._configure_model(page)
            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("停顿测试")
            page.evaluate("() => { window.__messageSentAt = performance.now(); }")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            page.wait_for_timeout(150)
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            page.wait_for_timeout(300)
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            page.locator("#timeline .typing-indicator").wait_for(state="visible")
            typing_elapsed = page.evaluate("() => performance.now() - window.__messageSentAt")
            self.assertGreaterEqual(typing_elapsed, 1750)
            self.assertLessEqual(typing_elapsed, 2900)
            bounds = page.evaluate(
                """() => {
                    const outer = document.querySelector('#timeline').getBoundingClientRect();
                    const inner = document.querySelector('#timeline .typing-indicator-bubble').getBoundingClientRect();
                    return {inside: inner.left >= outer.left && inner.right <= outer.right};
                }"""
            )
            self.assertTrue(bounds["inside"])
            self.assertTrue(page.locator("#stop-waiting").is_visible())
            overlap = page.evaluate(
                """() => {
                    const feedback = document.querySelector('#floating-feedback').getBoundingClientRect();
                    const stop = document.querySelector('#stop-waiting').getBoundingClientRect();
                    return !(feedback.right <= stop.left || stop.right <= feedback.left || feedback.bottom <= stop.top || stop.bottom <= feedback.top);
                }"""
            )
            self.assertFalse(overlap)
            PublicFrontendHandler.chat_stream_release.set()
            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible")
            browser.close()

    def test_network_recovery_reuses_request_id_without_duplicate_messages(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#message-input").fill("断流恢复测试")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible", timeout=8000)
            self.assertEqual(len(PublicFrontendHandler.chat_attempts), 1)
            self.assertEqual(next(iter(PublicFrontendHandler.chat_attempts.values())), 2)
            self.assertEqual(page.locator("#timeline .message.user").count(), 1)
            self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
            browser.close()

    def test_stop_waiting_cancels_reconnect_backoff(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#message-input").fill("取消重连测试")
            page.locator("#send-message").click()
            page.locator("#request-status").get_by_text("连接中断，正在恢复（1/3）").wait_for(state="visible")
            page.locator("#stop-waiting").click()
            page.locator("#request-status").get_by_text("已停止等待").wait_for(state="visible")
            page.wait_for_timeout(1300)
            self.assertEqual(sum(PublicFrontendHandler.chat_attempts.values()), 1)
            browser.close()

    def test_movement_invitation_requires_explicit_send_and_preserves_main_draft(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#message-input").fill("不要覆盖这段主草稿")
            page.locator("#open-movement-shortcuts").click()
            page.locator('[data-movement-id="commercial_street"]').click()
            self.assertEqual(page.locator("#movement-invitation").input_value(), "现在一起去商业街吗？")
            self.assertEqual(page.locator("#message-input").input_value(), "不要覆盖这段主草稿")
            page.locator("#send-movement-invitation").click()
            page.locator("#movement-dialog").wait_for(state="hidden")
            page.locator("#timeline").get_by_text("现在一起去商业街吗？").wait_for(state="visible")
            page.locator("#timeline").get_by_text("我先去商业街等你。").wait_for(state="visible")
            page.locator("#timeline .rendezvous-card").wait_for(state="visible")
            self.assertIn("她已先到商业街等你", page.locator("#timeline .rendezvous-card").inner_text())
            self.assertEqual(page.locator("#message-input").input_value(), "不要覆盖这段主草稿")
            feedback_overlap = page.evaluate(
                """() => {
                    const message = document.querySelector('#timeline .message:last-of-type').getBoundingClientRect();
                    const feedback = document.querySelector('#floating-feedback').getBoundingClientRect();
                    return !(message.right <= feedback.left || feedback.right <= message.left || message.bottom <= feedback.top || feedback.bottom <= message.top);
                }"""
            )
            self.assertFalse(feedback_overlap)
            sent = PublicFrontendHandler.chat_payloads[-1]
            self.assertEqual(sent.get("message"), "现在一起去商业街吗？")
            self.assertEqual(sent.get("movement_location_id"), "commercial_street")
            self.assertEqual(
                sent.get("content_blocks"),
                [{"type": "message", "text": "现在一起去商业街吗？"}],
            )
            page.locator("#message-input").fill("继续在通讯器里说一句")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("晚上好，分析员。").wait_for(state="visible")
            self.assertTrue(page.locator("#timeline .rendezvous-card").is_visible())
            requests_before_stay = len(PublicFrontendHandler.chat_payloads)
            page.locator('[data-rendezvous-stay]').click()
            self.assertEqual(page.locator("#timeline .rendezvous-card").count(), 0)
            page.wait_for_timeout(100)
            self.assertEqual(len(PublicFrontendHandler.chat_payloads), requests_before_stay)

            page.locator("#open-movement-shortcuts").click()
            page.locator('[data-movement-id="commercial_street"]').click()
            page.locator("#send-movement-invitation").click()
            page.locator("#timeline .rendezvous-card").wait_for(state="visible")
            PublicFrontendHandler.arrival_mode = "http_error"
            page.locator('[data-rendezvous-go]').click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
            self.assertEqual(page.locator("#timeline .rendezvous-card").count(), 0)
            page.locator("#system-banner").get_by_text(
                "位置切换已完成；到场反应暂未生成，你可以直接开始对话。"
            ).wait_for(state="visible")
            page.wait_for_function(
                "() => !document.querySelector('#send-message').disabled"
            )
            self.assertTrue(page.locator("#message-input").is_enabled())
            self.assertTrue(page.locator("#toggle-action").is_enabled())
            page.locator("#toggle-action").click()
            page.locator("#action-input").fill("向她挥了挥手")
            page.locator("#message-input").fill("我到了，我们继续聊吧。")
            page.locator("#send-message").click()
            page.locator("#stage-speech").get_by_text("晚上好，分析员。").wait_for(
                state="visible"
            )
            sent = PublicFrontendHandler.chat_payloads[-1]
            self.assertEqual(sent.get("communication_channel"), "in_person")
            self.assertEqual(
                sent.get("content_blocks"),
                [
                    {"type": "action", "text": "向她挥了挥手"},
                    {"type": "speech", "text": "我到了，我们继续聊吧。"},
                ],
            )
            transition_index = max(
                index
                for index, path in enumerate(PublicFrontendHandler.request_paths)
                if path == "/public/v1/presence/transition"
            )
            arrival_index = max(
                index
                for index, path in enumerate(PublicFrontendHandler.request_paths)
                if path == "/public/v1/presence/arrival"
            )
            self.assertLess(transition_index, arrival_index)
            browser.close()

    def test_failed_presence_transition_preserves_text_draft(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#message-input").fill("这段文字通讯草稿不能丢失")

            PublicFrontendHandler.transition_mode = "http_error"
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#system-banner").get_by_text(
                "本地场景状态仍然无效，请重新发送本条消息。"
            ).wait_for(state="visible")
            page.wait_for_function(
                "() => document.querySelector('#presence-arrival-loading').hidden"
            )

            self.assertTrue(page.locator("#text-surface").is_visible())
            self.assertEqual(
                page.locator("#message-input").input_value(),
                "这段文字通讯草稿不能丢失",
            )
            browser.close()

    def test_face_to_face_character_switch_recovers_from_empty_arrival_reply(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#stage-speech").get_by_text("你来了。").wait_for(
                state="visible"
            )
            page.locator("#presence-arrival-loading").wait_for(state="hidden")

            PublicFrontendHandler.arrival_mode = "terminal_error"
            page.locator('[data-character="9f5804761c56"]').click()
            page.locator("#stage-character-name", has_text="安卡希雅").wait_for(
                state="visible"
            )
            page.locator("#presence-dialog").wait_for(state="visible")
            page.locator("#confirm-presence-transition").click()
            page.locator("#presence-arrival-loading").wait_for(state="hidden")
            page.locator("#system-banner").get_by_text(
                "位置切换已完成；她暂时没有作出到场回应，你可以直接开始对话。"
            ).wait_for(state="visible")
            page.wait_for_function(
                "() => !document.querySelector('#send-message').disabled"
            )
            self.assertTrue(page.locator("#message-input").is_enabled())
            self.assertTrue(page.locator("#toggle-action").is_enabled())
            self.assertNotIn(
                "模型没有返回可用正文",
                page.locator("#system-banner").inner_text(),
            )

            page.locator("#toggle-action").click()
            page.locator("#action-input").fill("在她面前停下脚步")
            page.locator("#message-input").fill("现在可以听见我吗？")
            page.locator("#send-message").click()
            page.locator("#stage-speech").get_by_text("晚上好，分析员。").wait_for(
                state="visible"
            )
            sent = PublicFrontendHandler.chat_payloads[-1]
            self.assertEqual(sent.get("character_id"), "9f5804761c56")
            self.assertEqual(sent.get("communication_channel"), "in_person")
            self.assertEqual(
                sent.get("content_blocks"),
                [
                    {"type": "action", "text": "在她面前停下脚步"},
                    {"type": "speech", "text": "现在可以听见我吗？"},
                ],
            )
            browser.close()

    def test_rendezvous_terminal_arrival_error_keeps_in_person_composer_usable(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(reduced_motion="reduce")
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)

            page.locator("#open-movement-shortcuts").click()
            page.locator('[data-movement-id="commercial_street"]').click()
            page.locator("#send-movement-invitation").click()
            page.locator("#timeline .rendezvous-card").wait_for(state="visible")

            PublicFrontendHandler.arrival_mode = "terminal_error"
            page.locator('[data-rendezvous-go]').click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#presence-arrival-loading").wait_for(state="hidden")
            page.locator("#system-banner").get_by_text(
                "位置切换已完成；她暂时没有作出到场回应，你可以直接开始对话。"
            ).wait_for(state="visible")

            self.assertNotIn(
                "模型没有返回可用正文",
                page.locator("#system-banner").inner_text(),
            )
            self.assertEqual(page.locator("#timeline .rendezvous-card").count(), 0)
            page.wait_for_function(
                "() => !document.querySelector('#send-message').disabled"
            )
            self.assertTrue(page.locator("#message-input").is_enabled())
            self.assertTrue(page.locator("#toggle-action").is_enabled())

            page.locator("#message-input").fill("我到了，我们继续聊吧。")
            page.locator("#send-message").click()
            page.locator("#stage-speech").get_by_text("晚上好，分析员。").wait_for(
                state="visible"
            )
            sent = PublicFrontendHandler.chat_payloads[-1]
            self.assertEqual(sent.get("communication_channel"), "in_person")
            self.assertEqual(
                sent.get("content_blocks"),
                [{"type": "speech", "text": "我到了，我们继续聊吧。"}],
            )
            browser.close()

    def test_reload_recovers_persisted_request_id_without_a_second_generation(self) -> None:
        fixed_request_id = "de305d54-75b4-431b-adb2-eb6b9e546014"
        current_local_day = datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        attempts: list[dict[str, object]] = []
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.evaluate(
                """async ({requestId, localDayKey}) => {
                    const request = indexedDB.open('project-snow-public');
                    const db = await new Promise((resolve, reject) => {
                        request.onsuccess = () => resolve(request.result);
                        request.onerror = () => reject(request.error);
                    });
                    const characterId = '25b23cb64398';
                    const segmentId = 'persisted-recovery-segment';
                    const snapshot = {
                        request_id: requestId,
                        provider: 'openai',
                        model: 'gpt-e2e',
                        character_id: characterId,
                        message: '刷新恢复测试',
                        communication_channel: 'text',
                        content_blocks: [{type: 'message', text: '刷新恢复测试'}],
                        recent_history: [],
                        history_summary: '',
                        state_package: '',
                        continuity_decision: '',
                        local_day_key: localDayKey,
                    };
                    const tx = db.transaction(['threads', 'messages'], 'readwrite');
                    tx.objectStore('threads').put({
                        characterId,
                        channel: 'text',
                        turnCount: 0,
                        conversationSegmentId: segmentId,
                        localDayKey,
                        messageCount: 1,
                        lastActiveAt: Date.now(),
                    });
                    tx.objectStore('messages').put({
                        id: 'persisted-recovery-user',
                        characterId,
                        role: 'user',
                        content: '刷新恢复测试',
                        contentBlocks: [{type: 'message', text: '刷新恢复测试'}],
                        communicationChannel: 'text',
                        createdAt: Date.now(),
                        status: 'pending',
                        requestId,
                        requestSnapshot: snapshot,
                        conversationSegmentId: segmentId,
                    });
                    await new Promise((resolve, reject) => {
                        tx.oncomplete = resolve;
                        tx.onerror = () => reject(tx.error);
                        tx.onabort = () => reject(tx.error);
                    });
                    db.close();
                }""",
                {"requestId": fixed_request_id, "localDayKey": current_local_day},
            )

            def fulfill_chat(route) -> None:
                payload = json.loads(route.request.post_data or "{}")
                attempts.append(payload)
                route.fulfill(
                    status=200,
                    content_type="text/event-stream; charset=utf-8",
                    body="".join(
                        (
                            'event: delta\ndata: {"text":"恢复了同一请求。"}\n\n',
                            "event: done\ndata: "
                            + json.dumps(
                                {
                                    "truncated": False,
                                    "communication_channel": "text",
                                    "content_blocks": [
                                        {"type": "message", "text": "恢复了同一请求。"}
                                    ],
                                    "usage": {"provider_calls": 1},
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n",
                        )
                    ),
                )

            page.route("**/public/v1/chat/stream", fulfill_chat)
            page.reload(wait_until="networkidle")
            retry = page.locator('[data-retry-message="persisted-recovery-user"]')
            retry.wait_for(state="visible")
            self.assertIn("生成失败", page.locator('[data-message-id="persisted-recovery-user"] .meta').inner_text())
            retry.click()
            page.locator("#timeline").get_by_text("恢复了同一请求。").wait_for(state="visible")

            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["request_id"], fixed_request_id)
            self.assertEqual(page.locator("#timeline .message.user").count(), 1)
            self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
            persisted = page.evaluate(
                """async () => {
                    const request = indexedDB.open('project-snow-public');
                    const db = await new Promise((resolve, reject) => {
                        request.onsuccess = () => resolve(request.result);
                        request.onerror = () => reject(request.error);
                    });
                    const tx = db.transaction('messages', 'readonly');
                    const stored = tx.objectStore('messages').get('persisted-recovery-user');
                    return await new Promise((resolve, reject) => {
                        tx.oncomplete = () => resolve(stored.result || null);
                        tx.onerror = () => reject(tx.error);
                    });
                }"""
            )
            self.assertEqual(persisted["status"], "sent")
            self.assertIsNone(persisted.get("requestSnapshot"))
            browser.close()

    def test_invalid_world_state_is_cleared_and_chat_retries_once_with_new_id(self) -> None:
        attempts: list[dict[str, object]] = []
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()

            def fulfill_chat(route) -> None:
                payload = json.loads(route.request.post_data or "{}")
                attempts.append(payload)
                if len(attempts) == 1:
                    body = (
                        "event: error\n"
                        'data: {"code":"state_subject_mismatch"}\n\n'
                    )
                else:
                    body = "".join(
                        (
                            'event: delta\ndata: {"text":"状态已重新建立。"}\n\n',
                            "event: done\ndata: "
                            + json.dumps(
                                {
                                    "truncated": False,
                                    "communication_channel": "text",
                                    "content_blocks": [
                                        {"type": "message", "text": "状态已重新建立。"}
                                    ],
                                    "usage": {"provider_calls": 1},
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n",
                        )
                    )
                route.fulfill(
                    status=200,
                    content_type="text/event-stream; charset=utf-8",
                    body=body,
                )

            page.route("**/public/v1/chat/stream", fulfill_chat)
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            page.locator("#go-in-person-label", has_text="观景区").wait_for(state="visible")
            page.evaluate(
                """async () => {
                    const request = indexedDB.open('project-snow-public');
                    const db = await new Promise((resolve, reject) => {
                        request.onsuccess = () => resolve(request.result);
                        request.onerror = () => reject(request.error);
                    });
                    const tx = db.transaction('threads', 'readwrite');
                    tx.objectStore('threads').put({
                        characterId: 'legacy-v3-thread',
                        statePackage: 'fixture-state.signature',
                        legacyStatePackage: 'fixture-state.signature',
                    });
                    await new Promise((resolve, reject) => {
                        tx.oncomplete = resolve;
                        tx.onerror = () => reject(tx.error);
                    });
                }"""
            )
            self._configure_model(page)
            page.locator("#message-input").fill("重新建立状态")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("状态已重新建立。").wait_for(
                state="visible"
            )

            self.assertEqual(len(attempts), 2)
            self.assertNotEqual(attempts[0]["request_id"], attempts[1]["request_id"])
            self.assertTrue(attempts[0]["state_package"])
            self.assertEqual(attempts[1]["state_package"], "")
            persisted_state = page.evaluate(
                """async () => {
                    const request = indexedDB.open('project-snow-public');
                    const db = await new Promise((resolve, reject) => {
                        request.onsuccess = () => resolve(request.result);
                        request.onerror = () => reject(request.error);
                    });
                    const tx = db.transaction(['app_state', 'threads'], 'readonly');
                    const world = tx.objectStore('app_state').get('world');
                    const legacy = tx.objectStore('threads').get('legacy-v3-thread');
                    return await new Promise((resolve, reject) => {
                        tx.oncomplete = () => resolve({
                            world: world.result || null,
                            legacy: legacy.result || null,
                        });
                        tx.onerror = () => reject(tx.error);
                    });
                }"""
            )
            self.assertIsNone(persisted_state["world"])
            self.assertNotIn("statePackage", persisted_state["legacy"])
            self.assertNotIn("legacyStatePackage", persisted_state["legacy"])
            self.assertEqual(page.locator("#timeline .message.user").count(), 1)
            self.assertEqual(page.locator("#timeline .message.assistant").count(), 1)
            browser.close()

    def test_presentation_queue_segments_text_but_uses_one_face_to_face_surface(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#message-input").fill("多段测试")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("第一段").wait_for(state="visible")
            page.locator("#timeline .typing-indicator").wait_for(state="visible")
            page.locator("#timeline").get_by_text("第二段").wait_for(state="visible")
            page.locator("#timeline .typing-indicator").wait_for(state="visible")
            page.locator("#timeline").get_by_text("收到").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            sticker_image = page.locator("#timeline .content-sticker img")
            self.assertEqual(sticker_image.get_attribute("data-animated"), "true")
            self.assertTrue(
                (sticker_image.get_attribute("data-animated-src") or "").endswith(
                    "#display"
                )
            )
            self.assertFalse(
                (sticker_image.get_attribute("data-static-src") or "").endswith(
                    "#display"
                )
            )
            page.locator("#message-input").fill("句界测试")
            page.locator("#send-message").click()
            page.locator("#timeline").get_by_text("第一句自然回应。").wait_for(state="visible")
            first_segment_at = page.evaluate("() => performance.now()")
            page.locator("#timeline .typing-indicator").wait_for(state="visible")
            page.locator("#timeline").get_by_text("第二句换个角度继续。").wait_for(state="visible")
            second_segment_at = page.evaluate("() => performance.now()")
            self.assertGreaterEqual(second_segment_at - first_segment_at, 2400)
            self.assertLessEqual(second_segment_at - first_segment_at, 4600)
            page.locator("#timeline .typing-indicator").wait_for(state="visible")
            page.locator("#timeline").get_by_text("第三句收住话题。").wait_for(state="visible")
            self.assertEqual(page.locator("#timeline .typing-indicator").count(), 0)
            sentence_message = page.locator("#timeline .message.assistant").filter(has_text="第一句自然回应。")
            self.assertEqual(sentence_message.locator(".content-message").count(), 3)
            sentence_message_id = sentence_message.get_attribute("data-message-id")
            page.reload(wait_until="networkidle")
            persisted_message = page.locator(f'[data-message-id="{sentence_message_id}"]')
            persisted_message.wait_for(state="visible")
            self.assertEqual(persisted_message.locator(".content-message").count(), 3)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            page.locator("#stage-speech").get_by_text("你来了。").wait_for(
                state="visible"
            )
            page.locator("#presence-arrival-loading").wait_for(
                state="hidden", timeout=7000
            )
            page.wait_for_function(
                "() => !document.querySelector('#send-message').disabled"
            )
            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("多段测试")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            page.locator("#stage-speech .typing-indicator").wait_for(state="visible")
            page.evaluate(
                """({firstSentence, actionText}) => {
                    const speech = document.querySelector('#stage-speech');
                    const narration = document.querySelector('#stage-narration');
                    const surface = document.querySelector('#in-person-surface');
                    const trace = {
                        actionAt: 0,
                        firstCharacterAt: 0,
                        boundaryAt: 0,
                        resumedAt: 0,
                        lastRevealedLength: 0,
                        sawTextRegression: false,
                        sawTypingAfterFirst: false,
                        sawTranscriptBubble: false,
                    };
                    const inspect = () => {
                        const text = speech.textContent;
                        if (narration.textContent === actionText && !trace.actionAt) {
                            trace.actionAt = performance.now();
                        }
                        if (text && !speech.querySelector('.typing-indicator') && !trace.firstCharacterAt) {
                            trace.firstCharacterAt = performance.now();
                        }
                        if (text === firstSentence && !trace.boundaryAt) {
                            trace.boundaryAt = performance.now();
                        }
                        if (trace.boundaryAt && text.length > firstSentence.length && !trace.resumedAt) {
                            trace.resumedAt = performance.now();
                        }
                        if (trace.boundaryAt && text.length < trace.lastRevealedLength) {
                            trace.sawTextRegression = true;
                        }
                        if (trace.boundaryAt) trace.lastRevealedLength = Math.max(trace.lastRevealedLength, text.length);
                        if (trace.boundaryAt && speech.querySelector('.typing-indicator')) {
                            trace.sawTypingAfterFirst = true;
                        }
                        if (surface.querySelector('.content-speech')) trace.sawTranscriptBubble = true;
                    };
                    window.__facePresentationTrace = trace;
                    window.__facePresentationObserver = new MutationObserver(inspect);
                    window.__facePresentationObserver.observe(surface, {
                        childList: true,
                        characterData: true,
                        subtree: true,
                    });
                    inspect();
                }""",
                {
                    "firstSentence": "第一句。",
                    "actionText": "凯茜娅抬起手。\n凯茜娅轻轻一笑。",
                },
            )
            PublicFrontendHandler.chat_stream_release.set()
            page.wait_for_function(
                "() => document.querySelector('#stage-narration').textContent === '凯茜娅抬起手。\\n凯茜娅轻轻一笑。'"
            )
            page.wait_for_function(
                "() => document.querySelector('#stage-speech').textContent === '第一句。\\n第二句。'"
            )
            page.evaluate("() => window.__facePresentationObserver?.disconnect()")
            trace = page.evaluate("() => window.__facePresentationTrace")
            self.assertGreater(trace["actionAt"], 0, trace)
            self.assertGreater(trace["firstCharacterAt"], 0, trace)
            self.assertLessEqual(trace["actionAt"], trace["firstCharacterAt"], trace)
            self.assertGreater(trace["boundaryAt"], 0, trace)
            self.assertGreaterEqual(trace["resumedAt"] - trace["boundaryAt"], 250, trace)
            self.assertFalse(trace["sawTextRegression"], trace)
            self.assertFalse(trace["sawTypingAfterFirst"], trace)
            self.assertFalse(trace["sawTranscriptBubble"], trace)
            self.assertEqual(page.locator("#stage-speech").count(), 1)
            self.assertEqual(page.locator("#stage-speech .typing-indicator").count(), 0)
            self.assertEqual(page.locator("#in-person-surface .content-speech").count(), 0)
            self.assertEqual(page.locator("#stage-speech").get_attribute("aria-busy"), "false")
            self.assertEqual(
                page.locator("#stage-narration").inner_text(),
                "凯茜娅抬起手。\n凯茜娅轻轻一笑。",
            )
            page.locator("#message-input").fill("边界保真测试")
            page.locator("#send-message").click()
            page.wait_for_function(
                "expected => document.querySelector('#stage-speech').textContent === expected",
                arg=BOUNDARY_IN_PERSON_REPLY,
            )
            self.assertEqual(page.locator("#stage-speech").inner_text(), BOUNDARY_IN_PERSON_REPLY)
            self.assertEqual(page.locator("#stage-narration").inner_text(), "她停下来听雪。")
            browser.close()

    def test_long_face_to_face_reply_is_bounded_and_never_splits_graphemes(self) -> None:
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context()
            context.add_init_script(
                """(() => {
                    Object.defineProperty(Intl, "Segmenter", {
                        value: undefined,
                        configurable: true,
                    });
                })();"""
            )
            page = context.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            self.assertEqual(page.evaluate("() => typeof Intl.Segmenter"), "undefined")
            page.locator("#accept-experience-notice").click()
            self._configure_model(page)
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#stage-speech").get_by_text("你来了。").wait_for(state="visible")
            page.locator("#presence-arrival-loading").wait_for(state="hidden", timeout=7000)
            page.wait_for_function("() => !document.querySelector('#send-message').disabled")

            PublicFrontendHandler.chat_stream_started = threading.Event()
            PublicFrontendHandler.chat_stream_release = threading.Event()
            page.locator("#message-input").fill("长台词字素测试")
            page.locator("#send-message").click()
            self.assertTrue(PublicFrontendHandler.chat_stream_started.wait(timeout=5))
            page.locator("#stage-speech .typing-indicator").wait_for(state="visible")
            page.evaluate(
                """({fullText, graphemes}) => {
                    const speech = document.querySelector("#stage-speech");
                    const forbidden = new Set();
                    for (const grapheme of graphemes) {
                        const start = fullText.indexOf(grapheme);
                        const points = Array.from(grapheme);
                        for (let count = 1; count < points.length; count += 1) {
                            forbidden.add(fullText.slice(0, start) + points.slice(0, count).join(""));
                        }
                    }
                    const trace = {
                        startedAt: 0,
                        finishedAt: 0,
                        mutationCount: 0,
                        lastLength: 0,
                        sawRegression: false,
                        forbiddenHits: [],
                    };
                    const inspect = () => {
                        const text = speech.textContent;
                        const revealing = speech.classList.contains("is-revealing");
                        if (revealing && !trace.startedAt) trace.startedAt = performance.now();
                        if (!trace.startedAt) return;
                        trace.mutationCount += 1;
                        if (text.length < trace.lastLength) trace.sawRegression = true;
                        trace.lastLength = Math.max(trace.lastLength, text.length);
                        if (forbidden.has(text)) trace.forbiddenHits.push(text.slice(-24));
                        if (!revealing && text === fullText && !trace.finishedAt) {
                            trace.finishedAt = performance.now();
                        }
                    };
                    window.__longGraphemeTrace = trace;
                    window.__longGraphemeObserver = new MutationObserver(inspect);
                    window.__longGraphemeObserver.observe(speech, {
                        childList: true,
                        characterData: true,
                        subtree: true,
                    });
                    inspect();
                }""",
                {
                    "fullText": LONG_IN_PERSON_REPLY,
                    "graphemes": [
                        "👨‍👩‍👧‍👦",
                        "👍🏽",
                        "🇭🇰",
                        "e\u0301",
                        "✈️",
                    ],
                },
            )
            released_at = page.evaluate("() => performance.now()")
            PublicFrontendHandler.chat_stream_release.set()
            page.wait_for_function(
                "() => !document.querySelector('#send-message').disabled",
                timeout=12000,
            )
            composer_elapsed = page.evaluate("started => performance.now() - started", released_at)
            page.wait_for_function(
                "expected => document.querySelector('#stage-speech').textContent === expected",
                arg=LONG_IN_PERSON_REPLY,
            )
            page.evaluate("() => window.__longGraphemeObserver?.disconnect()")
            trace = page.evaluate("() => window.__longGraphemeTrace")

            self.assertGreater(trace["startedAt"], 0, trace)
            self.assertGreater(trace["finishedAt"], trace["startedAt"], trace)
            self.assertLess(trace["finishedAt"] - trace["startedAt"], 6500, trace)
            self.assertLess(composer_elapsed, 8500)
            self.assertLess(trace["mutationCount"], 200, trace)
            self.assertFalse(trace["sawRegression"], trace)
            self.assertEqual(trace["forbiddenHits"], [], trace)
            self.assertEqual(page.locator("#stage-speech").inner_text(), LONG_IN_PERSON_REPLY)
            self.assertEqual(page.locator("#stage-speech").get_attribute("aria-busy"), "false")
            self.assertFalse(page.locator("#stage-speech").evaluate("node => node.classList.contains('is-revealing')"))
            context.close()
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
            browser = _launch_browser(playwright)
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
            panel = page.locator("#contact-panel")
            page.wait_for_function(
                "() => document.querySelector('#contact-panel')?.classList.contains('open')"
            )
            page.wait_for_function(
                """() => {
                    const rect = document.querySelector('#contact-panel')?.getBoundingClientRect();
                    return Boolean(rect && rect.left >= -1 && rect.right <= innerWidth + 1);
                }"""
            )
            self.assertEqual(page.locator("#open-contacts").get_attribute("aria-expanded"), "true")
            panel_bounds = panel.evaluate(
                """element => {
                    const rect = element.getBoundingClientRect();
                    return {left: rect.left, right: rect.right, viewport: innerWidth};
                }"""
            )
            self.assertGreaterEqual(panel_bounds["left"], -1)
            self.assertLessEqual(panel_bounds["right"], panel_bounds["viewport"] + 1)
            contacts = page.locator("#character-list")
            contacts.wait_for(state="visible")
            before = contacts.evaluate(
                "(element) => ({scrollHeight: element.scrollHeight, clientHeight: element.clientHeight})"
            )
            after = contacts.evaluate(
                "element => { element.scrollTop = element.scrollHeight; return element.scrollTop; }"
            )
            self.assertGreater(before["scrollHeight"], before["clientHeight"])
            self.assertGreater(after, 0)
            page.locator('[data-character="fixture-21"]').click()
            self.assertIsNotNone(page.locator("#toggle-action").get_attribute("hidden"))
            self.assertIsNone(page.locator("#toggle-sticker").get_attribute("hidden"))
            page.locator("#toggle-sticker").click()
            page.locator('[data-sticker-id="fixture-sticker"]').click()
            self.assertTrue(page.locator("#selected-sticker").is_visible())
            self.assertIn("发送时会单独作为一条消息", page.locator("#selected-sticker").inner_text())
            page.set_viewport_size({"width": 320, "height": 720})
            self._assert_no_horizontal_overflow(page)
            self._assert_visible_controls_do_not_overlap(page, ".composer-row > :not([hidden])")
            composer_text_fits = page.locator(".composer-row > :not([hidden])").evaluate_all(
                """elements => elements.every(element => element.scrollWidth <= element.clientWidth + 1)"""
            )
            self.assertTrue(composer_text_fits)
            page.locator("#open-contacts").click()
            self._configure_model(page)
            page.locator("#close-contacts").click()
            page.locator("#go-in-person").click()
            page.locator("#confirm-presence-transition").click()
            page.locator("#in-person-surface").wait_for(state="visible")
            self.assertEqual(page.locator("#stage-message-feedback").count(), 0)
            self._assert_no_horizontal_overflow(page)
            self._assert_visible_controls_do_not_overlap(
                page,
                ".scene-hud-actions > button:not([hidden]), .scene-hud-actions > details:not([hidden])",
            )
            page.locator("#stage-menu > summary").click()
            self.assertTrue(page.locator("#stage-open-transcript").is_visible())
            self.assertTrue(page.locator("#stage-toggle-ui").is_visible())
            browser.close()
