"""Serve the static evidence workspace and proxy same-origin API requests.

This development-only server mirrors the Nginx routing in infra/nginx.conf so
browser clients never need to expose the API on a separate origin.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import os
from pathlib import Path
from urllib.parse import urlsplit


APP_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceHandler(http.server.SimpleHTTPRequestHandler):
    api_host = os.getenv("LOCAL_API_HOST", "127.0.0.1")
    api_port = int(os.getenv("LOCAL_API_PORT", "8000"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_ROOT / "frontend"), **kwargs)

    def end_headers(self) -> None:  # noqa: D401
        """Prevent stale HTML/JS/CSS pairs during local development."""

        if self.path != "/health" and not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
        super().end_headers()

    def _proxy(self) -> None:
        split = urlsplit(self.path)
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else None
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {"host", "connection"}}
        connection = http.client.HTTPConnection(self.api_host, self.api_port, timeout=120)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            try:
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "transfer-encoding"}:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # Closing or reloading a browser tab can cancel an otherwise
                # successful proxied response. This is normal client behavior.
                return
        finally:
            connection.close()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health" or self.path.startswith("/api/"):
            self._proxy()
            return
        route = urlsplit(self.path).path.rstrip("/")
        if route in {"/immersive", "/assistant"}:
            original = self.path
            self.path = "/index.html"
            try:
                super().do_GET()
            finally:
                self.path = original
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._proxy()
            return
        self.send_error(405, "Only API paths accept POST")

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path.startswith("/api/"):
            self._proxy()
            return
        self.send_error(405, "Only API paths accept DELETE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), WorkspaceHandler)
    print(f"Project Snow chat client: http://127.0.0.1:{args.port}/")
    print(f"Project Snow immersive client: http://127.0.0.1:{args.port}/immersive/")
    print(f"Project Snow assistant client: http://127.0.0.1:{args.port}/assistant/")
    print(f"Project Snow evidence workspace: http://127.0.0.1:{args.port}/workspace/")
    server.serve_forever()


if __name__ == "__main__":
    main()
