"""Loopback workspace boundary and bounded upload reader."""

from __future__ import annotations

import asyncio
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


async def bounded_body(request: Request, limit: int, timeout: float = 15) -> bytes:
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(timeout):
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail="request_too_large")
                chunks.append(chunk)
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail="request_read_timeout") from exc
    body = b"".join(chunks)
    request._body = body
    return body


def install_local_boundary(app, allowed_origins: list[str], *, legacy_enabled: bool) -> None:
    origins = frozenset(origin.rstrip("/") for origin in allowed_origins) | {
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    }
    hosts = {"127.0.0.1", "localhost", "::1"}
    hosts.update(urlsplit(origin).hostname for origin in origins)
    token = secrets.token_urlsafe(32)
    legacy_prefixes = ("/api/v1/agent", "/api/v1/connectors")

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # TestClient is not a network peer. Production ASGI clients have an IP.
        test_client = request.client is not None and request.client.host == "testclient"
        host = (request.url.hostname or "").lower().rstrip(".")
        if host not in hosts and not (test_client and host == "testserver"):
            return JSONResponse(status_code=400, content={"detail": "host_rejected"})
        origin = request.headers.get("origin", "").rstrip("/")
        if origin and origin not in origins:
            return JSONResponse(status_code=403, content={"detail": "origin_rejected"})
        if not legacy_enabled and request.url.path.startswith(legacy_prefixes):
            return JSONResponse(status_code=404, content={"detail": "legacy_disabled"})
        write = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        if write and not test_client:
            supplied = request.cookies.get("snow_local_session", "")
            if not origin or not hmac.compare_digest(supplied, token):
                return JSONResponse(status_code=403, content={"detail": "local_session_required"})
        response = await call_next(request)
        if request.method == "GET" and not test_client:
            response.set_cookie("snow_local_session", token, httponly=True, samesite="strict", path="/api/")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
