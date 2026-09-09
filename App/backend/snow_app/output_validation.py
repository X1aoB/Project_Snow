"""Pure model-envelope parsing and final renderable-text normalization.

These helpers intentionally have no model, session, database or filesystem
access. Legacy imports from mvp_service remain available during migration.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_MODEL_ENVELOPE_KEYS = frozenset(
    {
        "answer",
        "content_blocks",
        "stage_motion",
        "expression_state",
        "performance_id",
        "confidence",
        "used_document_ids",
        "narrative_scope",
        "work_summary",
        "work_steps",
        "analysis_process",
    }
)


def _looks_like_model_envelope(value: Any) -> bool:
    return isinstance(value, dict) and "answer" in value and bool(_MODEL_ENVELOPE_KEYS.intersection(value))


def _decode_json_candidate(text: str) -> Any:
    """Decode a JSON value even when a gateway adds prose or a code fence."""

    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    # ``raw_decode`` handles nested braces and quoted braces correctly; the
    # old first/last-brace slice could swallow two adjacent JSON objects and
    # leak the whole string as visible dialogue.
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        return value
    return None


def _extract_partial_model_answer(text: str) -> str | None:
    """Extract a closed ``answer`` string from a truncated JSON envelope.

    OpenAI-compatible gateways occasionally terminate a response after the
    answer value (for example ``{"answer":"...","``).  Treating that raw
    fragment as dialogue leaks implementation syntax to the user.  We only
    accept a complete JSON string value; an incomplete value returns ``None``
    so the caller can use the channel-safe deterministic fallback.
    """

    cleaned = str(text or "").strip()
    if not cleaned or "answer" not in cleaned:
        return None
    match = re.search(r"[\"']answer[\"']\s*:\s*", cleaned, flags=re.IGNORECASE)
    if not match:
        return None
    value_text = cleaned[match.end() :].lstrip()
    if not value_text or value_text[0] not in {'"', "'"}:
        return None
    quote = value_text[0]
    decoder = json.JSONDecoder()
    try:
        if quote == '"':
            value, _ = decoder.raw_decode(value_text)
        else:
            # A few gateways fall back to Python/JavaScript-style single
            # quotes when JSON mode is rejected.  ``literal_eval`` parses only
            # literals (never executes provider text), and is restricted to
            # the bounded first string token below.
            end = None
            escaped = False
            for index in range(1, len(value_text)):
                char = value_text[index]
                if escaped:
                    escaped = False
                    continue
                if char == "\\":
                    escaped = True
                    continue
                if char == quote:
                    end = index + 1
                    break
            if end is None:
                raise ValueError("unterminated single-quoted answer")
            value = ast.literal_eval(value_text[:end])
    except (json.JSONDecodeError, ValueError, SyntaxError):
        # ``raw_decode`` rejects a malformed trailing envelope, but the string
        # itself may still be closed.  Find the first unescaped quote and
        # decode only that bounded JSON string.
        escaped = False
        for index in range(1, len(value_text)):
            char = value_text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char != '"':
                continue
            try:
                value = (
                    json.loads(value_text[: index + 1])
                    if quote == '"'
                    else ast.literal_eval(value_text[: index + 1])
                )
            except (json.JSONDecodeError, ValueError, SyntaxError):
                return None
            break
        else:
            return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _looks_like_structured_fragment(text: str) -> bool:
    """Return whether a string is likely an unrenderable JSON envelope."""

    cleaned = str(text or "").lstrip()
    lowered = cleaned.casefold()
    if not cleaned.startswith(("{", "[")):
        return False
    return bool(
        re.search(
            r"[\"'](?:answer|content_blocks|confidence)[\"']\s*:",
            lowered,
            flags=re.IGNORECASE,
        )
    )


def _clean_renderable_text(text: Any) -> str:
    """Return only user-renderable text from a provider response fragment.

    Compatible endpoints are not completely consistent about JSON mode.  In
    addition to a normal object they may return a JSON *string* containing an
    object, a fenced object, or a response which was cut off immediately after
    the ``answer`` value.  The latter two forms used to bypass the parser when
    the outer value was a quoted string and consequently appeared in the chat
    bubble as ``{"answer": ...``.  This helper is deliberately independent of
    the model envelope parser and is used again immediately before rendering,
    so a future provider/parser regression cannot expose implementation syntax.

    An empty return means the fragment was recognised as a malformed internal
    envelope and must be replaced by the normal channel-safe fallback.  Plain
    user-facing JSON which has no envelope keys is preserved as ordinary text.
    """

    # Provider fields are expected to be strings.  Converting mappings/lists
    # with ``str(...)`` would expose Python implementation syntax (for
    # example ``{'text': '...'}'``) in a chat bubble when a gateway returns a
    # schema-invalid answer.  Reject non-string fragments and let the caller
    # select its deterministic, channel-safe fallback instead.
    if not isinstance(text, str):
        return ""
    candidate = text.strip()
    if not candidate:
        return ""
    # Remove a markdown fence before probing for a truncated envelope.  JSON
    # mode is meant to avoid fences, but some gateways add them anyway.
    candidate = re.sub(r"^```(?:json|JSON)?\s*", "", candidate).strip()
    candidate = re.sub(r"\s*```$", "", candidate).strip()
    seen: set[str] = set()
    for _ in range(5):
        if not candidate or candidate in seen:
            break
        seen.add(candidate)
        decoded = _decode_json_candidate(candidate)
        if isinstance(decoded, dict):
            if "answer" in decoded:
                nested_answer = decoded.get("answer")
                if isinstance(nested_answer, str) and nested_answer.strip() != candidate:
                    candidate = nested_answer.strip()
                    continue
                # A valid envelope with an empty answer may still contain
                # renderable blocks.  Ignore metadata and use their text only.
                block_items = decoded.get("content_blocks")
                if isinstance(block_items, list):
                    block_text = "\n".join(
                        str(item.get("text") or "").strip()
                        for item in block_items
                        if isinstance(item, dict) and str(item.get("text") or "").strip()
                    ).strip()
                    if block_text:
                        candidate = block_text
                        continue
                return ""
            # An object without an answer key is not necessarily an internal
            # envelope (the assistant may have been asked for JSON), therefore
            # leave it untouched.
            return candidate
        if isinstance(decoded, str) and decoded.strip() != candidate:
            # This handles a gateway that JSON-encodes the whole response once
            # more.  Continue so a nested/truncated envelope can be inspected.
            candidate = decoded.strip()
            continue
        structured_probe = candidate
        if not structured_probe.startswith(("{", "[")):
            # A short gateway preamble (for example ``Here is the JSON:``)
            # should not make the bounded answer extractor miss the envelope.
            starts = [
                index for index in (structured_probe.find("{"), structured_probe.find("[")) if index >= 0
            ]
            if starts:
                possible = structured_probe[min(starts) :]
                if _looks_like_structured_fragment(possible):
                    structured_probe = possible
        partial = _extract_partial_model_answer(structured_probe)
        if partial is not None and _looks_like_structured_fragment(structured_probe):
            return partial
        if _looks_like_structured_fragment(structured_probe):
            return ""
        # A JSON-encoded plain string has already been unwrapped above; the
        # remaining text is ordinary dialogue.
        return candidate
    return "" if _looks_like_structured_fragment(candidate) else candidate


def _parse_model_json(content: str) -> dict[str, Any]:
    value: Any = _decode_json_candidate(content)
    # Gateways occasionally wrap the entire envelope in a JSON string.  Keep
    # unwrapping bounded; never recursively decode arbitrary user text.
    for _ in range(4):
        if not isinstance(value, str):
            break
        nested = _decode_json_candidate(value)
        if nested is None or nested == value:
            break
        value = nested
    if not isinstance(value, dict):
        # Use the decoded string (when present), not the outer quoted JSON,
        # when looking for a truncated answer value.
        answer = _clean_renderable_text(value if isinstance(value, str) else content)
        return {"answer": answer, "confidence": "low", "used_document_ids": []}

    # Some compatible gateways JSON-encode the envelope once more and put it
    # in ``answer``.  Unwrap only a genuine envelope; a user-facing answer
    # that happens to start with ``{`` must remain ordinary text.
    for _ in range(3):
        nested_text = value.get("answer")
        if not isinstance(nested_text, str):
            break
        nested_value = _decode_json_candidate(nested_text)
        if not _looks_like_model_envelope(nested_value):
            break
        value = {**value, **nested_value}
    answer_value = value.get("answer")
    if isinstance(answer_value, str):
        value = {**value, "answer": _clean_renderable_text(answer_value)}
    return value
