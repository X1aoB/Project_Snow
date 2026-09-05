"""Probe public liveness without confusing edge errors with invalid JSON."""
from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_URL = "https://snow.xiaob.dev/public/v1/health/live"
RETRYABLE_STATUS = {408, 429} | set(range(500, 600))
MAX_BODY_BYTES = 65536


class HealthCheckFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def response_details(status: int, headers) -> str:
    details = [f"HTTP {status}"]
    for name in ("server", "content-type", "cf-ray", "cf-mitigated"):
        if value := headers.get(name):
            details.append(f"{name}={str(value)[:200]}")
    if headers.get("cf-mitigated") == "challenge":
        details.append("Cloudflare challenged the health probe before it reached the application")
    return "; ".join(details)


def probe(opener, request: Request) -> str:
    try:
        with opener.open(request, timeout=20) as response:
            status, headers = response.status, response.headers
            body = response.read(MAX_BODY_BYTES + 1)
    except HTTPError as error:
        status, headers, body = error.code, error.headers, b""
        error.close()
    except (URLError, TimeoutError, OSError) as error:
        raise HealthCheckFailure(
            f"Production health request failed: {error}", retryable=True
        ) from error
    if status != 200:
        raise HealthCheckFailure(
            "Production health failed: " + response_details(status, headers),
            retryable=status in RETRYABLE_STATUS,
        )
    if len(body) > MAX_BODY_BYTES:
        raise HealthCheckFailure("Production health response is oversized")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HealthCheckFailure(
            "Production health returned invalid JSON; " + response_details(status, headers)
        ) from error
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise HealthCheckFailure("Production health did not report status=ok")
    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        raise HealthCheckFailure("Production health did not report a version")
    return version


def check(url: str = DEFAULT_URL, *, attempts: int = 4) -> str:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    opener = build_opener(NoRedirect())
    request = Request(url, headers={
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "Project-Snow-Production-Health/1.0",
    })
    for attempt in range(attempts):
        try:
            return probe(opener, request)
        except HealthCheckFailure as error:
            if not error.retryable or attempt + 1 == attempts:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default=DEFAULT_URL)
    args = parser.parse_args()
    try:
        version = check(args.url)
    except HealthCheckFailure as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Production health passed: status=ok, version={version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
