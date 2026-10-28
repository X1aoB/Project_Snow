"""Fixed official BYOK provider adapters. Custom base URLs are never accepted."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx


# A relay station is an attacker-choosable upstream, so a completion response
# from it is bounded before we parse it (official vendors are trusted hosts and
# keep the unbounded path). 2 MiB comfortably holds any legitimate single-turn
# chat completion JSON.
RELAY_RESPONSE_BYTE_CAP = 2 * 1024 * 1024


class ProviderHTTPPool:
    """Reuse one bounded async HTTP pool per running public API process.

    Test clients can create a fresh event loop for each request, so the pool
    lazily replaces a client that belongs to an old loop.  Production uvicorn
    workers normally keep one loop for their entire lifetime.
    """

    def __init__(self, subscription_broker=None) -> None:
        self.subscription_broker = subscription_broker
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
        if str(url).startswith("https://codex-subscription.invalid"):
            from .public_subscription import BASE_URL, SubscriptionError, relay_authorization
            if url != BASE_URL + "/models" or self.subscription_broker is None:
                raise SubscriptionError("subscription_disabled", 404)
            catalogue = self.subscription_broker.catalogue(relay_authorization(kwargs.get("headers")))
            return httpx.Response(200, json={"data": [{"id": item["id"]} for item in catalogue]},
                                  request=httpx.Request("GET", url))
        client = await self._for_current_loop()
        return await client.get(url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        if str(url).startswith("https://codex-subscription.invalid"):
            from .public_subscription import BASE_URL, SubscriptionError, relay_authorization
            if url != BASE_URL + "/chat/completions" or self.subscription_broker is None:
                raise SubscriptionError("subscription_disabled", 404)
            payload = await self.subscription_broker.complete_async(
                relay_authorization(kwargs.get("headers")), kwargs.get("json") or {}, kwargs.get("timeout", 180),
            )
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))
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
    # Where the user creates a key ("" if none / not applicable).
    console_url: str = ""
    # "official" for a fixed-domain vendor adapter, "relay" for a user-entered
    # third-party transit station whose base_url is sealed per credential.
    kind: str = "official"
    # Short zh-CN note shown in the UI/config ("" when there is nothing to warn).
    caveat: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "codex_subscription": ProviderSpec(
        "codex_subscription", "Codex 订阅", "https://codex-subscription.invalid/v1",
        "https://developers.openai.com/codex/app-server/", "https://openai.com/policies/privacy-policy/",
    ),
    "openai": ProviderSpec(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "https://platform.openai.com/docs",
        "https://openai.com/policies/privacy-policy/",
        console_url="https://platform.openai.com/api-keys",
    ),
    "deepseek": ProviderSpec(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        "https://api-docs.deepseek.com/",
        "https://cdn.deepseek.com/policies/zh-CN/deepseek-privacy-policy.html",
        console_url="https://platform.deepseek.com/api_keys",
    ),
    "dashscope": ProviderSpec(
        "dashscope",
        "阿里云百炼",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://help.aliyun.com/zh/model-studio/",
        "https://www.alibabacloud.com/help/en/legal/latest/privacy-policy",
        console_url="https://bailian.console.aliyun.com/?apiKey=1",
    ),
    "zhipu": ProviderSpec(
        "zhipu",
        "智谱",
        "https://open.bigmodel.cn/api/paas/v4",
        "https://open.bigmodel.cn/dev/api",
        "https://docs.bigmodel.cn/cn/terms/privacy-policy",
        console_url="https://open.bigmodel.cn/usercenter/apikeys",
    ),
    "moonshot": ProviderSpec(
        "moonshot",
        "Moonshot",
        "https://api.moonshot.cn/v1",
        "https://platform.moonshot.cn/docs",
        "https://platform.kimi.com/docs/agreement/userprivacy",
        console_url="https://platform.moonshot.cn/console/api-keys",
    ),
    "anthropic": ProviderSpec(
        "anthropic",
        "Anthropic Claude",
        # The OpenAI-compatible layer lives under the same host; keep the
        # trailing slash so _official_url rstrips to a clean origin.
        "https://api.anthropic.com/v1/",
        "https://docs.anthropic.com/en/api/openai-sdk",
        "https://www.anthropic.com/legal/privacy",
        console_url="https://console.anthropic.com/settings/keys",
        caveat="Anthropic 官方说明其 OpenAI 兼容层主要用于测试与迁移对比，长期/生产建议用原生 API。",
    ),
    "xai": ProviderSpec(
        "xai",
        "xAI Grok",
        "https://api.x.ai/v1",
        "https://docs.x.ai/docs/api-reference",
        "https://x.ai/legal/privacy-policy",
        console_url="https://console.x.ai/",
    ),
    "gemini": ProviderSpec(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "https://ai.google.dev/gemini-api/docs/openai",
        "https://ai.google.dev/gemini-api/terms",
        console_url="https://aistudio.google.com/app/apikey",
    ),
    # Relay (第三方中转站) is a per-credential adapter: the real base_url is
    # sealed into each BYOK credential, never read from this dict.  This
    # sentinel only advertises the provider in /config and carries the UI
    # caveat; relay_provider_spec() builds the live spec from the sealed claim.
    "relay": ProviderSpec(
        "relay",
        "第三方中转站",
        "https://relay.invalid/v1",
        "",
        "",
        console_url="",
        kind="relay",
        caveat="第三方中转站由你自行选择与信任，密钥与对话内容会经由该站点转发，风险自担。",
    ),
}


def _official_url(spec: ProviderSpec, path: str) -> str:
    """Construct a request URL only from the immutable adapter definition."""

    return f"{spec.base_url.rstrip('/')}/{path.lstrip('/')}"


# Anthropic's version header. Its OpenAI-compatible layer accepts a standard
# Authorization: Bearer, but /models and the native surface also look for the
# x-api-key + anthropic-version pair, so we send both and the request works on
# either path.
ANTHROPIC_VERSION = "2023-06-01"


def _auth_headers(spec: ProviderSpec, api_key: str, *, json_body: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    else:
        headers["Accept"] = "application/json"
    if spec.provider_id == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    return headers


def relay_provider_spec(base_url: str) -> ProviderSpec:
    """Build the live relay adapter from a base URL sealed in a credential.

    The base URL is never taken from a request body here — callers pass the
    value already opened from the BYOK claim. The returned spec reuses the
    normal OpenAI-compatible discover/completion code path with provider_id
    "relay" so every route resolves identically.
    """

    relay = PROVIDERS["relay"]
    return ProviderSpec(
        provider_id="relay",
        display_name=relay.display_name,
        base_url=base_url,
        documentation_url=relay.documentation_url,
        privacy_url=relay.privacy_url,
        console_url=relay.console_url,
        kind="relay",
        caveat=relay.caveat,
    )


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
    if spec.provider_id == "codex_subscription" and client is None:
        raise ProviderRequestError("subscription_not_connected", 409)
    try:
        if client is not None:
            response = await client.get(
                _official_url(spec, "models"),
                headers=_auth_headers(spec, api_key),
                timeout=timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as request_client:
                response = await request_client.get(
                    _official_url(spec, "models"),
                    headers=_auth_headers(spec, api_key),
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
    reasoning_effort: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if spec.provider_id == "codex_subscription" and client is None:
        raise ProviderRequestError("subscription_not_connected", 409)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    if spec.provider_id == "codex_subscription" and reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    try:
        if client is not None:
            response = await client.post(
                _official_url(spec, "chat/completions"),
                headers=_auth_headers(spec, api_key, json_body=True),
                json=body,
                timeout=timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as request_client:
                response = await request_client.post(
                    _official_url(spec, "chat/completions"),
                    headers=_auth_headers(spec, api_key, json_body=True),
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
    # A relay is an attacker-choosable host: cap the response body we will
    # parse so a hostile station cannot exhaust memory with a giant payload.
    if spec.kind == "relay":
        raw = response.content
        if len(raw) > RELAY_RESPONSE_BYTE_CAP:
            raise ProviderRequestError("provider_response_too_large")
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ProviderRequestError("provider_invalid_response") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderRequestError("provider_empty_response")
    return content.strip(), dict(payload.get("usage") or {})
