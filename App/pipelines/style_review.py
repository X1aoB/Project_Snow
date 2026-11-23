"""Create a small, source-linked human review queue and legacy-rule comparison."""

import argparse
from math import ceil
from pathlib import Path

from backend.snow_app.mvp_policy import canonical_mvp_character
from backend.snow_app.mvp_service import _FENNY_HARSH_DAILY_MARKERS

from .common import RUNTIME_ROOT, read_jsonl
from .style_audit import table
from .style_mining import candidate_digest, interval


def report(style, out, include_all=False):
    lines = list(read_jsonl(style / "character_lines.jsonl"))
    signatures = list(read_jsonl(style / "signature_terms.jsonl"))
    register = list(read_jsonl(style / "register_terms.jsonl"))
    topics = list(read_jsonl(style / "shared_topics.jsonl"))
    shortlist = []
    for cid in sorted({s["character_id"] for s in signatures}):
        # Include one high-score unclassified term per character so that a
        # conservative first pass cannot silently discard unknown expressions.
        for eligible in (True, False):
            pool = [
                s for s in signatures if s["character_id"] == cid and (s["category"] != "topic") == eligible
            ]
            if pool:
                s = max(pool, key=lambda s: s["z"])
                shortlist.append((s, "signature", s["category"]))
        pool = [s for s in topics if s["character_id"] == cid]
        if pool:
            shortlist.append((max(pool, key=lambda s: s["count"]), "shared_topic", "topic"))
    shortlist.extend((s, "register", "high_stakes") for s in register)
    if include_all:
        shortlist = [(s, "signature", s["category"]) for s in signatures]
        shortlist.extend((s, "register", "high_stakes") for s in register)
        shortlist.extend((s, "shared_topic", "topic") for s in topics)
    out.mkdir(parents=True, exist_ok=True)
    # The output may already contain a maintainer's unmerged decisions.
    if (out / "review.csv").exists():
        raise FileExistsError("Use a fresh directory to preserve the existing review queue")
    table(
        out / "review.csv",
        [
            dict(
                character=canonical_mvp_character(s["character_id"]).display_name,
                character_id=s["character_id"],
                table=kind,
                term=s["term"],
                proposed_category=cat,
                count=s["count"],
                other_character_id=s.get("other_character_id"),
                page_id=(s.get("evidence") or [s])[0]["page_id"],
                document_id=(s.get("evidence") or [s])[0]["document_id"],
                review_status=s["review_status"],
                candidate_sha256=candidate_digest(s),
                decision="",
                reason="",
            )
            for s, kind, cat in shortlist
        ],
    )
    comparison = []
    for name, terms in (("卜卜", ("算卦", "卦象", "运势", "本天师")), ("芬妮", _FENNY_HARSH_DAILY_MARKERS)):
        cid = canonical_mvp_character(name).character_id
        rows = [
            r
            for r in lines
            if r["character_id"] == cid
            and r["split"] == "train"
            and r["spoken"]
            and r["source_type"] != "character_voice"
        ]
        daily = [r for r in rows if r["source_type"] in {"random_event", "affinity_story"}]
        high = [r for r in rows if r["high_stakes"]]
        for term in terms:
            hits = [r for r in rows if term in r["text"]]
            pool = signatures if name == "卜卜" else register
            status = next(
                (r["review_status"] for r in pool if r["character_id"] == cid and r["term"] == term),
                "未进入候选表",
            )
            comparison.append(
                dict(
                    character=name,
                    term=term,
                    story_matches=len(hits),
                    story_lines=len(rows),
                    rate=len(hits) / len(rows),
                    interval95=interval(len(hits), len(rows)),
                    daily_matches=sum(term in r["text"] for r in daily),
                    daily_lines=len(daily),
                    high_matches=sum(term in r["text"] for r in high),
                    high_lines=len(high),
                    eligible_table_status=status,
                    example_page=hits[0]["page_id"] if hits else "",
                    example_document=hits[0]["document_id"] if hits else "",
                    six_turn_cap_if_approved=(
                        max(1, ceil(6 * len(hits) / len(rows))) if name == "卜卜" else None
                    ),
                )
            )
    table(out / "legacy-comparison.csv", comparison)
    md = [
        "# 首批风格审阅条目",
        "",
        "review.csv 列出候选词、来源和待填的审阅意见。优先核对分类、说话人、同场证据与适用语境。",
        "在 decision 中填写 approved 或 rejected，并在 reason 写下依据。留空表示继续待审。"
        "可修订标志词的 proposed_category。"
        "使用 style_corpus --decisions review.csv 在独立输出目录生成审阅后的版本。",
        "legacy-comparison.csv 列出既有规则涉及的 15 个表达。分母为训练页面中去重且通过口语筛选的台词。",
        "卜卜的原规则会在最近三轮用过任一标志词后限制再次使用这组词。"
        "数据模式只统计同一个已批准用语，默认含本轮在内观察六轮。表中上限仅供审阅。",
        "芬妮的原规则在日常语境匹配固定命令词。数据模式按角色和已批准的高强度限定词触发。",
        "本次新挖掘词条均待人工审阅。表中频率未用于生产校准。",
    ]
    (out / "review.md").write_text("\n".join(md) + "\n", encoding="utf8")
    return comparison


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--style", type=Path, default=RUNTIME_ROOT / "style/1.4.0")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--all", action="store_true", help="Export all candidates for review")
    a = p.parse_args()
    report(a.style, a.out, a.all)
