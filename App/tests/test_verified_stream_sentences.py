"""Publication-only sentence gates with real guards and synthetic upstream IO."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import Request

from backend.snow_app.dialogue_core import DialogueEngine, GenerationGate
from backend.snow_app.generation_stream import GenerationObserver, observe_generation
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_contracts import ChatRequest
from backend.snow_app.public_main import create_app
from backend.snow_app.public_service import PublicChatService
from backend.snow_app.public_store import PublicStore
from tests.test_generation_latency_v2 import envelope, isolated_dialogue, sse
from tests.test_public_request_recovery import settings


@pytest.mark.parametrize("text", [
    "因为", "我其实", "因为。", "我其实。", "早上好", "早上好，",
    "早上好……", "早上好...", "让我想想。", "正在输入。", "请稍等！",
    "Let me think.", "Thinking.", "Please wait.",
    "“让我想想。”", "早上好（想说的话。", "早上好”。", "早上好。\"",
])
def test_real_role_guard_does_not_publish_incomplete_or_exact_waiting_prose(isolated_dialogue, text):
    service, character = isolated_dialogue
    context = service.retrieve(character.character_id, "早上好")
    context["communication_context"] = {"channel": "text"}
    blocks = [{"type": "message", "text": text}]
    checks = ["_answer_guardrail_violations", "_repair_explicit_relationship_answer",
              "_repair_latest_state_answer", "_normalize_explicit_relationship_address",
              "_normalize_response_aliases"]
    spies = []
    for name in checks:
        spy = Mock(wraps=getattr(service, name))
        setattr(service, name, spy)
        spies.append(spy)
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    with observe_generation(observer):
        service._verify_stream_blocks(blocks, message="早上好", context=context,
                                      channel="text", character_name=character.display_name)
    assert observer.published_blocks == []
    assert all(spy.call_count == 1 for spy in spies)


@pytest.mark.parametrize("text", ["早上好。", "早上好！", "今天想聊些什么？", "我听见了。"])
@pytest.mark.parametrize("channel,block_type", [("text", "message"), ("in_person", "speech")])
def test_real_role_guard_still_publishes_complete_verbal_reply(isolated_dialogue, text, channel, block_type):
    service, character = isolated_dialogue
    context = service.retrieve(character.character_id, "早上好")
    context["communication_context"] = {"channel": channel}
    blocks = [{"type": block_type, "text": text}]
    observer = GenerationObserver(on_verified_blocks=lambda _blocks: True)
    with observe_generation(observer):
        service._verify_stream_blocks(blocks, message="早上好", context=context,
                                      channel=channel, character_name=character.display_name)
    assert observer.published_blocks == blocks


@pytest.mark.parametrize("text,ready", [
    ("“早上好。”", True), ("「早上好！」", True), ("『早上好？』", True),
    ("我听见（你的问候）了。", True), ('"Good morning!"', True), ("I'm here.", True),
    ("“早上好。", False), ("「早上好。』", False), ("我听见（你的问候]了。", False),
    ("我听见了（。", False), ('"Good morning!', False), ("。", False),
    ("让我想想，你今天想聊些什么？", True),
])
def test_delimiter_gate_is_conservative_without_rewriting_text(text, ready):
    blocks = [{"type": "speech", "text": text}]
    original = [dict(blocks[0])]
    assert MVPService._stream_verbal_blocks_complete(blocks) is ready
    assert blocks == original


@pytest.mark.parametrize("channel,block_type", [("text", "message"), ("in_person", "speech")])
@pytest.mark.parametrize("texts,early_count", [
    (("因为", "你想聊些什么？"), 0),
    (("我其实", "你想聊些什么？"), 0),
    (("让我想想。", "你想聊些什么？"), 0),
    (("正在输入。", "你想聊些什么？"), 0),
    (("早上好",), 0),
    (("早上好。",), 1),
    (("早上好。", "你想聊些什么"), 1),
])
def test_real_public_pipeline_withholds_incomplete_blocks_and_preserves_acked_prefix(
    isolated_dialogue, monkeypatch, channel, block_type, texts, early_count,
):
    """Real parser/MVP guards/public boundary/store/done/replay; no paid IO.

    In particular a legitimate unpunctuated tail after an ACKed first block
    must complete successfully, and the final transport must not resend that
    prefix as a delta. Neither normalization nor final persistence changes it.
    """
    mvp, character = isolated_dialogue
    public_settings = replace(settings(), verified_block_stream=True)
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
        "subject_binding": "subject", "analyst_location": "休息室",
        "presence": {character.character_id: {"character_id": character.character_id,
                    "character_name": character.display_name, "location": "休息室", "activity": "休息"}},
    })
    monkeypatch.setattr("backend.snow_app.public_main.open_byok_credential",
                        lambda *_args, **_kwargs: {"api_key": "synthetic"})
    store = PublicStore("sqlite+pysqlite:///:memory:")
    store.create_schema()
    payload = ChatRequest(request_id=uuid4(), provider="openai", credential="synthetic-credential-value",
                          model="gpt-4o-mini", character_id=character.character_id,
                          communication_channel=channel, message="早上好")
    raw = envelope(*texts, channel=block_type)
    if len(texts) > 1:
        prefix, tail = raw.split(',{"type":"' + block_type + '"', 1)
        prefix += ","
        tail = '{"type":"' + block_type + '"' + tail
    else:
        prefix, tail = raw[:-1], raw[-1:]
    provider_calls = []
    before_eof = []
    after_tail = []

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield sse([prefix], finish=None).replace("data: [DONE]\n\n", "").encode()
            records = store.request_blocks(str(payload.request_id), "subject")
            assert len(records) == early_count
            before_eof.extend(records)
            assert store.request_result(str(payload.request_id), "subject") is None
            yield sse([tail], finish=None).replace("data: [DONE]\n\n", "").encode()
            after_tail.extend(store.request_blocks(str(payload.request_id), "subject"))
            assert len(after_tail) == early_count
            yield sse([], usage={"total_tokens": 30}).encode()

    def handler(request):
        provider_calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

    mvp._model_http_client.close()
    mvp._model_http_client = httpx.Client(transport=httpx.MockTransport(handler))
    app = create_app(public_settings, mvp.settings, store, service)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/public/v1/chat/stream")

    def request():
        return Request({"type": "http", "headers": [(b"x-snow-stream-version", b"2")],
                        "state": {"anonymous_id": "synthetic", "subject_hash": "subject"}})

    def events(packets, name):
        return [json.loads(packet.split("data: ", 1)[1]) for packet in packets
                if packet.startswith(f"event: {name}\n")]

    async def exercise():
        try:
            response = await endpoint(request(), payload)
            packets = [packet async for packet in response.body_iterator]
            assert events(packets, "error") == []
            assert len(events(packets, "done")) == 1
            saved = store.request_result(str(payload.request_id), "subject")
            expected_blocks = [{"type": block_type, "text": text} for text in texts]
            assert saved["content_blocks"] == expected_blocks
            assert saved["answer"] == "\n".join(texts)
            assert not saved.get("terminal_error")
            committed = events(packets, "committed_block")
            assert len(committed) == early_count
            assert store.request_blocks(str(payload.request_id), "subject") == before_eof == after_tail
            expected_deltas = [{"text": text, "block_index": index, "block_type": block_type}
                               for index, text in enumerate(texts) if index >= early_count]
            assert events(packets, "delta") == expected_deltas
            if early_count:
                assert committed[0]["block"] == expected_blocks[0]
                assert all(event["block_index"] != 0 for event in events(packets, "delta"))
            replay = await endpoint(request(), payload)
            repeated = [packet async for packet in replay.body_iterator]
            assert events(repeated, "error") == []
            assert len(events(repeated, "done")) == 1
            assert len(events(repeated, "committed_block")) == early_count
            assert events(repeated, "delta") == expected_deltas
            if early_count:
                assert events(repeated, "committed_block")[0]["block_id"] == committed[0]["block_id"]
            assert len(provider_calls) == 1
            assert provider_calls[0]["stream"] is True
        finally:
            await asyncio.gather(*list(app.state.chat_jobs.values()), return_exceptions=True)
            await app.state.async_store.close()
            await app.state.provider_http.close()
            store.engine.dispose()

    asyncio.run(exercise())
