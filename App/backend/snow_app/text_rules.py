"""Pure text matching shared by dialogue policies."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def _compact(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-·・,，。！？!?、:：;；'\"“”‘’()（）\[\]【】<>《》「」『』]+", "", normalized)


def _contains_term(value: Any, term: str) -> bool:
    """Match compact CJK phrases without finding API/RAG inside English words."""

    normalized = _compact(value)
    needle = _compact(term)
    if not needle:
        return False
    if needle.isascii() and needle.isalpha():
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", normalized) is not None
    return needle in normalized
