"""Minimal stdio MCP bridge for the local Project Snow Persona Gateway."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

try:
    import keyring
except ImportError:  # pragma: no cover - actionable error is returned to Codex
    keyring = None


SERVER_NAME = "snow-persona"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
KEYRING_SERVICE = "ProjectSnow"
TOKEN_REFERENCE = "persona-codex-current-token"
SERVER_INSTRUCTIONS = (
    "Read-only Project Snow persona server for Codex. At the start of an @Snow task, call "
    "snow_get_configuration once, select the requested or default character, then call "
    "snow_get_persona_snapshot and keep that character and profile_version fixed for the task. "
    "Use snow_search_knowledge only for relevant public lore. Never request private chats, "
    "attachments, scene state, Agent history, or tool logs. Never write data back to Snow. "
    "Preserve numbers, code, commands, paths, URLs, citations, tool results, failures, and "
    "uncertainty exactly when applying character voice."
)


class SnowMCPError(RuntimeError):
    """An actionable error that is safe to return through the MCP boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _plugin_version() -> str:
    manifest_path = Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = str(manifest.get("version") or "").strip()
        if version:
            return version
    except (OSError, json.JSONDecodeError):
        pass
    return "0.5.0"


SERVER_VERSION = _plugin_version()


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


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
        raise SnowMCPError(
            "credential_backend_unavailable",
            "未安装 keyring，无法读取 Project Snow 的本机配对令牌；请安装项目 requirements 后重试。"
        )
    try:
        token = str(keyring.get_password(KEYRING_SERVICE, TOKEN_REFERENCE) or "").strip()
    except Exception as exc:
        raise SnowMCPError(
            "credential_backend_unavailable",
            "无法读取 Windows 凭据库中的 Project Snow 配对令牌；请重新配对后重试。",
        ) from exc
    if not token:
        raise SnowMCPError(
            "pairing_missing",
            "尚未配对 Project Snow。请启动本地服务，在 /assistant/ 管理中心选择默认角色并点击“配对 Codex”。",
        )
    return token


def _base_url() -> str:
    raw = str(os.getenv("SNOW_PERSONA_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SnowMCPError(
            "invalid_configuration",
            "SNOW_PERSONA_BASE_URL 必须是有效的本机 HTTP 地址。",
        ) from exc
    if (
        parsed.scheme.casefold() != "http"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in {"127.0.0.1", "::1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is None
    ):
        raise SnowMCPError(
            "invalid_configuration",
            "SNOW_PERSONA_BASE_URL 只允许带显式端口的本机 HTTP loopback 地址。",
        )
    return raw


def _get(path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
    base_url = _base_url()
    token = _token()
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
        if exc.code == 401:
            raise SnowMCPError(
                "pairing_invalid",
                "Project Snow 配对已失效；请在 /assistant/ 管理中心重新配对 Codex。",
            ) from exc
        if exc.code == 404:
            raise SnowMCPError(
                "not_found",
                "Project Snow 中不存在所请求的角色或知识资源。",
            ) from exc
        raise SnowMCPError(
            "gateway_http_error",
            f"Project Snow 本地 Gateway 返回 HTTP {exc.code}。",
        ) from exc
    except URLError as exc:
        raise SnowMCPError(
            "gateway_unavailable",
            "无法连接 Project Snow 本地 API（默认 http://127.0.0.1:8000）；请先启动服务。"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnowMCPError(
            "gateway_invalid_response",
            "Project Snow 本地 Gateway 返回了无法解析的响应。",
        ) from exc


def _validate_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
    unexpected = sorted(set(arguments) - allowed)
    if unexpected:
        raise SnowMCPError(
            "invalid_arguments",
            "工具参数包含不支持的字段：" + ", ".join(unexpected),
        )


def _required_text(arguments: dict[str, Any], key: str, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnowMCPError("invalid_arguments", f"{key} 必须是非空字符串。")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise SnowMCPError("invalid_arguments", f"{key} 最长允许 {maximum} 个字符。")
    return normalized


def _knowledge_limit(arguments: dict[str, Any]) -> int:
    value = arguments.get("limit", 6)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8:
        raise SnowMCPError("invalid_arguments", "limit 必须是 1 到 8 之间的整数。")
    return value


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "snow_get_configuration":
        _validate_keys(arguments, set())
        payload = _get("/api/v1/persona/pairing")
    elif name == "snow_get_persona_snapshot":
        _validate_keys(arguments, {"character_id"})
        character_id = _required_text(arguments, "character_id", 200)
        payload = _get("/api/v1/persona/snapshot/" + quote(character_id, safe=""))
    elif name == "snow_search_knowledge":
        _validate_keys(arguments, {"character_id", "query", "limit"})
        payload = _get(
            "/api/v1/knowledge/search",
            {
                "character_id": _required_text(arguments, "character_id", 200),
                "query": _required_text(arguments, "query", 1000),
                "limit": _knowledge_limit(arguments),
            },
        )
    elif name == "snow_get_relationship":
        _validate_keys(arguments, {"character_id"})
        character_id = _required_text(arguments, "character_id", 200)
        payload = _get("/api/v1/relationships/" + quote(character_id, safe=""))
    else:
        raise SnowMCPError("unknown_tool", f"未知的 Snow 工具：{name}")
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


def _tool_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SnowMCPError):
        code = exc.code
        message = str(exc)
    else:
        code = "internal_error"
        message = "Snow Persona MCP 遇到未预期错误；请查看本地服务状态后重试。"
        if str(os.getenv("SNOW_PERSONA_DEBUG") or "").strip() == "1":
            exception_name = type(exc).__name__
            message += f"（异常类型：{exception_name}）"
            sys.stderr.write(f"snow-persona internal_error: {exception_name}\n")
            sys.stderr.flush()
    return {
        "content": [{"type": "text", "text": f"{code}: {message}"}],
        "structuredContent": {"error": {"code": code, "message": message}},
        "isError": True,
    }


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
                "instructions": SERVER_INSTRUCTIONS,
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
            if not isinstance(params, dict):
                raise SnowMCPError("invalid_arguments", "tools/call params 必须是对象。")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise SnowMCPError("invalid_arguments", "工具 arguments 必须是对象。")
            _response(
                request_id,
                _call_tool(str(params.get("name") or ""), arguments),
            )
        except Exception as exc:  # noqa: BLE001 - keep the stdio server alive with a safe error
            _response(request_id, _tool_error(exc))
        return
    if request_id is not None:
        _response(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    _configure_stdio()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                _handle(message)
        except json.JSONDecodeError as exc:
            _response(None, error={"code": -32700, "message": f"Parse error: {exc}"})


if __name__ == "__main__":
    main()
