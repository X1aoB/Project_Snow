from __future__ import annotations

import json
import sys
from urllib.error import HTTPError
from urllib.request import urlopen


base = (sys.argv[1] if len(sys.argv) > 1 else "http://caddy:8080").rstrip("/")


def get(path: str) -> tuple[int, dict]:
    try:
        with urlopen(base + path, timeout=15) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


assert get("/public/v1/health/live")[1].get("status") == "ok"
assert get("/public/v1/health/ready")[1].get("status") == "ok"
assert get("/public/v1/health/full")[1].get("status") == "ok"
assert get("/public/v1/config")[1].get("history_policy") == "browser_indexeddb_plaintext"
assert get("/public/v1/characters")[1].get("count") == 22
assert get("/api/v1/mvp/bootstrap")[0] == 404
print("Public live, ready, config, characters and internal-boundary checks passed.")
