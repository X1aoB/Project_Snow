"""Minimal stdio MCP bridge for the local Project Snow Persona Gateway."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import keyring
except ImportError:  # pragma: no cover - actionable error is returned to Codex
    keyring = None


SERVER_NAME = "snow-persona"
SERVER_VERSION = "0.5.0"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
KEYRING_SERVICE = "ProjectSnow"
TOKEN_REFERENCE = "persona-codex-current-token"


TOOLS = [
    {
        "name": "snow_get_configuration",
        "description": "Read the active Snow pairing and default character. Does not return the token.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "snow_get_persona_snapshot",
        "description": "Get a read-only, versioned character persona and relationship projection for one Codex task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "character_id": {
                    "type": "string",
                    "description": "Canonical Snow character ID or exact display name.",
                }
            },
            "required": ["character_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "snow_search_knowledge",
        "description": "Search public Project Snow character/story knowledge with canonical citations. Never searches private chats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "string"},
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8, "default": 6},
            },
            "required": ["character_id", "query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "snow_get_relationship",
        "description": "Read the structured relationship and preferred address for a Snow character.",
        "inputSchema": {
            "type": "object",
            "properties": {"character_id": {"type": "string"}},
            "required": ["character_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
]


def _token() -> str:
    explicit = str(os.getenv("SNOW_PERSONA_TOKEN") or "").strip()
    if explicit:
        return explicit
    if keyring is None:
        raise RuntimeError(
            "未安装 keyring，无法读取 Project Snow 的本机配对令牌；请安装项目 requirements 后重试。"
        )
    return str(keyring.get_password(KEYRING_SERVICE, TOKEN_REFERENCE) or "").strip()


def _get(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _token()
    if not token:
        raise RuntimeError(
            "尚未配对 Project Snow。请启动本地服务，在 /assistant/ 管理中心选择默认角色并点击“配对 Codex”。"
        )
    base_url = str(os.getenv("SNOW_PERSONA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    url = base_url + path
    if query:
        url += "?" + urlencode(query)
    request = Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Project Snow Gateway 返回 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            "无法连接 Project Snow 本地 API（默认 http://127.0.0.1:8000）；请先启动服务。"
        ) from exc


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "snow_get_configuration":
        payload = _get("/api/v1/persona/pairing")
    elif name == "snow_get_persona_snapshot":
        payload = _get(
            "/api/v1/persona/snapshot/" + str(arguments.get("character_id") or "").strip()
        )
    elif name == "snow_search_knowledge":
        payload = _get(
            "/api/v1/knowledge/search",
            {
                "character_id": str(arguments.get("character_id") or "").strip(),
                "query": str(arguments.get("query") or "").strip(),
                "limit": max(1, min(int(arguments.get("limit") or 6), 8)),
            },
        )
    elif name == "snow_get_relationship":
        payload = _get(
            "/api/v1/relationships/" + str(arguments.get("character_id") or "").strip()
        )
    else:
        raise RuntimeError(f"未知的 Snow 工具：{name}")
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def _response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _handle(message: dict[str, Any]) -> None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if method == "initialize":
        requested = str((message.get("params") or {}).get("protocolVersion") or "2025-06-18")
        _response(
            request_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return
    if method == "ping":
        _response(request_id, {})
        return
    if method == "tools/list":
        _response(request_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            _response(
                request_id,
                _call_tool(str(params.get("name") or ""), dict(params.get("arguments") or {})),
            )
        except Exception as exc:
            _response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        return
    if request_id is not None:
        _response(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                _handle(message)
        except json.JSONDecodeError as exc:
            _response(None, error={"code": -32700, "message": f"Parse error: {exc}"})


if __name__ == "__main__":
    main()
