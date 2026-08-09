"""Provider/model capability registry and quality-first routing.

The registry deliberately treats model names as untrusted labels.  A model is
eligible for a modality only when its capability is declared and verified (or
explicitly overridden by the user).  Secrets are delegated to the operating
system credential store through ``keyring``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx

from .agent_store import AgentStore

try:  # pragma: no cover - platform backend is exercised on Windows
    import keyring
except ImportError:  # pragma: no cover
    keyring = None


CAPABILITY_KEYS = (
    "text",
    "structured_output",
    "native_tool_calling",
    "vision",
    "audio_input",
    "speech_to_text",
    "text_to_speech",
    "streaming",
    "reasoning",
)

BUILTIN_PROVIDERS = (
    {"provider_id": "openai", "display_name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1"},
    {"provider_id": "dashscope", "display_name": "阿里云百炼 Qwen", "kind": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"provider_id": "zhipu", "display_name": "智谱 GLM", "kind": "zhipu", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"provider_id": "deepseek", "display_name": "DeepSeek", "kind": "deepseek", "base_url": "https://api.deepseek.com/v1"},
    {"provider_id": "moonshot", "display_name": "Moonshot / Kimi", "kind": "moonshot", "base_url": "https://api.moonshot.cn/v1"},
)

# These hints only affect presentation order.  They never grant a capability:
# availability still comes from the provider's own ``/models`` response and
# routability still comes from a successful text probe.
RECOMMENDED_MODEL_HINTS = {
    "openai": ("gpt-5", "gpt-4.1", "o3", "o4"),
    "deepseek": ("deepseek-v4", "deepseek-chat", "deepseek-reasoner"),
    "dashscope": ("qwen3", "qwen-max", "qwen-plus"),
    "zhipu": ("glm-4", "glm-5"),
    "moonshot": ("kimi", "moonshot"),
}

NON_CHAT_MODEL_HINTS = (
    "embedding",
    "moderation",
    "rerank",
    "speech",
    "tts",
    "whisper",
    "image",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ModelSelection:
    provider_id: str
    provider_name: str
    model_name: str
    base_url: str
    credential_ref: str
    capabilities: dict[str, Any]
    reason: str
    fallback: bool = False
    quality_score: float = 0.0
    provider_kind: str = "openai-compatible"

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "provider_kind": self.provider_kind,
            "model_name": self.model_name,
            "capabilities": self.capabilities,
            "quality_score": self.quality_score,
            "reason": self.reason,
            "fallback": self.fallback,
        }


class CredentialVault:
    SERVICE = "ProjectSnow"

    def put(self, reference: str, secret: str) -> str:
        reference = reference.strip() or f"provider-{uuid4().hex}"
        if not secret:
            return reference
        if keyring is None:
            raise RuntimeError("当前 Python 环境没有可用的系统凭据库。")
        keyring.set_password(self.SERVICE, reference, secret)
        return reference

    def get(self, reference: str) -> str:
        if not reference or keyring is None:
            return ""
        return str(keyring.get_password(self.SERVICE, reference) or "")

    def delete(self, reference: str) -> None:
        if not reference or keyring is None:
            return
        try:
            keyring.delete_password(self.SERVICE, reference)
        except Exception:
            return


def _capabilities(**overrides: bool) -> dict[str, Any]:
    base = {key: False for key in CAPABILITY_KEYS}
    base.update({"context_window": None, "max_output_tokens": None})
    base.update(overrides)
    return base


class ProviderRegistry:
    def __init__(self, store: AgentStore):
        self.store = store
        self.vault = CredentialVault()

    @staticmethod
    def builtin_providers() -> list[dict[str, Any]]:
        return [
            {
                **item,
                "enabled": False,
                "configured": False,
                "credential_ref": "",
                "trusted_data_types": [],
                "config": {},
                "source": "builtin",
            }
            for item in BUILTIN_PROVIDERS
        ]

    def save_provider(self, payload: dict[str, Any], api_key: str = "") -> dict[str, Any]:
        provider_id = str(payload.get("provider_id") or f"provider-{uuid4().hex[:12]}")
        existing = self._provider_record(provider_id) or {}
        previous_base_url = str(existing.get("base_url") or "").rstrip("/")
        credential_ref = str(payload.get("credential_ref") or provider_id)
        if api_key:
            credential_ref = self.vault.put(credential_ref, api_key)
        elif existing.get("credential_ref"):
            credential_ref = str(existing["credential_ref"])
        merged_config = {
            **dict(existing.get("config") or {}),
            **dict(payload.get("config") or {}),
        }
        record = self.store.upsert_provider({
            **existing,
            **payload,
            "provider_id": provider_id,
            "credential_ref": credential_ref,
            "config": merged_config,
        })
        current_base_url = str(record.get("base_url") or "").rstrip("/")
        if previous_base_url and current_base_url != previous_base_url:
            for model in self.store.list_models(provider_id):
                probe = {
                    **dict(model.get("probe") or {}),
                    "text": "unverified",
                    "health_status": "stale",
                    "stale_reason": "provider_base_url_changed",
                    "stale_at": _utc_now(),
                }
                self.store.upsert_model({
                    **model,
                    "probe_status": "unverified",
                    "probe": probe,
                })
        return self._public_provider(record)

    @staticmethod
    def _public_provider(record: dict[str, Any]) -> dict[str, Any]:
        return {
            **record,
            "configured": bool(record.get("credential_ref")),
            "credential_ref": "configured" if record.get("credential_ref") else "",
            "source": "stored",
        }

    def providers(self) -> list[dict[str, Any]]:
        stored = {item["provider_id"]: self._public_provider(item) for item in self.store.list_providers()}
        result: list[dict[str, Any]] = []
        for builtin in self.builtin_providers():
            configured = stored.pop(builtin["provider_id"], None)
            result.append(builtin | (configured or {}))
        result.extend(stored.values())
        return result

    def save_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = self.store.upsert_model(payload)
        return self._public_model(model)

    def models(self) -> list[dict[str, Any]]:
        return [self._public_model(model) for model in self.store.list_models()]

    @staticmethod
    def _capability_status(model: dict[str, Any], capability: str) -> str:
        capabilities = dict(model.get("capabilities") or {})
        probe = dict(model.get("probe") or {})
        evidence = dict(probe.get("evidence") or {})
        detail = str(probe.get(capability) or "").lower()
        source = str(evidence.get(capability) or "unverified")
        if source == "probe":
            if capabilities.get(capability) is True and detail != "failed":
                return "verified"
            return "failed" if detail == "failed" else "unsupported"
        if source in {"adapter_declaration", "vendor_metadata", "provider_listing", "user_override"}:
            return "declared" if capabilities.get(capability) is True else "unsupported"
        return "unverified"

    @classmethod
    def _public_model(cls, model: dict[str, Any]) -> dict[str, Any]:
        capabilities = dict(model.get("capabilities") or {})
        probe = dict(model.get("probe") or {})
        text_ready = bool(capabilities.get("text")) and (
            str(model.get("probe_status") or "") == "verified"
            or str(probe.get("text") or "") == "passed"
        )
        text_failed = str(probe.get("text") or "") == "failed"
        provider_enabled = bool(model.get("provider_enabled", True))
        return {
            **model,
            "selectable": provider_enabled and bool(capabilities.get("text")),
            "text_status": "ready" if text_ready else ("failed" if text_failed else "unverified"),
            "automatic_routing_eligible": provider_enabled and text_ready,
            "capability_status": {
                key: cls._capability_status(model, key)
                for key in (
                    "structured_output",
                    "streaming",
                    "vision",
                    "native_tool_calling",
                    "reasoning",
                )
            },
        }

    @staticmethod
    def _model_category(provider_id: str, model_name: str) -> str:
        lowered = model_name.casefold()
        if any(hint in lowered for hint in NON_CHAT_MODEL_HINTS):
            return "unknown_purpose"
        if any(hint in lowered for hint in RECOMMENDED_MODEL_HINTS.get(provider_id, ())):
            return "recommended"
        return "other_text"

    @staticmethod
    def _discovery_error(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, httpx.TimeoutException):
            return "timeout", "连接厂商超时；已保留上次发现的模型列表。"
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 401:
                return "authentication_failed", "API Key 无效或不属于该厂商的官方 API。"
            if status_code == 402:
                return "billing_required", "厂商账户余额或计费状态不可用。"
            if status_code == 403:
                return "permission_denied", "API Key 没有读取模型列表的权限。"
            if status_code == 404:
                return "endpoint_not_found", "厂商模型列表接口不存在，请检查 API 地址。"
            if status_code == 429:
                return "rate_limited", "厂商接口限流或额度已用尽，请稍后重试。"
            return "provider_http_error", f"厂商模型列表接口返回 HTTP {status_code}。"
        if isinstance(exc, httpx.HTTPError):
            return "network_error", "无法连接厂商接口，请检查网络和 API 地址。"
        return "invalid_response", f"厂商模型列表响应无法解析：{str(exc)[:240]}"

    def _save_discovery_state(self, provider: dict[str, Any], state: dict[str, Any]) -> None:
        config = {**dict(provider.get("config") or {}), "model_discovery": state}
        self.store.upsert_provider({**provider, "config": config})

    def discover_models(self, provider_id: str) -> dict[str, Any]:
        provider = self._provider_record(provider_id)
        if not provider:
            raise KeyError("Provider 不存在。")
        api_key = self._credential(provider)
        if not api_key:
            raise ValueError("Provider 尚未配置 API Key。")
        base_url = str(provider.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("Provider 尚未配置 API 地址。")
        cached = self.store.list_models(provider_id)
        discovered_at = _utc_now()
        try:
            response = httpx.get(
                base_url + "/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
            raw_models = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw_models, list):
                raise ValueError("响应缺少 data 模型列表。")
            model_names = sorted({
                str(item.get("id") or "").strip()
                for item in raw_models
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            })
            if not model_names:
                raise ValueError("厂商没有返回当前账户可用的模型。")
            existing = {str(item.get("model_name")): item for item in cached}
            for model_name in model_names:
                previous = existing.get(model_name) or {}
                capabilities = dict(previous.get("capabilities") or _capabilities())
                capabilities["text"] = capabilities.get("text") is not False
                if not previous:
                    capabilities["text"] = True
                probe = dict(previous.get("probe") or {})
                evidence = dict(probe.get("evidence") or {})
                evidence.setdefault("text", "provider_listing")
                probe.update({
                    "evidence": evidence,
                    "discovery_source": "provider_models_endpoint",
                    "discovered_at": discovered_at,
                    "category": self._model_category(provider_id, model_name),
                })
                self.store.upsert_model({
                    **previous,
                    "provider_id": provider_id,
                    "model_name": model_name,
                    "capabilities": capabilities,
                    "probe_status": previous.get("probe_status") or "unverified",
                    "probe": probe,
                })
            self._save_discovery_state(provider, {
                "status": "succeeded",
                "discovered_at": discovered_at,
                "model_count": len(model_names),
            })
            models = [self._public_model(item) for item in self.store.list_models(provider_id)]
            models.sort(key=lambda item: (
                {"recommended": 0, "other_text": 1, "unknown_purpose": 2}.get(
                    str((item.get("probe") or {}).get("category") or "other_text"), 1
                ),
                str(item.get("model_name") or ""),
            ))
            return {
                "status": "succeeded",
                "source": "provider_models_endpoint",
                "stale": False,
                "discovered_at": discovered_at,
                "models": models,
            }
        except Exception as exc:
            code, message = self._discovery_error(exc)
            self._save_discovery_state(provider, {
                "status": "failed",
                "attempted_at": discovered_at,
                "error_code": code,
                "error": message,
            })
            return {
                "status": "failed",
                "source": "cache",
                "stale": bool(cached),
                "attempted_at": discovered_at,
                "error_code": code,
                "error": message,
                "models": [self._public_model(item) for item in cached],
            }

    def _provider_record(self, provider_id: str) -> dict[str, Any] | None:
        return next((item for item in self.store.list_providers() if item["provider_id"] == provider_id), None)

    def _credential(self, provider: dict[str, Any]) -> str:
        reference = str(provider.get("credential_ref") or "")
        return self.vault.get(reference)

    def credential_for_selection(self, selection: ModelSelection) -> str:
        """Resolve a secret for an internal call without exposing it in APIs."""

        if selection.credential_ref == "environment":
            return (
                os.getenv("MVP_CHAT_API_KEY")
                or os.getenv("DASHSCOPE_API_KEY")
                or os.getenv("OPENAI_COMPATIBLE_API_KEY")
                or ""
            )
        return self.vault.get(selection.credential_ref)

    @staticmethod
    def thinking_request_fields(provider_kind: str, effective: str) -> dict[str, Any]:
        """Translate the internal policy into a provider-specific extension.

        Unknown compatible APIs receive no speculative parameter.  Callers
        still record the normalized decision so the UI can explain why a
        requested thinking mode was not applied.
        """

        kind = str(provider_kind or "openai-compatible").strip().casefold()
        enabled = str(effective or "off") == "on"
        if kind == "deepseek":
            return {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if kind == "dashscope":
            return {"enable_thinking": enabled}
        if kind == "openai" and enabled:
            return {"reasoning_effort": "medium"}
        return {}

    @classmethod
    def resolve_thinking(
        cls,
        selection: ModelSelection,
        mode: str,
        requested: str,
        *,
        complex_task: bool = False,
        agent_mode: bool = False,
    ) -> dict[str, Any]:
        requested_value = str(requested or "auto").strip().casefold()
        normalized_requested = requested_value if requested_value in {"auto", "off", "on"} else "auto"
        normalized_mode = str(mode or "immersive").strip().casefold()
        if normalized_mode == "immersive":
            effective = "off"
            reason = "immersive_low_latency_policy"
        elif normalized_requested == "off":
            effective = "off"
            reason = "user_disabled"
        elif normalized_requested == "on":
            effective = "on"
            reason = "user_enabled"
        elif agent_mode or complex_task:
            effective = "on"
            reason = "agent_or_complex_task"
        else:
            effective = "off"
            reason = "assistant_standard_question"
        supported = selection.provider_kind in {"deepseek", "dashscope"} or (
            selection.provider_kind == "openai"
            and selection.capabilities.get("reasoning") is True
        )
        if effective == "on" and not supported:
            effective = "off"
            reason = "provider_thinking_unverified"
        return {
            "requested": normalized_requested,
            "effective": effective,
            "reason": reason,
            "provider_kind": selection.provider_kind,
            "request_fields": cls.thinking_request_fields(selection.provider_kind, effective),
        }

    def probe(self, provider_id: str, model_name: str, requested: dict[str, Any] | None = None) -> dict[str, Any]:
        provider = self._provider_record(provider_id)
        if not provider:
            raise KeyError("Provider 不存在。")
        api_key = self._credential(provider)
        if not api_key:
            raise ValueError("Provider 尚未配置 API Key。")
        base_url = str(provider.get("base_url") or "").rstrip("/")
        if not base_url or not model_name:
            raise ValueError("能力探测需要 base_url 和 model_name。")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        provider_kind = str(provider.get("kind") or "openai-compatible")
        body = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Respond with the single word OK."}],
            "max_tokens": 64,
            "temperature": 0,
            **self.thinking_request_fields(provider_kind, "off"),
        }
        started = monotonic()
        try:
            response = httpx.post(base_url + "/chat/completions", headers=headers, json=body, timeout=30, follow_redirects=True)
            response.raise_for_status()
            payload = response.json()
            content = str((((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
            if not content:
                raise ValueError("文本探测没有返回最终正文。")
        except Exception as exc:
            previous = next((
                item for item in self.store.list_models(provider_id)
                if item.get("model_name") == model_name
            ), {})
            failed_probe = {
                **dict(previous.get("probe") or {}),
                "text": "failed",
                "attempted_at": _utc_now(),
                "error": self._discovery_error(exc)[1],
            }
            self.store.upsert_model({
                **previous,
                "provider_id": provider_id,
                "model_name": model_name,
                "capabilities": {**dict(previous.get("capabilities") or _capabilities()), "text": True},
                "probe_status": "unverified",
                "probe": failed_probe,
            })
            raise
        previous = next((
            item for item in self.store.list_models(provider_id)
            if item.get("model_name") == model_name
        ), {})
        capabilities = {**_capabilities(), **dict(previous.get("capabilities") or {}), "text": True}
        evidence = {key: ("probe" if key == "text" else "unverified") for key in CAPABILITY_KEYS}
        evidence.update(dict((previous.get("probe") or {}).get("evidence") or {}))
        evidence["text"] = "probe"
        provider_config = dict(provider.get("config") or {})
        adapter_capabilities = dict(provider_config.get("adapter_capabilities") or {})
        vendor_models = dict(provider_config.get("model_metadata") or {})
        vendor_metadata = dict(vendor_models.get(model_name) or {})
        for source_name, source in (("adapter_declaration", adapter_capabilities), ("vendor_metadata", vendor_metadata.get("capabilities") or {})):
            for key in CAPABILITY_KEYS:
                if key in source and key != "text":
                    capabilities[key] = bool(source[key])
                    evidence[key] = source_name
        probe_details: dict[str, Any] = {
            **dict(previous.get("probe") or {}),
            "text": "passed",
            "status_code": response.status_code,
            "latency_ms": round((monotonic() - started) * 1000, 2),
            "health_status": "healthy",
            "attempted_at": _utc_now(),
        }
        if (requested or {}).get("structured_output"):
            structured_body = {
                **body,
                "messages": [{"role": "user", "content": "Return JSON with exactly one boolean field named ok."}],
                "response_format": {"type": "json_object"},
                "max_tokens": 256,
            }
            try:
                structured_response = httpx.post(base_url + "/chat/completions", headers=headers, json=structured_body, timeout=30, follow_redirects=True)
                structured_response.raise_for_status()
                structured_payload = structured_response.json()
                structured_content = (((structured_payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                decoded = json.loads(structured_content)
                capabilities["structured_output"] = isinstance(decoded, dict)
                evidence["structured_output"] = "probe"
                probe_details["structured_output"] = "passed" if capabilities["structured_output"] else "failed"
            except Exception as exc:
                capabilities["structured_output"] = False
                evidence["structured_output"] = "probe"
                probe_details["structured_output"] = "failed"
                probe_details["structured_output_error"] = self._discovery_error(exc)[1]
        if (requested or {}).get("native_tool_calling"):
            tool_body = {
                **body,
                "messages": [{"role": "user", "content": "Call the probe tool once."}],
                "tools": [{"type": "function", "function": {"name": "probe", "description": "Capability probe", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
                "tool_choice": "required",
                "max_tokens": 64,
            }
            try:
                tool_response = httpx.post(base_url + "/chat/completions", headers=headers, json=tool_body, timeout=30, follow_redirects=True)
                tool_response.raise_for_status()
                tool_payload = tool_response.json()
                tool_calls = (((tool_payload.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or [])
                capabilities["native_tool_calling"] = bool(tool_calls)
                evidence["native_tool_calling"] = "probe"
                probe_details["native_tool_calling"] = "passed" if tool_calls else "failed"
            except Exception as exc:
                capabilities["native_tool_calling"] = False
                evidence["native_tool_calling"] = "probe"
                probe_details["native_tool_calling"] = "failed"
                probe_details["native_tool_calling_error"] = self._discovery_error(exc)[1]
        if (requested or {}).get("streaming"):
            stream_body = {**body, "stream": True, "max_tokens": 16}
            received = False
            try:
                with httpx.stream("POST", base_url + "/chat/completions", headers=headers, json=stream_body, timeout=30, follow_redirects=True) as stream_response:
                    stream_response.raise_for_status()
                    for line in stream_response.iter_lines():
                        if str(line).strip().startswith("data:"):
                            received = True
                            break
            except Exception as exc:
                probe_details["streaming_error"] = self._discovery_error(exc)[1]
            capabilities["streaming"] = received
            evidence["streaming"] = "probe"
            probe_details["streaming"] = "passed" if received else "failed"
        for key in CAPABILITY_KEYS:
            if key in (requested or {}):
                if key not in {"structured_output", "native_tool_calling", "streaming"}:
                    capabilities[key] = bool(requested[key])
                    evidence[key] = "user_override"
        context_window = (requested or {}).get("context_window") or vendor_metadata.get("context_window")
        max_output_tokens = (requested or {}).get("max_output_tokens") or vendor_metadata.get("max_output_tokens")
        pricing = {
            "input_per_million": (requested or {}).get("input_price_per_million", vendor_metadata.get("input_price_per_million")),
            "output_per_million": (requested or {}).get("output_price_per_million", vendor_metadata.get("output_price_per_million")),
        }
        record = self.store.upsert_model({
            **previous,
            "provider_id": provider_id,
            "model_name": model_name,
            "capabilities": capabilities,
            "probe_status": "verified",
            "probe": {**probe_details, "evidence": evidence, "pricing": pricing},
            "quality_score": float((requested or {}).get("quality_score") or 0),
            "context_window": context_window,
            "max_output_tokens": max_output_tokens,
        })
        return {**self._public_model(record), "provider_name": provider.get("display_name"), "probe_response": "ok"}

    def _env_selection(self, required: set[str]) -> ModelSelection | None:
        base_url = (os.getenv("MVP_CHAT_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL") or os.getenv("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        api_key = os.getenv("MVP_CHAT_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_COMPATIBLE_API_KEY") or ""
        model = os.getenv("MVP_CHAT_MODEL") or os.getenv("OPENAI_COMPATIBLE_MODEL") or ""
        if not base_url or not api_key or not model:
            return None
        capabilities = _capabilities(text=True, structured_output=True, streaming=True)
        if not required.issubset({key for key, value in capabilities.items() if value is True}):
            return None
        provider_kind = (
            "deepseek" if "deepseek" in base_url.casefold()
            else "dashscope" if "dashscope" in base_url.casefold()
            else "openai" if "api.openai.com" in base_url.casefold()
            else "openai-compatible"
        )
        return ModelSelection(
            "env-default", "当前环境配置", model, base_url, "environment",
            capabilities, "environment_default", provider_kind=provider_kind,
        )

    def route(
        self,
        required: set[str],
        override: dict[str, Any] | None = None,
        required_data_types: set[str] | None = None,
        excluded_models: set[tuple[str, str]] | None = None,
        any_of: set[str] | None = None,
        profile: str | None = None,
    ) -> ModelSelection:
        explicit_override = bool(override)
        default_kind = profile or (
                "speech_to_text" if "speech_to_text" in required
                else "text_to_speech" if "text_to_speech" in required
                else "vision" if "vision" in required
                else "text"
        )
        defaults = dict(self.store.get_meta("model_defaults", {}) or {})
        if not override:
            default_value = defaults.get(default_kind)
            if default_value is None and default_kind in {"immersive_text", "assistant_text", "assistant_agent"}:
                default_value = defaults.get("text")
            override = dict(default_value or {})
        required_data_types = set(required_data_types or {"text"})
        excluded_models = set(excluded_models or set())
        override = dict(override or {})
        override_key = (str(override.get("provider_id") or ""), str(override.get("model_name") or ""))
        if override_key in excluded_models:
            override = {}
        if override.get("provider_id") and override.get("model_name"):
            provider = self._provider_record(str(override["provider_id"]))
            if not provider or not provider.get("enabled"):
                raise ValueError("指定的 Provider 不存在或未启用。")
            if not required_data_types.issubset(set(provider.get("trusted_data_types") or [])):
                raise ValueError("指定 Provider 未获准接收本次数据类型。")
            model = next((item for item in self.store.list_models(str(override["provider_id"])) if item["model_name"] == str(override["model_name"])), None)
            if not model:
                raise ValueError("指定模型尚未被厂商发现或手动登记。")
            actual = {key for key, value in (model.get("capabilities") or {}).items() if value is True}
            if not required.issubset(actual) or (any_of and not actual.intersection(any_of)):
                if explicit_override:
                    raise ValueError(f"指定模型不具备任务所需能力：{', '.join(sorted(required - actual))}")
            else:
                public_model = self._public_model(model)
                if not public_model.get("selectable"):
                    if explicit_override:
                        raise ValueError("指定模型当前不可用于文本对话。")
                else:
                    return ModelSelection(
                        provider["provider_id"], provider["display_name"], model["model_name"],
                        provider["base_url"], provider["credential_ref"], model["capabilities"],
                        "user_override" if explicit_override else f"{default_kind}_default",
                        quality_score=float(model.get("quality_score") or 0),
                        provider_kind=str(provider.get("kind") or "openai-compatible"),
                    )
        candidates: list[ModelSelection] = []
        for model in self.store.list_models():
            if (str(model.get("provider_id")), str(model.get("model_name"))) in excluded_models:
                continue
            provider = self._provider_record(model["provider_id"])
            if not provider or not provider.get("enabled"):
                continue
            if not required_data_types.issubset(set(provider.get("trusted_data_types") or [])):
                continue
            if not self._credential(provider):
                continue
            capabilities = model.get("capabilities") or {}
            actual = {key for key, value in capabilities.items() if value is True}
            public_model = self._public_model(model)
            if required.issubset(actual) and (not any_of or actual.intersection(any_of)) and public_model.get("automatic_routing_eligible"):
                candidates.append(ModelSelection(
                    provider["provider_id"], provider["display_name"], model["model_name"],
                    provider["base_url"], provider["credential_ref"], capabilities,
                    f"{default_kind}_quality_first", quality_score=float(model.get("quality_score") or 0),
                    provider_kind=str(provider.get("kind") or "openai-compatible"),
                ))
        if candidates:
            model_records = {
                (str(item.get("provider_id")), str(item.get("model_name"))): item
                for item in self.store.list_models()
            }

            def rank(item: ModelSelection) -> tuple[float, int, int, float, float]:
                record = model_records.get((item.provider_id, item.model_name), {})
                probe = dict(record.get("probe") or {})
                pricing = dict(probe.get("pricing") or {})
                price = sum(float(value or 0) for value in pricing.values())
                latency = float(probe.get("latency_ms") or 10**9)
                return (
                    item.quality_score,
                    1 if item.capabilities.get("native_tool_calling") else 0,
                    int(record.get("context_window") or 0),
                    -latency,
                    -price,
                )

            return sorted(candidates, key=rank, reverse=True)[0]
        env = self._env_selection(required) if required_data_types.issubset({"text"}) else None
        if env and (not any_of or {key for key, value in env.capabilities.items() if value is True}.intersection(any_of)):
            return env
        raise ValueError("没有可自动路由的文本模型；请先配置 API Key 并选择厂商返回的模型。")
