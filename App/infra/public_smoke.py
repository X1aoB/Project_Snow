from __future__ import annotations

import argparse
import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PUBLIC_HOST = "snow.xiaob.dev"
COMMON_JSON_CHECKS = (
    ("/public/v1/health/live", "status", "ok"),
    ("/public/v1/config", "history_policy", "browser_indexeddb_plaintext"),
    ("/public/v1/characters", "count", 22),
)
INTERNAL_ONLY_JSON_CHECKS = (
    ("/public/v1/health/ready", "status", "ok"),
    ("/public/v1/health/full", "status", "ok"),
)


class SmokeFailure(RuntimeError):
    pass


def _read_json(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def get(base: str, path: str, *, host: str | None) -> tuple[int, dict[str, Any]]:
    headers = {"Host": host} if host else {}
    request = Request(base + path, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, _read_json(response.read())
    except HTTPError as exc:
        return exc.code, _read_json(exc.read())


def run(
    *,
    base: str,
    mode: str,
    public_host: str = PUBLIC_HOST,
    allow_private_health: bool = False,
) -> None:
    base = base.rstrip("/")
    if mode not in {"internal", "public"}:
        raise ValueError("smoke mode must be internal or public")
    if allow_private_health and mode != "public":
        raise ValueError("private-health compatibility is valid only for public smoke")
    # A public smoke must exercise the same virtual host as the real domain.
    # It intentionally never requests ready/full, which are private endpoints.
    host = public_host if mode == "public" else None
    checks = COMMON_JSON_CHECKS + (
        INTERNAL_ONLY_JSON_CHECKS if mode == "internal" else ()
    )
    for path, field, expected in checks:
        status, payload = get(base, path, host=host)
        if status != 200 or payload.get(field) != expected:
            raise SmokeFailure(
                f"{mode} smoke failed for {path}: status={status}, {field}={payload.get(field)!r}"
            )
    if mode == "public" and not allow_private_health:
        for path, _field, _expected in INTERNAL_ONLY_JSON_CHECKS:
            status, _ = get(base, path, host=host)
            if status != 404:
                raise SmokeFailure(
                    f"public smoke exposed private health endpoint {path}: status={status}"
                )
    status, _ = get(base, "/api/v1/mvp/bootstrap", host=host)
    if status != 404:
        raise SmokeFailure(f"private API boundary returned {status}, expected 404")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="http://caddy:8080")
    parser.add_argument("--mode", choices=("internal", "public"), default="internal")
    parser.add_argument("--public-host", default=PUBLIC_HOST)
    parser.add_argument(
        "--allow-private-health",
        action="store_true",
        help="legacy rollback only: do not require ready/full to be hidden",
    )
    args = parser.parse_args()
    run(
        base=args.base,
        mode=args.mode,
        public_host=args.public_host,
        allow_private_health=args.allow_private_health,
    )
    scope = (
        "live/config/characters"
        if args.mode == "public"
        else "live/ready/full/config/characters"
    )
    health_policy = (
        "legacy private-health compatibility"
        if args.allow_private_health
        else "private health policy"
    )
    print(
        f"Project Snow {args.mode} smoke passed: {scope}, {health_policy}, "
        "and private API boundary."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
