"""Offline provider/stream regressions using synthetic content and transports."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import Request

from backend.snow_app.generation_stream import (
    CompleteBlockParser, GenerationObserver, GenerationStreamError,
    GenerationStreamPartialError, current_generation_observer, observe_generation,
    read_completion_stream, validate_generation_blocks,
)
from backend.snow_app.model_capabilities import immersive_model_policy
from backend.snow_app.mvp_policy import MVP_CHARACTERS
from backend.snow_app.mvp_service import MVPProviderError, MVPService
from backend.snow_app.config import Settings
from backend.snow_app.dialogue_core import DialogueEngine, GenerationGate, WorldState
from backend.snow_app.public_contracts import ChatRequest
from backend.snow_app.public_main import create_app
from backend.snow_app.public_service import PublicChatService
from backend.snow_app.public_store import PublicStore, PublicStoreUnavailable
from backend.snow_app.repository import RuntimeRepository
from backend.snow_app.output_validation import _clean_renderable_text, _parse_model_json
from tests.test_public_request_recovery import settings as public_test_settings


def envelope(*texts, channel="message"):
    return json.dumps({
        "state_updates": [], "expression_state": "neutral", "stage_motion": "none",
        "performance_id": "", "content_blocks": [{"type": channel, "text": text} for text in texts],
    }, ensure_ascii=False, separators=(",", ":"))


def sse(content, *, finish="stop", usage=None):
    records = [json.dumps({"choices": [{"index": 0, "delta": {"content": piece}}]}, ensure_ascii=False)
               for piece in content]
    if finish is not None:
        records.append(json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
    if usage is not None:
        records.append(json.dumps({"choices": [], "usage": usage}))
    return "".join("data: " + record + "\n\n" for record in records) + "data: [DONE]\n\n"


def model_service(handler):
    service = MVPService.__new__(MVPService)
    service._model_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    service._generation_diagnostics = threading.local()
    service.settings = SimpleNamespace(mvp_chat_timeout_seconds=1)
    service.reset_generation_diagnostics()
    return service


def decision(provider="deepseek", *, stream=True):
    return {"provider_kind": provider, "chat_latency_v2": True, "compact_public_schema": True,
            "verified_block_stream": stream, "max_provider_http_calls": 2,
            "disable_compatibility_retries": True}


@pytest.mark.parametrize("provider,model,fields,effective", [
    ("deepseek", "deepseek-flash", {"thinking": {"type": "disabled"}}, "off"),
    ("dashscope", "qwen3.8-max", {"enable_thinking": False}, "off"),
    ("zhipu", "glm-4.7", {"thinking": {"type": "disabled"}}, "off"),
    ("moonshot", "kimi-k2.6", {"thinking": {"type": "disabled"}}, "off"),
    ("moonshot", "kimi-k3", {"reasoning_effort": "low"}, "minimum"),
    ("moonshot", "kimi-k2.7-code", {}, "required"),
    ("openai", "gpt-5", {"reasoning_effort": "minimal"}, "minimum"),
    ("openai", "gpt-5.1", {"reasoning_effort": "none"}, "off"),
    ("zhipu", "glm-5.3", {}, "required"),
])
def test_exact_model_policy(provider, model, fields, effective):
    policy = immersive_model_policy(provider, model)
    assert policy["request_fields"] == fields
    assert policy["effective"] == effective
    assert policy["supports_streaming"] is True


def test_unknown_model_does_not_inherit_similar_model_capabilities():
    for provider, model in [("openai", "gpt-5.1-custom"), ("moonshot", "kimi-k2.7-code-private"),
                            ("dashscope", "kimi-k2.6"), ("deepseek", "custom-deepseek-flash")]:
        policy = immersive_model_policy(provider, model)
        assert policy["request_fields"] == {}
        assert policy["effective"] == "unverified"
        assert not policy["supports_streaming"]


@pytest.mark.parametrize("provider,model,expected,missing", [
    ("openai", "gpt-5", {"reasoning_effort": "minimal", "max_completion_tokens": 1600}, {"temperature", "max_tokens"}),
    ("moonshot", "kimi-k2.6", {"thinking": {"type": "disabled"}, "max_tokens": 1600}, {"temperature"}),
    ("moonshot", "kimi-k3", {"reasoning_effort": "low", "max_completion_tokens": 1600}, {"temperature", "max_tokens", "thinking"}),
])
def test_model_parameters_apply_without_losing_messages(provider, model, expected, missing):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": envelope("你好。")}}]})
    service = model_service(handler)
    try:
        service._call_model("所有角色事实", "完整十二轮记忆", model_settings=("https://example.invalid/v1", "fake", model),
                            thinking_decision=decision(provider, stream=False), max_tokens_override=1600)
    finally:
        service.close()
    body = captured[0]
    assert all(body[key] == value for key, value in expected.items())
    assert missing.isdisjoint(body)
    assert body["model"] == model
    assert body["messages"][1]["content"] == "完整十二轮记忆"


def test_compact_envelope_derives_answer_and_never_leaks_internal_json():
    raw = envelope("早上好。", "今天也想听你说说。")
    parsed = _parse_model_json(raw)
    assert parsed["answer"] == "早上好。\n今天也想听你说说。"
    assert _clean_renderable_text(raw) == parsed["answer"]
    assert _clean_renderable_text(json.dumps(raw)) == parsed["answer"]
    assert _clean_renderable_text('{"content_blocks":[{"type":"message","text":"未完成') == ""
    assert _clean_renderable_text('{"content_blocks":[{"type":"message","text":{"private":"field"}}]}') == ""


def test_compact_schema_keeps_character_contract_and_assistant_schema_is_unchanged():
    service = MVPService.__new__(MVPService)
    character = MVP_CHARACTERS[0]
    normal = service._system_prompt(character, None, communication_channel="text")
    compact = service._system_prompt(character, None, communication_channel="text", compact_public_schema=True)
    assistant = service._system_prompt(character, None, mode="assistant", compact_public_schema=True)
    assert len(compact) < len(normal)
    assert '"answer":"中文回答"' not in compact
    assert '"sections":' not in compact
    assert "content_blocks 必须是对象最后一个字段" in compact
    assert "身份与证据规则" in compact and "文字通讯" in compact
    assert '"sections":' in assistant


def test_complete_block_parser_does_not_publish_partial_string_or_duplicate_block():
    seen = []
    parser = CompleteBlockParser(seen.append)
    raw = envelope('早上好，"朋友"。', "第二条🌨。")
    for char in raw:
        parser.feed(char)
    assert len(seen) == 2
    assert seen[0] == [{"type": "message", "text": '早上好，"朋友"。'}]
    assert seen[1][-1]["text"] == "第二条🌨。"


@pytest.mark.parametrize("raw", [
    '{"content_blocks":[{"type":"message","text":"你好。"}],"state_updates":[]}',
    '{"state_updates":[{"type":"joint_move"}],"content_blocks":[{"type":"message","text":"走吧。"}]}',
    '{"state_updates":[],"stage_motion":"lean_in","content_blocks":[{"type":"speech","text":"你好。"}]}',
    '{"state_updates":[],"content_blocks":[{"type":"message","text":"你好。"}]}',
    '{"state_updates":[],"stage_motion":"none","content_blocks":[{"type":"message","text":"你好。"}]}',
    '```json\n' + envelope("你好。") + '\n```',
    '{"state_updates":[],"content_blocks":[{"type":"sticker","asset_id":"fixture"}]}',
])
def test_unverified_envelopes_stay_buffered(raw):
    seen = []
    CompleteBlockParser(seen.append).feed(raw)
    assert seen == []


def test_callback_requires_durable_success_before_marking_published():
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: False)
    blocks = [{"type": "message", "text": "你好。"}]
    assert not observer.publish(blocks)
    assert observer.published_blocks == []
    observer.on_verified_blocks = lambda _blocks: True
    assert observer.publish(blocks)
    with pytest.raises(GenerationStreamPartialError) as caught:
        observer.ensure_prefix([{"type": "message", "text": "被替换。"}])
    assert caught.value.committed_blocks == blocks


def test_observer_propagates_to_thread_but_not_another_request():
    observer = GenerationObserver()
    async def exercise():
        with observe_generation(observer):
            assert await asyncio.to_thread(current_generation_observer) is observer
        assert current_generation_observer() is None
    asyncio.run(exercise())


def test_real_sse_publishes_before_upstream_eof_and_excludes_reasoning():
    raw = envelope("早上好。", "你想聊什么？")
    prefix, suffix = raw.split(',{"type":"message"', 1)
    prefix += ","
    suffix = '{"type":"message"' + suffix
    committed = []
    observer = GenerationObserver(on_verified_blocks=lambda blocks: committed.append(blocks) or True)
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield ('data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "HIDDEN"}}]}) + '\n\n').encode()
            yield sse([prefix], finish=None).replace("data: [DONE]\n\n", "").encode()
            assert len(committed) == 1, "First complete block must commit before the next upstream chunk"
            yield sse([suffix], usage={"prompt_tokens": 10, "completion_tokens": 20}).encode()
    service = model_service(lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks()))
    try:
        with observe_generation(observer), validate_generation_blocks(observer.publish):
            content, usage = service._call_model("json", "你好", model_settings=("https://example.invalid/v1", "fake", "deepseek-flash"),
                                                  thinking_decision=decision(), max_tokens_override=1600)
    finally:
        service.close()
    assert content == raw and "HIDDEN" not in content
    assert len(committed) == 2 and usage["completion_tokens"] == 20
    assert "provider_first_content_ms" in observer.timings_ms
    assert service.generation_diagnostics()["provider_http_calls"] == 1


@pytest.mark.parametrize("kind", ["http", "timeout", "incomplete", "invalid_sse", "wrong_content_type"])
def test_stream_failure_never_blindly_retries(kind):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        if kind == "http":
            return httpx.Response(503)
        content = "data: not-json\n\n" if kind == "invalid_sse" else sse([envelope("你好。")], finish=None)
        return httpx.Response(200, headers={"content-type": "application/json" if kind == "wrong_content_type" else "text/event-stream"}, text=content)
    service = model_service(handler)
    try:
        with pytest.raises(MVPProviderError):
            service._call_model("json", "你好", model_settings=("https://example.invalid/v1", "fake", "deepseek-flash"),
                                thinking_decision=decision(), max_tokens_override=1600)
    finally:
        service.close()
    assert count == 1


def test_interrupted_stream_preserves_committed_prefix_and_call_count():
    prefix = envelope("早上好。")[:-2] + ","
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    service = model_service(lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse([prefix], finish=None)))
    try:
        with observe_generation(observer), validate_generation_blocks(observer.publish), pytest.raises(GenerationStreamPartialError) as caught:
            service._call_model("json", "你好", model_settings=("https://example.invalid/v1", "fake", "deepseek-flash"),
                                thinking_decision=decision(), max_tokens_override=1600)
    finally:
        service.close()
    assert caught.value.committed_blocks == [{"type": "message", "text": "早上好。"}]
    assert service.generation_diagnostics()["provider_http_calls"] == 1


def test_eligibility_excludes_global_state_and_keeps_full_context():
    context = {"question_focus": "casual_check_in", "conversation_mode": "natural_chat",
               "session_context": {"turns": ["完整记忆"] * 24}}
    assert MVPService._verified_stream_eligible(context, "immersive")
    assert len(context["session_context"]["turns"]) == 24
    for altered in ({"question_focus": "past_experience"}, {"live_scene": {"status": "active"}},
                    {"query_intents": ["relationship"]}, {"mentioned_characters": [{"character_id": "fixture"}]}):
        assert not MVPService._verified_stream_eligible({**context, **altered}, "immersive")
    assert not MVPService._verified_stream_eligible(context, "immersive", movement_candidates=[{"location_id": "fixture"}])


def test_prefix_reuses_full_guard_and_refuses_rewrite_after_publication():
    service = MVPService.__new__(MVPService)
    service._answer_guardrail_violations = Mock(return_value=[])
    service._repair_explicit_relationship_answer = Mock(side_effect=lambda _message, text, _context: (text, False))
    service._repair_latest_state_answer = Mock(side_effect=lambda _message, text, _context: (text, False))
    service._normalize_explicit_relationship_address = Mock(side_effect=lambda text, _context: (text, False))
    service._normalize_response_aliases = Mock(side_effect=lambda text, _context: (text, False))
    blocks = [{"type": "message", "text": "早上好。"}]
    context = {"session_context": {"turns": ["prior"] * 24}}
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    with observe_generation(observer):
        service._verify_stream_blocks(blocks, message="早上好", context=context, channel="text", character_name="里芙")
        assert observer.published_blocks == blocks
        assert service._answer_guardrail_violations.call_args.args[2] is context
        service._answer_guardrail_violations.return_value = ["unsupported_session_premise:fixture"]
        with pytest.raises(GenerationStreamPartialError):
            service._verify_stream_blocks(blocks + [{"type": "message", "text": "我们刚才一起。"}],
                                          message="早上好", context=context, channel="text", character_name="里芙")


@pytest.fixture
def isolated_dialogue(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.snow_app.mvp_service._load_local_environment", lambda: None)
    settings = Settings(data_root=tmp_path, runtime_root=tmp_path, chat_enabled=True,
                        embedding_model="fixture", allowed_origins=[], mvp_chat_enabled=True)
    service = MVPService(settings, RuntimeRepository(settings), force_chat_enabled=True,
                         conversation_database_path=tmp_path / "fixture.sqlite3")
    character = next(item for item in MVP_CHARACTERS if item.display_name == "里芙")
    monkeypatch.setattr(service, "_views", lambda: {character.character_id: {"character_id": character.character_id}})
    context = {
        "character": character, "question_focus": "casual_check_in", "conversation_mode": "natural_chat",
        "mode": "immersive", "query_intents": (), "hits": [], "provisional_relations": [],
        "fusion": "fixture", "vector_available": False, "session_context": {"turns": []},
    }
    monkeypatch.setattr(service, "retrieve", lambda *_args, **_kwargs: dict(context))
    service.reset_generation_diagnostics()
    yield service, character
    service.close()


@pytest.mark.parametrize("channel,suffix,success", [
    ("text", "你想聊些什么？", True),
    ("in_person", "你想聊些什么？", True),
    ("text", "我看见你穿着红色外套。", False),
])
def test_full_dialogue_commits_verified_greeting_before_eof_and_never_rewrites_prefix(isolated_dialogue, channel, suffix, success):
    service, character = isolated_dialogue
    block_type = "message" if channel == "text" else "speech"
    raw = envelope("早上好。", suffix, channel=block_type)
    block_start = '{"type":"' + block_type + '"'
    before, after = raw.split("," + block_start, 1)
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    calls = []
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield sse([before + ","], finish=None).replace("data: [DONE]\n\n", "").encode()
            assert observer.published_blocks == [{"type": block_type, "text": "早上好。"}]
            yield sse([block_start + after], usage={"completion_tokens": 20}).encode()
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())
    service._model_http_client.close()
    service._model_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    session = {**service._empty_session_context(), "character_id": character.character_id, "communication_channel": channel}
    world = WorldState({"world_session_id": "fixture-world", "created_at": "2026-10-13T00:00:00Z",
                        "analyst_location": "休息室", "presence": {character.character_id: {"location": "休息室", "activity": "休息"}}})
    with service.isolated_state("fixture-session", "fixture-world", session, world), observe_generation(observer):
        kwargs = dict(session_id="fixture-session", world_session_id="fixture-world", communication_channel=channel,
                      model_settings=("https://example.invalid/v1", "fake", "deepseek-flash"),
                      thinking_decision=decision(), persist_exchange=False, remember_session=False, max_tokens_override=1600,
                      public_performance_candidates=[{"character_id": character.character_id, "performance_id": "approved_fixture"}])
        if success:
            result = service.chat(character.character_id, "早上好", **kwargs)
            assert result["answer"] == "早上好。\n" + suffix
            assert result["validation_disposition"] == "accepted"
        else:
            with pytest.raises(GenerationStreamPartialError) as caught:
                service.chat(character.character_id, "早上好", **kwargs)
            assert caught.value.committed_blocks == [{"type": "message", "text": "早上好。"}]
    assert len(calls) == 1
    assert service.generation_diagnostics()["provider_http_calls"] == 1


def test_real_retrieval_check_in_remains_eligible_with_synthetic_runtime(isolated_dialogue):
    service, character = isolated_dialogue
    context = MVPService.retrieve(service, character.character_id, "早上好", mode="immersive",
                                  session_context=service._empty_session_context(),
                                  world_state={"presence": {character.character_id: {"location": "休息室", "activity": "休息"}}})
    assert context["question_focus"] == "casual_check_in"
    assert MVPService._verified_stream_eligible(
        context, "immersive", performance_candidates=[{"performance_id": "approved_fixture"}]
    )


def test_whole_turn_guard_failure_after_published_blocks_cannot_start_rewrite(isolated_dialogue, monkeypatch):
    service, character = isolated_dialogue
    raw = envelope("早上好。", "你想聊些什么？")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse([raw]))
    service._model_http_client.close()
    service._model_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    original_guard = service._answer_guardrail_violations
    guard_calls = 0
    def guard(*args, **kwargs):
        nonlocal guard_calls
        guard_calls += 1
        violations = original_guard(*args, **kwargs)
        return violations + (["whole_turn_fixture_violation"] if guard_calls >= 3 else [])
    monkeypatch.setattr(service, "_answer_guardrail_violations", guard)
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    session = {**service._empty_session_context(), "character_id": character.character_id, "communication_channel": "text"}
    world = WorldState({"world_session_id": "fixture-world", "created_at": "2026-10-13T00:00:00Z", "presence": {}})
    with service.isolated_state("fixture-session", "fixture-world", session, world), observe_generation(observer):
        with pytest.raises(GenerationStreamPartialError) as caught:
            service.chat(character.character_id, "早上好", session_id="fixture-session", world_session_id="fixture-world",
                         communication_channel="text", model_settings=("https://example.invalid/v1", "fake", "deepseek-flash"),
                         thinking_decision=decision(), persist_exchange=False, remember_session=False)
    assert len(caught.value.committed_blocks) == 2
    assert len(calls) == 1
    assert service.generation_diagnostics()["model_calls"] == 1


def test_duplicate_state_metadata_cannot_replace_published_prefix():
    raw = envelope("早上好。")[:-1] + ',"state_updates":[{"type":"joint_move"}]}'
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    service = model_service(lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse([raw])))
    try:
        with observe_generation(observer), validate_generation_blocks(observer.publish), pytest.raises(GenerationStreamPartialError):
            service._call_model("json", "你好", model_settings=("https://example.invalid/v1", "fake", "deepseek-flash"),
                                thinking_decision=decision(), max_tokens_override=1600)
    finally:
        service.close()
    assert observer.published_blocks == [{"type": "message", "text": "早上好。"}]


@pytest.mark.parametrize("failure", ["first_write", "later_write", "lost_ack", "ack_timeout"])
def test_real_public_mvp_publication_failure_retains_paid_diagnostics_and_original_uuid(
    isolated_dialogue, monkeypatch, failure,
):
    """Run real route, public boundary, engine, MVP, HTTP parser and durable store.

    Only upstream HTTP, synthetic retrieval/world input and catalog metadata are
    fixtures; no replacement chat method can conceal worker-local accounting.
    """
    mvp, character = isolated_dialogue
    public_settings = replace(public_test_settings(), verified_block_stream=True)
    service = PublicChatService.__new__(PublicChatService)
    service.public_settings = public_settings
    service.provider_client = None
    service.mvp = mvp
    service.repository = Mock()
    service.repository.request_diagnostics.return_value = {}
    service.repository.request_health.return_value = {}
    service.engine = DialogueEngine()
    service.gate = GenerationGate()
    service.media = Mock()
    service.media.verify.return_value = {"status": "unavailable"}
    service.media.performance_catalog.return_value = []
    service.stickers = Mock()
    service.stickers.verify.return_value = {"status": "unavailable"}
    monkeypatch.setattr(service, "sticker_candidates", lambda **_kwargs: [])
    monkeypatch.setattr(service, "_normalized_state", lambda *_args: {
        "analyst_location": "休息室",
        "presence": {character.character_id: {"location": "休息室", "activity": "休息"}},
    })
    monkeypatch.setattr("backend.snow_app.public_main.open_byok_credential", lambda *_args, **_kwargs: {"api_key": "synthetic"})
    calls = []
    known_usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    raw = envelope("早上好。", "你想聊些什么？")
    def handler(request):
        calls.append(json.loads(request.content))
        # Usage can accompany a content event. It must survive publication
        # failure even though the stream never reaches its normal return.
        first_event = json.dumps({"usage": known_usage, "choices": [{"index": 0, "delta": {"content": raw}}]}, ensure_ascii=False)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="data: " + first_event + "\n\n" + sse([]))
    mvp._model_http_client.close()
    mvp._model_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    store = PublicStore("sqlite+pysqlite:///:memory:")
    store.create_schema()
    original_commit = store.commit_request_blocks
    writes = 0
    def failed_commit(*args, **kwargs):
        nonlocal writes
        writes += 1
        if failure == "later_write" and writes == 1:
            return original_commit(*args, **kwargs)
        if failure == "lost_ack":
            original_commit(*args, **kwargs)
        if failure == "ack_timeout":
            raise TimeoutError("synthetic publication ACK timeout")
        raise PublicStoreUnavailable("synthetic publication outage")
    monkeypatch.setattr(store, "commit_request_blocks", failed_commit)
    app = create_app(public_settings, mvp.settings, store, service)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/public/v1/chat/stream")
    payload = ChatRequest(request_id=uuid4(), provider="openai", credential="synthetic-credential-value",
                          model="gpt-4o-mini", character_id=character.character_id, message="早上好")
    def request():
        return Request({"type": "http", "headers": [(b"x-snow-stream-version", b"2")],
                        "state": {"anonymous_id": "synthetic", "subject_hash": "subject"}})
    async def exercise():
        try:
            response = await endpoint(request(), payload)
            first = "".join([packet async for packet in response.body_iterator])
            saved = store.request_result(str(payload.request_id), "subject")
            assert saved["terminal_error"] == "public_database_unavailable"
            assert saved["diagnostics"]["error_stage"] == "publication_store"
            assert saved["diagnostics"]["timings_ms"]["provider_http_calls"] == 1
            assert saved["usage"] == known_usage
            replay = await endpoint(request(), payload)
            repeated = "".join([packet async for packet in replay.body_iterator])
            assert '"code":"public_database_unavailable"' in first and '"code":"public_database_unavailable"' in repeated
            has_prefix = failure in {"later_write", "lost_ack"}
            assert ("event: committed_block" in first) == has_prefix
            assert ("event: committed_block" in repeated) == has_prefix
            assert ('"partial":true' in first) == has_prefix
            if not has_prefix:
                assert '"retryable":true' in first
            assert len(calls) == 1
            assert mvp.generation_diagnostics()["provider_http_calls"] == 0, "Counters remain worker-local; terminal persistence must capture them there"
        finally:
            await asyncio.gather(*list(app.state.chat_jobs.values()), return_exceptions=True)
            await app.state.async_store.close()
            await app.state.provider_http.close()
            store.engine.dispose()
    asyncio.run(exercise())


def static_profile():
    return {
        "profile_id": "synthetic-profile", "state_policy": "latest_available",
        "identity_evidence": [{"statement": "合成角色资料", "support_level": "supported", "evidence_document_ids": ["doc-a"]}],
        "address_terms": [{"term": "分析员", "support_level": "observed", "evidence_document_ids": ["doc-a"]}],
        "sentence_style": {"observations": ["先回应本轮问题"], "note": "静态风格"},
        "emotion_patterns": [{"quote": "早上好。", "title": "2026-10-01", "document_id": "doc-a"}],
        "narrative_evolution": {
            "latest_state_evidence": [
                {"quote": "较早证据", "title": "2025-10-01", "document_id": "doc-b"},
                {"quote": "最新证据", "title": "2026-10-01", "document_id": "doc-a"},
            ],
        },
    }


def test_static_profile_cache_is_byte_equivalent_and_detached(isolated_dialogue, monkeypatch):
    service, character = isolated_dialogue
    profile = static_profile()
    expected = MVPService._dialogue_profile_prompt_context(profile)
    project = Mock(wraps=MVPService._dialogue_profile_prompt_context)
    monkeypatch.setattr(service, "_dialogue_profile_prompt_context", project)
    first = service._cached_dialogue_profile_prompt_context(character.character_id, profile)
    assert json.dumps(first, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    first["identity_evidence"][0]["statement"] = "本请求的修改"
    first["narrative_evolution"]["latest_state_evidence"].clear()
    second = service._cached_dialogue_profile_prompt_context(character.character_id, profile)
    assert json.dumps(second, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)
    context = {"hits": [], "provisional_relations": [], "dialogue_profile": profile,
               "session_context": {"turns": [{"user": "本轮独立会话", "assistant": "仅留本请求"}]}}
    prompt = json.loads(service._prompt(character, "你好", context))
    assert prompt["dialogue_style_profile"] == expected
    assert prompt["conversation_context"] == context["session_context"]
    assert project.call_count == 1
    assert profile == static_profile()


def test_static_profile_cache_fences_version_file_fingerprint_revision_and_character(isolated_dialogue, monkeypatch):
    service, character = isolated_dialogue
    service.dialogue_profiles_path.parent.mkdir(parents=True, exist_ok=True)
    profile = {**static_profile(), "character_id": character.character_id}
    service.dialogue_profiles_path.write_text(json.dumps(profile, ensure_ascii=False) + "\n", encoding="utf-8")
    version = SimpleNamespace(data_version="fixture-v1")
    monkeypatch.setattr(service.repository, "public_settings", version, raising=False)
    project = Mock(wraps=MVPService._dialogue_profile_prompt_context)
    monkeypatch.setattr(service, "_dialogue_profile_prompt_context", project)
    loaded = service._dialogue_profiles()[character.character_id]
    service._cached_dialogue_profile_prompt_context(character.character_id, loaded)
    service._cached_dialogue_profile_prompt_context(character.character_id, loaded)
    assert project.call_count == 1
    version.data_version = "fixture-v2"
    service._cached_dialogue_profile_prompt_context(character.character_id, loaded)
    assert project.call_count == 2
    stamp = service.dialogue_profiles_path.stat()
    changed = {**profile, "state_policy_note": "更新的资料"}
    service.dialogue_profiles_path.write_text(json.dumps(changed, ensure_ascii=False) + "\n", encoding="utf-8")
    os.utime(service.dialogue_profiles_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    reloaded = service._dialogue_profiles()[character.character_id]
    assert service._cached_dialogue_profile_prompt_context(character.character_id, reloaded)["state_policy_note"] == "更新的资料"
    assert project.call_count == 3
    # An in-memory edit is fenced even when the runtime file timestamp is fixed.
    reloaded["state_policy_note"] = "相同文件版本的输入变更"
    assert service._cached_dialogue_profile_prompt_context(character.character_id, reloaded)["state_policy_note"] == "相同文件版本的输入变更"
    assert project.call_count == 4
    monkeypatch.setattr(service, "_PROFILE_PROMPT_REVISION", "fixture-projection-v2")
    service._cached_dialogue_profile_prompt_context(character.character_id, reloaded)
    service._cached_dialogue_profile_prompt_context("another-character", reloaded)
    assert project.call_count == 6
    stamp = service.dialogue_profiles_path.stat()
    os.utime(service.dialogue_profiles_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    service._cached_dialogue_profile_prompt_context(character.character_id, reloaded)
    assert project.call_count == 7


def test_static_profile_cache_is_bounded_lru(isolated_dialogue, monkeypatch):
    service, _character = isolated_dialogue
    monkeypatch.setattr(service, "_PROFILE_PROMPT_CACHE_LIMIT", 2)
    project = Mock(wraps=MVPService._dialogue_profile_prompt_context)
    monkeypatch.setattr(service, "_dialogue_profile_prompt_context", project)
    profile = static_profile()
    for character_id in ("a", "b", "a", "c", "a"):
        service._cached_dialogue_profile_prompt_context(character_id, profile)
    assert len(service._profile_prompt_cache) == 2
    assert project.call_count == 3
    service._cached_dialogue_profile_prompt_context("b", profile)
    assert len(service._profile_prompt_cache) == 2
    assert project.call_count == 4


def test_prompt_exact_evidence_dedup_preserves_provenance_order_history_and_guards(isolated_dialogue, monkeypatch):
    service, character = isolated_dialogue
    def hit(document_id="doc-a", *, source="character_voice", title="合成资料", text="早上好。", costume=False):
        return {"citation": {"document_id": document_id, "source_type": source, "title": title},
                "text": text, "metadata": {"requires_costume_context": costume}}
    first = hit()
    distinct = [hit(text="同文档的另一片段。"), hit("doc-b"), hit(source="character_story"),
                hit(costume=True), hit(title="另一个来源标题")]
    context = {"character": character, "question_focus": "casual_check_in", "conversation_mode": "natural_chat",
               "hits": [first, copy.deepcopy(first), *distinct], "provisional_relations": [],
               "session_context": {"turns": [{"user": "先前问题", "assistant": "完整保留的回答"}]},
               "continuity_card": {"has_prior_turn": True, "previous_character_reply": "完整保留的回答"}}
    original = copy.deepcopy(context)
    blocks = [{"type": "message", "text": "早上好。"}]
    before_guard = service._answer_guardrail_violations("早上好", "早上好。", context, "immersive", blocks)
    projected = json.loads(service._prompt(character, "早上好", context))
    assert len(projected["evidence"]) == 6
    assert [(row["document_id"], row["source_type"], row["title"], row["text"]) for row in projected["evidence"]] == [
        (row["citation"]["document_id"], row["citation"]["source_type"], row["citation"]["title"], row["text"])
        for row in [first, *distinct]
    ]
    assert projected["evidence"][4]["narrative_scope"] == "costume_specific"
    assert projected["conversation_context"] == original["session_context"]
    assert projected["continuity_card"] == original["continuity_card"]
    assert context == original
    assert service._answer_guardrail_violations("早上好", "早上好。", context, "immersive", blocks) == before_guard


def test_duplicate_evidence_does_not_spend_prompt_budget_twice(isolated_dialogue):
    service, character = isolated_dialogue
    first = {"citation": {"document_id": "doc-a", "title": "合成资料", "source_type": "character_voice"}, "text": "甲" * 1200}
    second = {"citation": {"document_id": "doc-b", "title": "另一来源", "source_type": "character_voice"}, "text": "不可丢失的独立证据。"}
    context = {"question_focus": "casual_check_in", "hits": [first] * 5 + [second], "provisional_relations": []}
    projected = json.loads(service._prompt(character, "早上好", context))
    assert [(row["document_id"], row["text"]) for row in projected["evidence"]] == [
        ("doc-a", first["text"]), ("doc-b", second["text"]),
    ]
