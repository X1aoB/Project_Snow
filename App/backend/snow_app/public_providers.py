"""Fixed official BYOK provider adapters. Custom base URLs are never accepted."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    base_url: str
    documentation_url: str


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec("openai", "OpenAI", "https://api.openai.com/v1", "https://platform.openai.com/docs"),
    "deepseek": ProviderSpec("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "https://api-docs.deepseek.com/"),
    "dashscope": ProviderSpec(
        "dashscope",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://help.aliyun.com/zh/model-studio/",
    ),
    "zhipu": ProviderSpec("zhipu", "智谱", "https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/dev/api"),
    "moonshot": ProviderSpec("moonshot", "Moonshot", "https://api.moonshot.cn/v1", "https://platform.moonshot.cn/docs"),
}


def _official_url(spec: ProviderSpec, path: str) -> str:
    """Construct a request URL only from the immutable adapter definition."""

    return f"{spec.base_url.rstrip('/')}/{path.lstrip('/')}"


class ProviderRequestError(RuntimeError):
    def __init__(self, code: str, status_code: int = 502):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def provider_spec(provider_id: str, enabled: tuple[str, ...]) -> ProviderSpec:
    provider_id = str(provider_id or "").casefold()
    if provider_id not in enabled or provider_id not in PROVIDERS:
        raise ProviderRequestError("provider_not_enabled", 422)
    return PROVIDERS[provider_id]


async def discover_models(spec: ProviderSpec, api_key: str, timeout: float = 30) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(
                _official_url(spec, "models"),
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise ProviderRequestError("provider_timeout", 504) from exc
    except httpx.HTTPError as exc:
        raise ProviderRequestError("provider_network_error") from exc
    if 300 <= response.status_code < 400:
        raise ProviderRequestError("provider_redirect_rejected")
    if response.status_code in {401, 403}:
        raise ProviderRequestError("provider_credential_rejected", 401)
    if response.status_code >= 400:
        raise ProviderRequestError("provider_model_discovery_failed")
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ProviderRequestError("provider_invalid_response") from exc
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    return sorted(
        {
            str(record.get("id"))
            for record in records
            if isinstance(record, dict) and str(record.get("id") or "").strip()
        }
    )[:500]


async def simple_completion(
    spec: ProviderSpec,
    api_key: str,
    model: str,
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 800,
    timeout: float = 120,
) -> tuple[str, dict[str, Any]]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.post(
                _official_url(spec, "chat/completions"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
    except httpx.TimeoutException as exc:
        raise ProviderRequestError("provider_timeout", 504) from exc
    except httpx.HTTPError as exc:
        raise ProviderRequestError("provider_network_error") from exc
    if 300 <= response.status_code < 400:
        raise ProviderRequestError("provider_redirect_rejected")
    if response.status_code in {401, 403}:
        raise ProviderRequestError("provider_credential_rejected", 401)
    if response.status_code == 429:
        raise ProviderRequestError("provider_rate_limited", 429)
    if response.status_code >= 400:
        raise ProviderRequestError("provider_request_failed")
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderRequestError("provider_invalid_response") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderRequestError("provider_empty_response")
    return content.strip(), dict(payload.get("usage") or {})
