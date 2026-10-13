"""Reviewed model-specific public chat parameters; never infer by substring.

Sources reviewed 2026-10-13:
https://developers.openai.com/api/docs/guides/latest-model
https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
https://api-docs.deepseek.com/guides/thinking_mode/
https://help.aliyun.com/zh/model-studio/deep-thinking
https://docs.z.ai/guides/capabilities/thinking-mode
https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart
https://platform.kimi.com/docs/guide/kimi-k2-7-code-quickstart
https://platform.kimi.com/docs/guide/kimi-k3-quickstart

These profiles apply only to the fixed official endpoints in public_providers.
This is parameter compatibility, not discovery or permission to switch models.
Unlisted IDs retain the existing non-streaming request contract and are reported
as unverified instead of falsely claiming that provider reasoning was disabled.
"""

from __future__ import annotations

import copy
from typing import Any


_MODELS: dict[tuple[str, str], dict[str, Any]] = {}


def _register(provider: str, names: tuple[str, ...], fields: dict[str, Any], *,
              effective: str = "off", token_parameter: str = "max_tokens",
              omit_temperature: bool = False, stream_include_usage: bool = False) -> None:
    for name in names:
        _MODELS[(provider, name)] = {
            "requested": "off",
            "effective": effective,
            "reason": "model_verified_thinking_off" if effective == "off" else "model_requires_reasoning",
            "provider_kind": provider,
            "request_fields": fields,
            "token_parameter": token_parameter,
            "omit_temperature": omit_temperature,
            "supports_streaming": True,
            "stream_include_usage": stream_include_usage,
            "capability_verified": True,
        }


_register("deepseek", ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"),
          {"thinking": {"type": "disabled"}}, stream_include_usage=True)

_register("dashscope", (
    "qwen-plus", "qwen-plus-latest", "qwen-plus-2025-04-28",
    "qwen-flash", "qwen-flash-2025-07-28",
    "qwen3.5-plus", "qwen3.5-plus-2026-02-15", "qwen3.5-flash", "qwen3.5-flash-2026-02-23",
    "qwen3.6-plus", "qwen3.6-plus-2026-04-02", "qwen3.6-flash", "qwen3.6-flash-2026-04-16",
    "qwen3.7-max", "qwen3.7-max-2026-05-20", "qwen3.7-max-2026-06-08",
    "qwen3.7-plus", "qwen3.7-plus-2026-05-26", "qwen3.7-flash", "qwen3.7-flash-2026-07-15",
    "qwen3.8-max", "qwen3.8-max-0902", "qwen3.8-flash",
), {"enable_thinking": False}, stream_include_usage=True)
_register("dashscope", ("qwen3.7-max-preview", "qwen3.7-max-2026-05-17", "qwen3.8-2.4t-a95b"),
          {}, effective="required", stream_include_usage=True)

_register("zhipu", ("glm-4.7", "glm-4.7-flashx"),
          {"thinking": {"type": "disabled"}})
_register("zhipu", ("glm-5.3", "glm-5.3-flash"), {}, effective="required")

_register("moonshot", ("kimi-k2.6",), {"thinking": {"type": "disabled"}}, omit_temperature=True)
_register("moonshot", ("kimi-k2.7-code",), {}, effective="required", omit_temperature=True)
_register("moonshot", ("kimi-k3",), {"reasoning_effort": "low"},
          effective="minimum", token_parameter="max_completion_tokens", omit_temperature=True)

_register("openai", (
    "gpt-4o", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20", "gpt-4o-mini", "gpt-4o-mini-2024-07-18",
    "gpt-4.1", "gpt-4.1-2025-04-14", "gpt-4.1-mini", "gpt-4.1-mini-2025-04-14",
), {}, token_parameter="max_completion_tokens", stream_include_usage=True)
_register("openai", ("gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-2025-08-07",
                     "gpt-5-mini-2025-08-07", "gpt-5-nano-2025-08-07"),
          {"reasoning_effort": "minimal"}, effective="minimum", token_parameter="max_completion_tokens",
          omit_temperature=True, stream_include_usage=True)
_register("openai", ("gpt-5.1", "gpt-5.1-2025-11-13"),
          {"reasoning_effort": "none"}, token_parameter="max_completion_tokens", stream_include_usage=True)


def immersive_model_policy(provider_kind: str, model: str) -> dict[str, Any]:
    """Return a detached policy for an exact official provider/model pair."""
    provider = str(provider_kind or "").strip().casefold()
    name = str(model or "").strip().casefold()
    policy = _MODELS.get((provider, name))
    if policy is not None:
        return copy.deepcopy(policy)
    return {
        "requested": "off", "effective": "unverified", "reason": "model_capability_unverified",
        "provider_kind": provider, "request_fields": {}, "token_parameter": "max_tokens",
        "omit_temperature": False, "supports_streaming": False,
        "stream_include_usage": False, "capability_verified": False,
    }


def public_model_profiles() -> dict[str, list[str]]:
    """Public, reviewed names only; never include discovered or private IDs."""
    return {
        provider: sorted(model for source, model in _MODELS if source == provider)
        for provider in sorted({provider for provider, _model in _MODELS})
    }
