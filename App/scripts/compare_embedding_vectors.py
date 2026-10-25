"""Prove a rebuilt embedding image returns the same vectors as the released one.

Stdlib only, so it runs in the first CI job before the application environment
is installed. Two modes:

  payload
      Print the fixed ``/embed`` request body as JSON. The sample set is a
      representative cross-section of Project Snow traffic (character names,
      short dialogue lines, base place names, lore words, a long paragraph, an
      English sentence and punctuation/emoji edge cases). Feed it to the old and
      the new container identically.

  compare OLD.json NEW.json [--min-cosine 0.99999]
      Both files are ``/embed`` responses of the shape produced by
      ``App/infra/embedding_service.py`` -- ``{"vectors": [[...512 floats]...]}``.
      Require the same vector count, 512 dimensions, finite values and a
      per-pair cosine similarity at or above ``--min-cosine``. Print one JSON
      summary and exit non-zero with a clear message on any mismatch.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

EXPECTED_DIMENSION = 512
DEFAULT_MIN_COSINE = 0.99999

# A fixed, representative sample of Project Snow embedding traffic. Keep it here
# so the old and new containers are always queried with exactly the same inputs.
SAMPLE_TEXTS: tuple[str, ...] = (
    # Character names.
    "里芙",
    "芬妮",
    "凯茜娅",
    "苔丝",
    "猫汐尔",
    # Short dialogue lines.
    "你今天过得怎么样？",
    "我在楼下等你，别着急。",
    "这件事我们明天再谈吧。",
    "谢谢你一直陪着我。",
    # Base place names.
    "静默研究所",
    "北境哨站",
    "旧城图书馆",
    # Lore words.
    "回响",
    "共鸣协议",
    "档案守门人",
    "湮灭纪元",
    # A long paragraph.
    "在漫长的湮灭纪元之后，幸存者们在旧城图书馆的地下重新建立了档案系统，"
    "他们相信只要记忆还在被人讲述，那些逝去的名字就不会真正消失；"
    "于是每到共鸣协议开启的夜晚，守门人会点亮回响之灯，让远方的人也能听见。",
    # An English sentence.
    "The analyst reviewed every rendezvous log before the schedule refreshed at midnight.",
    # Punctuation / emoji edge cases.
    "……？！",
    "“引号”与—破折号——的处理",
    "下雪了 ❄️，我们一起堆雪人吧 ⛄",
    "mixed 中英文 text with 123 numbers",
)


def payload() -> str:
    return json.dumps({"inputs": list(SAMPLE_TEXTS)}, ensure_ascii=False)


def _load_vectors(path: Path) -> list[list[float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("vectors"), list):
        raise ValueError(f"{path}: expected an /embed response with a 'vectors' list")
    vectors: list[list[float]] = []
    for index, row in enumerate(data["vectors"]):
        if not isinstance(row, list):
            raise ValueError(f"{path}: vector {index} is not a list")
        vectors.append([float(value) for value in row])
    return vectors


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("a zero-magnitude vector cannot be compared by cosine")
    return dot / (left_norm * right_norm)


def compare(old_path: Path, new_path: Path, min_cosine: float) -> dict[str, object]:
    old = _load_vectors(old_path)
    new = _load_vectors(new_path)

    if len(old) != len(new):
        raise ValueError(f"vector count differs: old={len(old)} new={len(new)}")
    if not old:
        raise ValueError("no vectors to compare")

    min_observed = math.inf
    max_abs_diff = 0.0
    for index, (old_vec, new_vec) in enumerate(zip(old, new)):
        for vec, label in ((old_vec, "old"), (new_vec, "new")):
            if len(vec) != EXPECTED_DIMENSION:
                raise ValueError(
                    f"vector {index} ({label}) has {len(vec)} dims, expected {EXPECTED_DIMENSION}"
                )
            if not all(math.isfinite(value) for value in vec):
                raise ValueError(f"vector {index} ({label}) has a non-finite value")
        cosine = _cosine(old_vec, new_vec)
        min_observed = min(min_observed, cosine)
        max_abs_diff = max(max_abs_diff, max(abs(a - b) for a, b in zip(old_vec, new_vec)))
        if cosine < min_cosine:
            raise ValueError(
                f"vector {index} cosine {cosine:.8f} below required {min_cosine:.8f}"
            )

    return {
        "count": len(old),
        "dims": EXPECTED_DIMENSION,
        "min_cosine": min_observed,
        "max_abs_diff": max_abs_diff,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("payload", help="Print the fixed /embed request JSON.")
    compare_parser = sub.add_parser("compare", help="Compare two /embed responses.")
    compare_parser.add_argument("old", type=Path, help="/embed response from the released image")
    compare_parser.add_argument("new", type=Path, help="/embed response from the rebuilt image")
    compare_parser.add_argument("--min-cosine", type=float, default=DEFAULT_MIN_COSINE)
    args = parser.parse_args(argv)

    if args.command == "payload":
        print(payload())
        return 0

    try:
        summary = compare(args.old, args.new, args.min_cosine)
    except ValueError as error:
        print(f"embedding vectors differ: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
