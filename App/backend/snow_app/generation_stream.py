"""Request-local generation observation and strict, bounded upstream streaming.

Observers never receive hidden reasoning. A visible prefix is committed only
after both the dialogue validator and the public boundary accept it; the
boundary callback must durably save the cumulative blocks before returning True.
"""

from __future__ import annotations

import copy
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator


class GenerationStreamError(RuntimeError):
    """An upstream stream did not finish with a valid, complete response."""


class GenerationStreamPartialError(GenerationStreamError):
    """A committed prefix must survive a later generation/validation failure."""

    def __init__(self, code: str, blocks: list[dict[str, Any]], usage: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.committed_blocks = copy.deepcopy(blocks)
        self.usage = dict(usage or {})


class GenerationPublicationError(RuntimeError):
    """Publication failed after an upstream call; its result remains uncertain."""

    def __init__(self, blocks: list[dict[str, Any]], usage: dict[str, Any] | None = None):
        super().__init__("generation_publication_failed")
        self.committed_blocks = copy.deepcopy(blocks)
        self.usage = copy.deepcopy(usage or {})


@dataclass
class GenerationObserver:
    on_phase: Callable[[str, int], None] | None = None
    on_verified_blocks: Callable[[list[dict[str, Any]]], bool] | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)
    published_blocks: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)

    def phase(self, name: str, elapsed_ms: int = 0) -> None:
        self.timings_ms[name] = max(0, int(elapsed_ms))
        if self.on_phase is not None:
            # Telemetry is not allowed to trigger another paid request.
            try:
                self.on_phase(name, self.timings_ms[name])
            except Exception:
                pass

    def publish(self, blocks: list[dict[str, Any]]) -> bool:
        candidate = copy.deepcopy(blocks)
        if candidate[:len(self.published_blocks)] != self.published_blocks:
            self.fail("published_prefix_changed")
        if len(candidate) <= len(self.published_blocks) or self.on_verified_blocks is None:
            return False
        try:
            committed = self.on_verified_blocks(candidate)
        except GenerationPublicationError:
            raise
        except Exception as exc:
            # A timed-out/lost ACK may already represent a durable write. Keep
            # the paid-call diagnostics and let the store reconcile this UUID.
            raise GenerationPublicationError(self.published_blocks, self.usage) from exc
        if committed is not True:
            return False
        first = not self.published_blocks
        self.published_blocks = candidate
        if first:
            self.phase("first_verified_block_ms", int((time.perf_counter() - self.started_at) * 1000))
        return True

    def fail(self, code: str) -> None:
        raise GenerationStreamPartialError(code, self.published_blocks, self.usage)

    def ensure_prefix(self, blocks: list[dict[str, Any]]) -> None:
        if self.published_blocks and blocks[:len(self.published_blocks)] != self.published_blocks:
            self.fail("published_prefix_changed")


_OBSERVER: ContextVar[GenerationObserver | None] = ContextVar("snow_generation_observer", default=None)
_BLOCK_VALIDATOR: ContextVar[Callable[[list[dict[str, Any]]], None] | None] = ContextVar(
    "snow_generation_block_validator", default=None
)


def current_generation_observer() -> GenerationObserver | None:
    return _OBSERVER.get()


@contextmanager
def observe_generation(observer: GenerationObserver) -> Iterator[GenerationObserver]:
    token = _OBSERVER.set(observer)
    try:
        yield observer
    finally:
        _OBSERVER.reset(token)


@contextmanager
def validate_generation_blocks(callback: Callable[[list[dict[str, Any]]], None] | None):
    token = _BLOCK_VALIDATOR.set(callback)
    try:
        yield
    finally:
        _BLOCK_VALIDATOR.reset(token)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GenerationStreamError("duplicate_json_key")
        result[key] = value
    return result


