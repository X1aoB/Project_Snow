"""Compare isolated relation-extraction benchmark runs without choosing a winner automatically."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .benchmark_relation_extraction import _benchmark_root, _safe_name
from .common import read_jsonl, utc_now, write_json


def _triples(path: Path) -> set[tuple[str, str, str]]:
    return {
        (
            str(row.get("subject", "")).strip(),
            str(row.get("relation_type", "")).strip(),
            str(row.get("object", "")).strip(),
        )
        for row in read_jsonl(path)
    }


def compare(sample_id: str, run_names: list[str]) -> dict[str, Any]:
    root = _benchmark_root(sample_id)
    if len(run_names) < 2:
        raise ValueError("At least two benchmark run names are required.")
    summaries: dict[str, dict[str, Any]] = {}
    triples: dict[str, set[tuple[str, str, str]]] = {}
    for run_name in run_names:
        run_name = _safe_name(run_name, "run name")
        summary_path = root / f"{run_name}.summary.json"
        candidates_path = root / f"{run_name}.candidates.jsonl"
        if not summary_path.exists() or not candidates_path.exists():
            raise FileNotFoundError(f"Benchmark run '{run_name}' is missing under {root}.")
        summaries[run_name] = json.loads(summary_path.read_text(encoding="utf-8"))
        triples[run_name] = _triples(candidates_path)
    overlap: dict[str, dict[str, Any]] = {}
    for left_name, left in triples.items():
        for right_name, right in triples.items():
            if left_name >= right_name:
                continue
            shared = left & right
            union = left | right
            overlap[f"{left_name}__{right_name}"] = {
                "shared_triples": len(shared),
                "jaccard": round(len(shared) / len(union), 3) if union else 1.0,
                "only_left": len(left - right),
                "only_right": len(right - left),
            }
    report = {
        "sample_id": sample_id,
        "generated_at": utc_now(),
        "runs": summaries,
        "triple_overlap": overlap,
        "decision_rule": "Use this report for cost, latency, schema reliability, and candidate-overlap comparison; choose quality only after evidence review of the disputed triples.",
    }
    write_json(root / "comparison.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-id", default="relation-extraction-10")
    parser.add_argument("--runs", nargs="+", required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.sample_id, args.runs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
