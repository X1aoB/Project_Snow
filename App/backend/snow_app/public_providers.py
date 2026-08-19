"""Fixed official BYOK provider adapters. Custom base URLs are never accepted."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


class ProviderHTTPPool:
    """Reuse one bounded async HTTP pool per running public API process.

    Test clients can create a fresh event loop for each request, so the pool
    lazily replaces a client that belongs to an old loop.  Production uvicorn
    workers normally keep one loop for their entire lifetime.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._loop: Any | None = None
        self._lock = asyncio.Lock()

    async def _for_current_loop(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if self._client is None or self._loop is not loop:
                previous = self._client
                self._client = httpx.AsyncClient(
                    timeout=None,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                )
                self._loop = loop
                if previous is not None:
                    try:
                        await previous.aclose()
                    except Exception:
                        # A client tied to a closed test loop cannot always be
                        # awaited from the replacement loop; dropping it is
                        # safe because no request is shared across loops.
                        pass
            return self._client

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._for_current_loop()
        return await client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        client = await self._for_current_loop()
        return await client.post(url, **kwargs)

    async def close(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
            self._loop = None
            if client is not None:
                await client.aclose()


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    display_name: str
    base_url: str
    documentation_url: str
    privacy_url: str


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "https://platform.openai.com/docs",
        "https://openai.com/policies/privacy-policy/",
    ),
    "deepseek": ProviderSpec(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        "https://api-docs.deepseek.com/",
        "https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html",
    ),
    "dashscope": ProviderSpec(
        "dashscope",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://help.aliyun.com/zh/model-studio/",
        "https://www.alibabacloud.com/help/en/legal/latest/privacy-policy",
    ),
    "zhipu": ProviderSpec(
        "zhipu",
        "智谱",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/dev/api",
        "https://docs.bigmodel.cn/cn/terms/privacy-policy",
    ),
    "moonshot": ProviderSpec(
        "moonshot",
        "Moonshot",
        "https://api.moonshot.cn/v1",
        "https://platform.moonshot.cn/docs",
        "https://platform.kimi.com/docs/agreement/userprivacy",
    ),
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


async def discover_models(
    spec: ProviderSpec,
    api_key: str,
    timeout: float = 30,
    *,
    client: ProviderHTTPPool | None = None,
) -> list[str]:
    try:
        if client is not None:
            response = await client.get(
                _official_url(spec, "models"),
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as request_client:
                response = await request_client.get(
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
    client: ProviderHTTPPool | None = None,
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
        if client is not None:
            response = await client.post(
                _official_url(spec, "chat/completions"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as request_client:
                response = await request_client.post(
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
