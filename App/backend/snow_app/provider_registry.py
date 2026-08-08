"""Provider/model capability registry and quality-first routing.

The registry deliberately treats model names as untrusted labels.  A model is
eligible for a modality only when its capability is declared and verified (or
explicitly overridden by the user).  Secrets are delegated to the operating
system credential store through ``keyring``.
"""

from __future__ import annotations

from dataclasses import dataclass
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
)

BUILTIN_PROVIDERS = (
    {"provider_id": "openai", "display_name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1"},
    {"provider_id": "dashscope", "display_name": "阿里云百炼 Qwen", "kind": "dashscope", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"provider_id": "zhipu", "display_name": "智谱 GLM", "kind": "zhipu", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"provider_id": "deepseek", "display_name": "DeepSeek", "kind": "deepseek", "base_url": "https://api.deepseek.com/v1"},
    {"provider_id": "moonshot", "display_name": "Moonshot / Kimi", "kind": "moonshot", "base_url": "https://api.moonshot.cn/v1"},
)


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

    def public(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
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
        credential_ref = str(payload.get("credential_ref") or provider_id)
        if api_key:
            credential_ref = self.vault.put(credential_ref, api_key)
        record = self.store.upsert_provider({**payload, "provider_id": provider_id, "credential_ref": credential_ref})
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
        return {**model, "provider_id": model["provider_id"]}

    def models(self) -> list[dict[str, Any]]:
        return self.store.list_models()

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
        body = {
            "model": model_name,
            "messages": [{"role": "user", "content": "Respond with the single word OK."}],
            "max_tokens": 8,
            "temperature": 0,
        }
        started = monotonic()
        response = httpx.post(base_url + "/chat/completions", headers=headers, json=body, timeout=30, follow_redirects=True)
        response.raise_for_status()
        capabilities = _capabilities(text=True)
        evidence = {key: ("probe" if key == "text" else "unverified") for key in CAPABILITY_KEYS}
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
            "text": "passed",
            "status_code": response.status_code,
            "latency_ms": round((monotonic() - started) * 1000, 2),
            "health_status": "healthy",
        }
        if (requested or {}).get("structured_output"):
            structured_body = {
                **body,
                "messages": [{"role": "user", "content": "Return JSON with exactly one boolean field named ok."}],
                "response_format": {"type": "json_object"},
                "max_tokens": 32,
            }
            structured_response = httpx.post(base_url + "/chat/completions", headers=headers, json=structured_body, timeout=30, follow_redirects=True)
            structured_response.raise_for_status()
            structured_payload = structured_response.json()
            structured_content = (((structured_payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            decoded = json.loads(structured_content)
            capabilities["structured_output"] = isinstance(decoded, dict)
            evidence["structured_output"] = "probe"
            probe_details["structured_output"] = "passed"
        if (requested or {}).get("native_tool_calling"):
            tool_body = {
                **body,
                "messages": [{"role": "user", "content": "Call the probe tool once."}],
                "tools": [{"type": "function", "function": {"name": "probe", "description": "Capability probe", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}],
                "tool_choice": "required",
                "max_tokens": 64,
            }
            tool_response = httpx.post(base_url + "/chat/completions", headers=headers, json=tool_body, timeout=30, follow_redirects=True)
            tool_response.raise_for_status()
            tool_payload = tool_response.json()
            tool_calls = (((tool_payload.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or [])
            capabilities["native_tool_calling"] = bool(tool_calls)
            evidence["native_tool_calling"] = "probe"
            probe_details["native_tool_calling"] = "passed" if tool_calls else "failed"
        if (requested or {}).get("streaming"):
            stream_body = {**body, "stream": True, "max_tokens": 16}
            received = False
            with httpx.stream("POST", base_url + "/chat/completions", headers=headers, json=stream_body, timeout=30, follow_redirects=True) as stream_response:
                stream_response.raise_for_status()
                for line in stream_response.iter_lines():
                    if str(line).strip().startswith("data:"):
                        received = True
                        break
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
            "provider_id": provider_id,
            "model_name": model_name,
            "capabilities": capabilities,
            "probe_status": "verified",
            "probe": {**probe_details, "evidence": evidence, "pricing": pricing},
            "quality_score": float((requested or {}).get("quality_score") or 0),
            "context_window": context_window,
            "max_output_tokens": max_output_tokens,
        })
        return {**record, "provider_name": provider.get("display_name"), "probe_response": "ok"}

    def _env_selection(self, required: set[str]) -> ModelSelection | None:
        base_url = (os.getenv("MVP_CHAT_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL") or os.getenv("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        api_key = os.getenv("MVP_CHAT_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_COMPATIBLE_API_KEY") or ""
        model = os.getenv("MVP_CHAT_MODEL") or os.getenv("OPENAI_COMPATIBLE_MODEL") or ""
        if not base_url or not api_key or not model:
            return None
        capabilities = _capabilities(text=True, structured_output=True, streaming=True)
        if not required.issubset({key for key, value in capabilities.items() if value is True}):
            return None
        return ModelSelection("env-default", "当前环境配置", model, base_url, "environment", capabilities, "environment_default")

    def route(
        self,
        required: set[str],
        override: dict[str, Any] | None = None,
        required_data_types: set[str] | None = None,
        excluded_models: set[tuple[str, str]] | None = None,
        any_of: set[str] | None = None,
    ) -> ModelSelection:
        if not override:
            default_kind = (
                "speech_to_text" if "speech_to_text" in required
                else "text_to_speech" if "text_to_speech" in required
                else "vision" if "vision" in required
                else "text"
            )
            override = dict((self.store.get_meta("model_defaults", {}) or {}).get(default_kind) or {})
        override = override or {}
        required_data_types = set(required_data_types or {"text"})
        excluded_models = set(excluded_models or set())
        if override.get("provider_id") and override.get("model_name"):
            provider = self._provider_record(str(override["provider_id"]))
            if not provider or not provider.get("enabled"):
                raise ValueError("指定的 Provider 不存在或未启用。")
            if not required_data_types.issubset(set(provider.get("trusted_data_types") or [])):
                raise ValueError("指定 Provider 未获准接收本次数据类型。")
            model = next((item for item in self.store.list_models(str(override["provider_id"])) if item["model_name"] == str(override["model_name"])), None)
            if not model:
                raise ValueError("指定模型尚未完成能力探测。")
            actual = {key for key, value in (model.get("capabilities") or {}).items() if value is True}
            if not required.issubset(actual) or (any_of and not actual.intersection(any_of)):
                raise ValueError(f"指定模型不具备任务所需能力：{', '.join(sorted(required - actual))}")
            return ModelSelection(provider["provider_id"], provider["display_name"], model["model_name"], provider["base_url"], provider["credential_ref"], model["capabilities"], "user_override", quality_score=float(model.get("quality_score") or 0))
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
            if required.issubset(actual) and (not any_of or actual.intersection(any_of)) and model.get("probe_status") == "verified":
                candidates.append(ModelSelection(provider["provider_id"], provider["display_name"], model["model_name"], provider["base_url"], provider["credential_ref"], capabilities, "quality_first", quality_score=float(model.get("quality_score") or 0)))
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
        raise ValueError("没有已验证且具备所需能力的模型，请先配置并探测 Provider。")
