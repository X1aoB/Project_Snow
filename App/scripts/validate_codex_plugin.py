"""Validate the repository's Codex plugin through its configured stdio command."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "snow-role-assistant"
EXPECTED_TOOLS = {
    "snow_get_configuration",
    "snow_get_persona_snapshot",
    "snow_search_knowledge",
    "snow_get_relationship",
}
PRIVATE_KEYS = {
    "messages",
    "conversation_summary",
    "scene_state",
    "analyst_location",
    "character_location",
    "active_costume",
    "agent_runs",
    "tool_logs",
    "attachments",
}
MAX_SNAPSHOT_BYTES = 32_000


class ValidationFailure(RuntimeError):
    """A safe, user-facing acceptance failure."""


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


def _stream_lines(stream: TextIO, destination: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            destination.put(line)
    finally:
        destination.put(None)


def _collect_stderr(stream: TextIO, destination: list[str]) -> None:
    for line in stream:
        destination.append(line.rstrip())


class MCPClient:
    def __init__(self, command: list[str], cwd: Path, environment: dict[str, str], timeout: float):
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self._next_id = 1
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        if self.process.stdin is None or self.process.stdout is None or self.process.stderr is None:
            raise ValidationFailure("无法创建 MCP stdio 管道。")
        self._stdout_thread = threading.Thread(
            target=_stream_lines,
            args=(self.process.stdout, self._responses),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=_collect_stderr,
            args=(self.process.stderr, self._stderr),
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _safe_stderr(self) -> str:
        value = "\n".join(self._stderr[-10:])[-1500:]
        token = str(os.getenv("SNOW_PERSONA_TOKEN") or "")
        if token:
            value = value.replace(token, "[REDACTED]")
        return value

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            detail = self._safe_stderr()
            raise ValidationFailure(f"MCP 进程已退出。{detail}".strip())
        self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValidationFailure(f"等待 MCP {method} 响应超时。")
            try:
                line = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise ValidationFailure(f"等待 MCP {method} 响应超时。") from exc
            if line is None:
                detail = self._safe_stderr()
                raise ValidationFailure(f"MCP 在响应 {method} 前退出。{detail}".strip())
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationFailure("MCP stdout 包含非 JSON-RPC 内容。") from exc
            if response.get("id") != request_id:
                raise ValidationFailure(
                    f"MCP 响应 ID 不匹配：预期 {request_id}，实际 {response.get('id')}。"
                )
            if response.get("error"):
                raise ValidationFailure(f"MCP {method} 返回协议错误：{response['error']}。")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValidationFailure(f"MCP {method} 未返回对象结果。")
            return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self, *, check: bool) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            return_code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return_code = self.process.wait(timeout=5)
        self._stdout_thread.join(timeout=1)
        self._stderr_thread.join(timeout=1)
        if check and return_code != 0:
            detail = self._safe_stderr()
            raise ValidationFailure(f"MCP 进程退出码为 {return_code}。{detail}".strip())

    def __enter__(self) -> MCPClient:
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        self.close(check=exc_type is None)


def _load_plugin(plugin_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    config_path = plugin_root / ".mcp.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailure(f"无法读取插件清单：{exc}") from exc
    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {"snow-persona"}:
        raise ValidationFailure(".mcp.json 必须只声明 snow-persona。")
    server = servers["snow-persona"]
    if not isinstance(server, dict) or not server.get("command"):
        raise ValidationFailure("snow-persona 缺少启动命令。")
    return manifest, server


def _process_environment(server: dict[str, Any]) -> dict[str, str]:
    configured = server.get("env") or {}
    if not isinstance(configured, dict):
        raise ValidationFailure("snow-persona env 必须是对象。")
    environment = {str(key): str(value) for key, value in configured.items()}
    environment.update(os.environ)
    return environment


def _server_cwd(plugin_root: Path, server: dict[str, Any]) -> Path:
    configured = server.get("cwd")
    if not isinstance(configured, str) or not configured.strip():
        raise ValidationFailure("snow-persona cwd 必须指向插件根目录。")
    resolved = (plugin_root / configured).resolve()
    if resolved != plugin_root.resolve():
        raise ValidationFailure("snow-persona cwd 必须解析为插件根目录。")
    return resolved


def _tool_payload(result: dict[str, Any], tool_name: str) -> dict[str, Any]:
    if result.get("isError"):
        error = (result.get("structuredContent") or {}).get("error") or {}
        code = str(error.get("code") or "tool_error")
        message = str(error.get("message") or "工具调用失败。")
        raise ValidationFailure(f"{tool_name} 失败：{code}: {message}")
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        raise ValidationFailure(f"{tool_name} 缺少 structuredContent。")
    return payload


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def validate(
    plugin_root: Path,
    *,
    mode: str,
    character_id: str | None,
    query_text: str,
    timeout: float,
) -> dict[str, Any]:
    manifest, server = _load_plugin(plugin_root)
    args = server.get("args") or []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValidationFailure("snow-persona args 必须是字符串数组。")
    command = [str(server["command"]), *args]
    started = time.perf_counter()
    server_cwd = _server_cwd(plugin_root, server)
    with MCPClient(command, server_cwd, _process_environment(server), timeout) as client:
        initialization = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "project-snow-validator", "version": "1"},
            },
        )
        client.notify("notifications/initialized", {})
        if initialization.get("serverInfo", {}).get("version") != manifest.get("version"):
            raise ValidationFailure("MCP serverInfo.version 与插件清单不一致。")
        instruction_prefix = str(initialization.get("instructions") or "")[:512]
        if "snow_get_configuration once" not in instruction_prefix:
            raise ValidationFailure("MCP instructions 未包含任务固定角色工作流。")
        tools = client.request("tools/list", {}).get("tools")
        if not isinstance(tools, list) or {item.get("name") for item in tools} != EXPECTED_TOOLS:
            raise ValidationFailure("MCP 工具集合与只读 Persona 契约不一致。")
        if any(
            not item.get("annotations", {}).get("readOnlyHint")
            or item.get("annotations", {}).get("destructiveHint")
            for item in tools
        ):
            raise ValidationFailure("MCP 工具缺少只读/非破坏性标注。")

        summary: dict[str, Any] = {
            "mode": mode,
            "plugin": manifest.get("name"),
            "version": manifest.get("version"),
            "server": initialization.get("serverInfo", {}).get("name"),
            "tool_count": len(tools),
        }
        if mode == "live":
            configuration = _tool_payload(
                client.request(
                    "tools/call",
                    {"name": "snow_get_configuration", "arguments": {}},
                ),
                "snow_get_configuration",
            )
            selected_character = str(
                character_id or configuration.get("default_character_id") or ""
            ).strip()
            if not selected_character:
                raise ValidationFailure("配对没有默认角色；请传入 --character-id。")
            snapshot_result = client.request(
                "tools/call",
                {
                    "name": "snow_get_persona_snapshot",
                    "arguments": {"character_id": selected_character},
                },
            )
            snapshot = _tool_payload(snapshot_result, "snow_get_persona_snapshot")
            relationship = _tool_payload(
                client.request(
                    "tools/call",
                    {
                        "name": "snow_get_relationship",
                        "arguments": {"character_id": selected_character},
                    },
                ),
                "snow_get_relationship",
            )
            knowledge = _tool_payload(
                client.request(
                    "tools/call",
                    {
                        "name": "snow_search_knowledge",
                        "arguments": {
                            "character_id": selected_character,
                            "query": query_text,
                            "limit": 3,
                        },
                    },
                ),
                "snow_search_knowledge",
            )
            leaked_keys = sorted(PRIVATE_KEYS.intersection(_walk_keys(snapshot)))
            if leaked_keys:
                raise ValidationFailure("人格快照包含私有字段：" + ", ".join(leaked_keys))
            snapshot_bytes = len(
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            if snapshot_bytes > MAX_SNAPSHOT_BYTES:
                raise ValidationFailure(
                    f"人格快照为 {snapshot_bytes} bytes，超过 {MAX_SNAPSHOT_BYTES} bytes 门槛。"
                )
            if (snapshot.get("projection") or {}).get("kind") != "codex_compact":
                raise ValidationFailure("人格快照缺少 codex_compact 投影标记。")
            content = snapshot_result.get("content") or []
            snapshot_text = content[0].get("text") if content and isinstance(content[0], dict) else ""
            snapshot_text_bytes = len(str(snapshot_text).encode("utf-8"))
            if snapshot_text_bytes > 2_048:
                raise ValidationFailure("人格快照文本重复了完整 structuredContent。")
            snapshot_character = str((snapshot.get("character") or {}).get("character_id") or "")
            relationship_character = str(
                (relationship.get("character") or {}).get("character_id") or ""
            )
            if not snapshot_character or snapshot_character != relationship_character:
                raise ValidationFailure("快照与关系工具返回的角色不一致。")
            summary.update(
                {
                    "character_id": snapshot_character,
                    "profile_version": snapshot.get("profile_version"),
                    "knowledge_results": len(knowledge.get("results") or []),
                    "private_key_count": 0,
                    "snapshot_bytes": snapshot_bytes,
                    "snapshot_text_bytes": snapshot_text_bytes,
                }
            )
    summary["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("portable", "live"), default="portable")
    parser.add_argument("--character-id")
    parser.add_argument("--query", default="角色的公开身份与当前关系")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--plugin-root", type=Path, default=DEFAULT_PLUGIN_ROOT)
    return parser


def main() -> int:
    _configure_stdio()
    args = _parser().parse_args()
    try:
        result = validate(
            args.plugin_root.resolve(),
            mode=args.mode,
            character_id=args.character_id,
            query_text=args.query,
            timeout=max(1.0, min(args.timeout, 120.0)),
        )
    except (ValidationFailure, OSError, subprocess.SubprocessError) as exc:
        print(f"Codex plugin validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