class CompleteBlockParser:
    """Read complete blocks from a compact envelope without repairing JSON.

The state proposal must precede the last, content_blocks field and be empty.
Any other ordering remains eligible for ordinary full-response validation only.
Incomplete strings, fenced JSON and nested envelopes are never early output.
"""

    _HEADERS = frozenset({
        "state_updates", "expression_state", "stage_motion", "performance_id",
        "confidence", "narrative_scope", "used_document_ids",
        "used_relation_candidate_ids", "uncertainties", "citation_notes",
    })

    def __init__(self, callback: Callable[[list[dict[str, Any]]], None], max_chars: int = 131072):
        self.callback = callback
        self.max_chars = max_chars
        self.text = ""
        self.delivered = 0
        self.disabled = False
        self.decoder = json.JSONDecoder(object_pairs_hook=_unique_object)

    def _disable(self) -> None:
        self.disabled = True
        observer = current_generation_observer()
        if observer is not None and observer.published_blocks:
            raise GenerationStreamError("stream_envelope_changed")

    def feed(self, delta: str) -> None:
        self.text += delta
        if len(self.text) > self.max_chars:
            raise GenerationStreamError("stream_response_too_large")
        if self.disabled:
            return
        source = self.text
        pos = len(source) - len(source.lstrip())
        if pos == len(source):
            return
        if source[pos] != "{":
            self._disable()
            return
        pos += 1
        headers: dict[str, Any] = {}
        try:
            while True:
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                key, pos = self.decoder.raw_decode(source, pos)
                if not isinstance(key, str) or key in headers:
                    self._disable()
                    return
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                if pos == len(source):
                    return
                if source[pos] != ":":
                    self._disable()
                    return
                pos += 1
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                if key == "content_blocks":
                    if (headers.get("state_updates") != []
                            or headers.get("stage_motion") != "none"
                            or headers.get("performance_id") != ""):
                        self._disable()
                        return
                    break
                if key not in self._HEADERS:
                    self._disable()
                    return
                headers[key], pos = self.decoder.raw_decode(source, pos)
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                if pos == len(source):
                    return
                if source[pos] != ",":
                    self._disable()
                    return
                pos += 1
            if pos == len(source):
                return
            if source[pos] != "[":
                self._disable()
                return
            pos += 1
            blocks: list[dict[str, Any]] = []
            while True:
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                if pos == len(source):
                    return
                if source[pos] == "]":
                    tail = source[pos + 1:].strip()
                    if tail and tail != "}":
                        self._disable()
                    return
                block, end = self.decoder.raw_decode(source, pos)
                if not isinstance(block, dict) or set(block) != {"type", "text"}:
                    self._disable()
                    return
                text = block.get("text")
                if block.get("type") not in {"message", "speech", "action"} or not isinstance(text, str) or not text.strip():
                    self._disable()
                    return
                text.encode("utf-8", errors="strict")
                blocks.append(block)
                if len(blocks) > 8:
                    self._disable()
                    return
                pos = end
                while pos < len(source) and source[pos].isspace():
                    pos += 1
                if pos == len(source):
                    return
                if source[pos] not in {",", "]"}:
                    self._disable()
                    return
                if len(blocks) > self.delivered:
                    self.callback(copy.deepcopy(blocks))
                    self.delivered = len(blocks)
                if source[pos] == "]":
                    tail = source[pos + 1:].strip()
                    if tail and tail != "}":
                        self._disable()
                    return
                pos += 1
        except json.JSONDecodeError:
            # The next transport fragment may complete the current JSON token.
            return
        except UnicodeError as exc:
            raise GenerationStreamError("invalid_stream_unicode") from exc


def read_completion_stream(response: Any, *, started_at: float | None = None) -> tuple[str, dict[str, Any]]:
    """Consume real chat-completion SSE once; never restart an uncertain call."""
    observer = current_generation_observer()
    validator = _BLOCK_VALIDATOR.get()
    parser = CompleteBlockParser(validator) if validator is not None else None
    started = started_at if started_at is not None else time.perf_counter()
    fragments: list[str] = []
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    data_lines: list[str] = []
    received_content = False
    finished = False
    response_chars = 0

    def consume(data: str) -> None:
        nonlocal received_content, finish_reason, finished, response_chars, usage
        if data == "[DONE]":
            finished = True
            return
        try:
            payload = json.loads(data)
        except (ValueError, TypeError) as exc:
            raise GenerationStreamError("invalid_provider_sse") from exc
        if not isinstance(payload, dict) or payload.get("error"):
            raise GenerationStreamError("provider_stream_error")
        if isinstance(payload.get("usage"), dict):
            usage = dict(payload["usage"])
            if observer:
                observer.usage = usage
        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            if choice.get("finish_reason") is not None:
                finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            content = delta.get("content") if isinstance(delta, dict) else None
            if content is None:
                continue
            if not isinstance(content, str):
                raise GenerationStreamError("invalid_provider_delta")
            if content and not received_content:
                received_content = True
                if observer and "provider_first_content_ms" not in observer.timings_ms:
                    observer.phase("provider_first_content_ms", int((time.perf_counter() - started) * 1000))
            response_chars += len(content)
            if response_chars > 131072:
                raise GenerationStreamError("stream_response_too_large")
            fragments.append(content)
            if parser:
                parser.feed(content)

    for line in response.iter_lines():
        if not isinstance(line, str) or len(line) > 262144:
            raise GenerationStreamError("invalid_provider_sse_line")
        if line == "":
            if data_lines:
                consume("\n".join(data_lines))
                data_lines = []
            if finished:
                break
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
            if sum(map(len, data_lines)) > 262144:
                raise GenerationStreamError("provider_sse_event_too_large")
    if data_lines and not finished:
        consume("\n".join(data_lines))
    if finish_reason not in {"stop"}:
        raise GenerationStreamError("provider_stream_incomplete")
    content = "".join(fragments)
    if parser and observer is not None and observer.published_blocks:
        try:
            final = json.loads(content, object_pairs_hook=_unique_object)
        except (ValueError, TypeError) as exc:
            raise GenerationStreamError("stream_envelope_incomplete") from exc
        if not isinstance(final, dict) or final.get("state_updates") != [] or list(final)[-1:] != ["content_blocks"]:
            raise GenerationStreamError("stream_envelope_changed")
    return content, usage
