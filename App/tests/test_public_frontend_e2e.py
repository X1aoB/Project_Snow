from __future__ import annotations

from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from unittest import TestCase, skipUnless
from urllib.parse import urlparse

RUN_PUBLIC_E2E = os.getenv("RUN_PUBLIC_E2E") == "1"
if RUN_PUBLIC_E2E:
    from playwright.sync_api import sync_playwright
else:
    sync_playwright = None


APP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = APP_ROOT / "public_frontend"


class PublicFrontendHandler(BaseHTTPRequestHandler):
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
                    "count": 1,
                    "characters": [
                        {
                            "character_id": "25b23cb64398",
                            "display_name": "凯茜娅",
                            "aliases": ["凯茜娅", "凯西娅"],
                            "avatar": None,
                            "license": "fixture",
                        }
                    ],
                }
            )
            return
        assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css"}
        filename = assets.get(path)
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

    def test_model_discovery_keeps_the_issued_credential(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.goto(self.base_url, wait_until="networkidle")
            self.assertEqual(page.locator("#provider-select option").count(), 1)
            page.locator("#api-key").fill("sk-e2e-only-not-real")
            for checkbox in ("#notice-transit", "#notice-cost", "#notice-history"):
                page.locator(checkbox).check()
            page.locator("#discover-models").click()
            page.locator("#discovered-models").wait_for(state="visible")
            page.locator("#discovered-models").select_option("gpt-e2e")
            self.assertTrue(page.locator("#credential-status").is_visible())
            self.assertEqual(page.locator("#api-key").input_value(), "")
            page.locator("#byok-form button[type=submit]").click()
            page.locator("#chat-view").wait_for(state="visible")
            page.locator("#character-list").get_by_text("凯茜娅").wait_for(
                state="visible"
            )
            self.assertIn("凯茜娅", page.locator("#character-list").inner_text())
            browser.close()
