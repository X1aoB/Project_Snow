"""Exercise the official wire contract without contacting any provider."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPProviderError
from backend.snow_app.proactive_message import ProactiveMessageService
from backend.snow_app.public_providers import PROVIDERS, ProviderRequestError
from backend.snow_app.public_service import PublicChatService
from tests.test_generation_latency_v2 import model_service, decision

ENDPOINT = "https://api.openai.com/v1"
SPEC = PROVIDERS["openai"]


def rejected(param):
    return httpx.Response(400, json={"error": {"type": "invalid_request_error",
        "code": "unsupported_parameter", "param": param, "message": "synthetic private text"}})


def strict_endpoint(request):
    body = json.loads(request.content)
    for field in ("max_tokens", "temperature"):
        if field in body:
            return rejected(field)
    assert body["max_completion_tokens"] > 0
    assert body.get("reasoning_effort") in {None, "low"}
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("path", ["chat", "summary", "proactive"])
def test_reasoning_model_accepts_all_public_generation_paths(monkeypatch, enabled, path):
    monkeypatch.setenv("PUBLIC_OPENAI_PARAMETER_ADAPTER", "on" if enabled else "off")
    model = "gpt-6.2-astra"
    if path == "chat":
        service = model_service(strict_endpoint)
        try:
            def call():
                return service._call_model("sys", "hi", model_settings=(ENDPOINT, "fake", model),
                    thinking_decision=decision("openai", stream=False))
            if enabled:
                assert call()[0] == "OK"
            else:
                with pytest.raises(MVPProviderError, match="400"):
                    call()
        finally:
            service.close()
        return

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(strict_endpoint)) as client:
            request = SimpleNamespace(model=model, reasoning_effort=None, turns=[], previous_summary="",
                character_id=MVP_CHARACTERS[0].character_id, request_id=uuid4())
            if path == "summary":
                async def run_once(job):
                    return await job()
                service = SimpleNamespace(provider_client=client, gate=SimpleNamespace(run=run_once),
                                          public_settings=SimpleNamespace(max_provider_calls_per_action=2))
                generate = PublicChatService.summarize(service, request, SPEC, "fake")
            else:
                service = SimpleNamespace(http=client, chat=Mock(), _tone_reference=lambda _: "")
                service.chat._public_generation_content.return_value = ([{"text": "OK"}], "OK", False, False)
                generate = ProactiveMessageService.generate(service, request, SimpleNamespace(location_name="test"),
                    SPEC, "fake", on_publication=lambda: None)
            if enabled:
                result = await generate
                assert result.get("summary") == "OK" or result.get("status") == "completed"
            else:
                with pytest.raises(ProviderRequestError, match="provider_request_failed"):
                    await generate

    asyncio.run(run())



@pytest.fixture(autouse=True)
def isolate_learned_fields(monkeypatch):
    from collections import OrderedDict
    from backend.snow_app import openai_completion
    monkeypatch.setattr(openai_completion, "_LEARNED", OrderedDict())
    monkeypatch.setenv("PUBLIC_OPENAI_PARAMETER_ADAPTER", "on")


@pytest.mark.parametrize("model,effort,sampling", [
    ("gpt-4o", None, True), ("gpt-4.1", None, True), ("gpt-5", "minimal", False),
    ("gpt-5.1", "none", True), ("gpt-5.5", "none", False), ("gpt-5.6-luna", "none", False),
    ("gpt-6-luna", "none", True), ("gpt-6-sol", "none", True), ("gpt-6.1-terra", "none", True),
    ("gpt-6-astra", "low", False), ("gpt-6.1-sol", "low", False), ("gpt-6.2-sol", "low", False),
    ("gpt-6.2-astra", "low", False), ("gpt-6.2-astra-unknown", None, False),
])
def test_official_request_shapes(model, effort, sampling):
    from backend.snow_app.openai_completion import CompletionRequest
    raw = {"model": model, "max_tokens": 800, **dict.fromkeys(("temperature", "top_p", "logprobs", "top_logprobs"), 1)}
    request = CompletionRequest(ENDPOINT + "/chat/completions", raw)
    assert request.body.get("reasoning_effort") == effort
    assert request.body["max_completion_tokens"] == 800 and "max_tokens" not in request.body
    assert all((key in request.body) == sampling for key in ("temperature", "top_p", "logprobs", "top_logprobs"))
    assert CompletionRequest("https://api.openai.com.example/v1/chat/completions", raw).body == raw


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("budget", [1, 2])
def test_learn_structured_error_even_when_budget_is_spent(stream, budget):
    from tests.test_generation_latency_v2 import envelope, sse
    bodies = []
    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "response_format" in body:
            return rejected("response_format")
        if stream:
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text=sse([envelope("OK")], usage={"total_tokens": 4}))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    service = model_service(handler)
    def call():
        return service._call_model("synthetic", "probe", model_settings=(ENDPOINT, "fake", "gpt-6.2-sol"),
            thinking_decision={**decision("openai", stream=stream), "max_provider_http_calls": budget})
    try:
        if budget == 1:
            with pytest.raises(MVPProviderError):
                call()
        else:
            assert call()[0]
        assert len(bodies) == budget
        service.reset_generation_diagnostics()
        assert call()[0]
        assert len(bodies) == budget + 1 and "response_format" not in bodies[-1]
    finally:
        service.close()


@pytest.mark.parametrize("status,code,public_code", [
    (429, "insufficient_quota", "provider_quota_exhausted"), (429, "rate_limit_exceeded", "provider_rate_limited"),
    (404, "model_not_found", "provider_model_unavailable"), (403, "permission_denied", "provider_model_unavailable"),
    (400, "other", "provider_request_failed"), (503, "other", "provider_unavailable"),
])
def test_error_classes_are_safe_and_do_not_replay_uncertain_requests(status, code, public_code):
    from backend.snow_app.public_providers import simple_completion
    from backend.snow_app.public_service import _public_error_payload
    from backend.snow_app.public_main import _error_detail
    from backend.snow_app.openai_completion import NON_RETRYABLE
    response = {"error": {"type": "invalid_request_error", "code": code, "param": "private field", "message": "private"}}
    calls = []
    def handler(req):
        calls.append(None)
        return httpx.Response(status, json=response)
    service = model_service(handler)
    try:
        with pytest.raises(MVPProviderError) as error:
            service._call_model("sys", "hi", model_settings=(ENDPOINT, "fake", "custom"))
        assert error.value.code == public_code and error.value.failure.http_status == status
    finally:
        service.close()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError) as caught:
                await simple_completion(SPEC, "fake", "custom", system_prompt="sys", user_prompt="hi", client=client)
            assert caught.value.code == public_code
            assert caught.value.failure.error_param == ""
            assert "private" not in str(caught.value.failure)
            assert _error_detail(public_code, caught.value.status_code)["retryable"] == (public_code not in NON_RETRYABLE)
            assert _public_error_payload("", public_code, "provider")["retryable"] == (public_code not in NON_RETRYABLE)
    asyncio.run(run())
    assert len(calls) == 2


def test_second_validation_failure_is_terminal():
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        return rejected("response_format" if len(calls) == 1 else "temperature")
    service = model_service(handler)
    try:
        with pytest.raises(MVPProviderError) as error:
            service._call_model("synthetic", "probe", model_settings=(ENDPOINT, "fake", "gpt-4o"))
        assert error.value.code == "provider_parameter_rejected"
        assert len(calls) == 2
        assert "response_format" in calls[0] and "response_format" not in calls[1]
    finally:
        service.close()


def test_private_probe_requires_the_parameter_under_test(tmp_path, monkeypatch):
    from backend.snow_app.agent_store import AgentStore
    from backend.snow_app.provider_registry import ProviderRegistry
    store = AgentStore(tmp_path / "agent.sqlite3")
    registry = ProviderRegistry(store)
    registry.vault = Mock()
    registry.vault.get.return_value = "fake"
    store.upsert_provider({"provider_id": "fixture", "display_name": "fixture", "kind": "openai",
        "base_url": ENDPOINT, "credential_ref": "fixture", "enabled": True, "trusted_data_types": ["text"]})
    seen = []
    def post(url, *, json, **kwargs):
        seen.append(json)
        request = httpx.Request("POST", url)
        if "response_format" in json:
            fail = rejected("response_format")
            fail.request = request
            return fail
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=request)
    monkeypatch.setattr(httpx, "post", post)
    result = registry.probe("fixture", "gpt-6.2-astra", {"structured_output": True})
    assert result["text_status"] == "ready"
    assert result["capability_status"]["structured_output"] == "failed"
    assert len(seen) == 2 and [item["max_completion_tokens"] for item in seen] == [64, 256]
    assert all("temperature" not in item and item["reasoning_effort"] == "low" for item in seen)


def test_private_text_does_not_reach_public_provider_log():
    import logging
    from backend.snow_app.public_logging import PublicLogFilter
    payload = {"event": "public_provider_failure", "provider": "openai", "model_profile": "private",
               "http_status": 429, "error_type": "private", "error_param": "private",
               "error_code": "insufficient_quota", "debug": "private"}
    record = logging.LogRecord("snow.public", logging.INFO, "", 0, json.dumps(payload), (), None)
    assert PublicLogFilter().filter(record)
    assert "private" not in record.getMessage()
    safe = json.loads(record.getMessage())
    assert safe["error_code"] == "insufficient_quota" and safe["model_profile"] == "other"



@pytest.mark.parametrize("status,code,expected", [(401, "invalid_api_key", "provider_credential_rejected"),
    (403, "permission_denied", "provider_model_discovery_failed"),
    (403, "unsupported_country_region_territory", "provider_region_unavailable"),
    (429, "insufficient_quota", "provider_quota_exhausted"), (503, "server_error", "provider_unavailable")])
def test_discovery_before_selection_distinguishes_failures(status, code, expected):
    from backend.snow_app.public_providers import discover_models
    def handler(request):
        assert request.url.path == "/v1/models" and request.method == "GET"
        return httpx.Response(status, json={"error": {"code": code}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError) as caught:
                await discover_models(SPEC, "fake", client=client)
            assert caught.value.code == expected
    asyncio.run(run())


def test_discovery_prioritizes_reviewed_account_models():
    from backend.snow_app.public_providers import discover_models
    models = ["arbitrary", "gpt-6.2-sol", "gpt-4o", "whisper-1"]
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": m} for m in models]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            actual = await discover_models(SPEC, "fake", client=client)
            assert actual == ["gpt-4o", "gpt-6.2-sol", "arbitrary", "whisper-1"]
    asyncio.run(run())



def test_relay_with_official_looking_model_is_never_retried_or_classified():
    from backend.snow_app.public_providers import relay_provider_spec, simple_completion
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return rejected("temperature")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ProviderRequestError, match="provider_request_failed"):
                await simple_completion(relay_provider_spec(ENDPOINT), "fixture", "gpt-6.2-sol",
                    system_prompt="synthetic", user_prompt="synthetic", client=client)
    asyncio.run(run())
    assert len(calls) == 1 and "max_tokens" in calls[0] and "temperature" in calls[0]


def test_discovery_cap_keeps_reviewed_model_and_only_valid_wire_ids():
    from backend.snow_app.public_providers import discover_models
    records = [{"id": f"arbitrary-{i}"} for i in range(600)]
    records.extend([{"id": "x" * 201}, {"id": 1234}, {"id": None}, {"id": " gpt-6.2-sol "}])
    def handler(request):
        return httpx.Response(200, json={"data": records})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            models = await discover_models(SPEC, "fake", client=client)
            assert len(models) == 500 and models[0] == "gpt-6.2-sol"
            assert all(len(model) <= 200 for model in models) and "1234" not in models
    asyncio.run(run())



def test_private_length_retry_updates_the_configured_token_field(monkeypatch):
    monkeypatch.setenv("MVP_CHAT_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("MVP_CHAT_MAX_TOKENS", "1024")
    bodies = []
    def handler(req):
        bodies.append(json.loads(req.content))
        reason = "length" if len(bodies) == 1 else "stop"
        return httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": "OK"}}]})
    service = model_service(handler)
    try:
        service._call_model("s", "u", model_settings=("https://example.invalid/v1", "fake", "kimi-k3"),
                            thinking_decision={"provider_kind": "moonshot", "chat_latency_v2": True})
        assert [body["max_completion_tokens"] for body in bodies] == [1024, 2048]
        assert all("max_tokens" not in body for body in bodies)
    finally:
        service.close()

