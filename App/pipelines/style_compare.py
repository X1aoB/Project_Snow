"""Compare matched style audit runs; retain every character and metric."""

import argparse
import csv
import json
from pathlib import Path

from .style_audit import table

MATCH = (
    "model",
    "seed",
    "characters",
    "probes",
    "sessions",
    "turns",
    "corpus_sha256",
    "runtime_inputs",
    "style_files",
    "probe_sha256",
    "dry_run",
    "commit",
)
# Review thresholds identify sizeable differences for inspection, not approval.
THRESHOLDS = {
    "recognition": 0.05,
    "fallback_rate": 0.05,
    "rewrite_rate": 0.05,
    "guard_rate": 0.05,
    "register_rate": 0.05,
    "shared_denial_rate": 0.05,
    "copy_14_or_60pct": 0.05,
    "copy_12_or_60pct": 0.05,
    "convergence": 0.1,
    "particle_punctuation_js": 0.05,
}


def compare(dirs, output):
    runs = [json.loads((d / "manifest.json").read_text(encoding="utf8")) for d in dirs]
    first = runs[0]
    if len(runs) < 2 or len({r["config"] for r in runs}) != len(runs):
        raise ValueError("Provide at least two distinct configurations")
    if any(
        r["status"] != "complete" or any(r.get(k) != first.get(k) for k in (*MATCH, "source_fingerprint"))
        for r in runs
    ):
        raise ValueError("Runs have incompatible inputs, commits, modes, or completion state")
    output.mkdir(parents=True, exist_ok=True)
    data = {}
    for d, r in zip(dirs, runs):
        for field in (
            "http_calls",
            "estimated_prompt_tokens",
            "estimate_calls",
            "input_tokens",
            "output_tokens",
        ):
            data.setdefault(("all", field), {})[r["config"]] = r[field]
        if first["dry_run"]:
            with (d / "turns.csv").open(encoding="utf-8-sig") as f:
                for turn in csv.DictReader(f):
                    for field in (
                        "estimated_prompt_tokens",
                        "estimate_calls",
                        "style_example_token_delta_estimate",
                    ):
                        key = (turn["character_id"], field)
                        values = data.setdefault(key, {})
                        values[r["config"]] = values.get(r["config"], 0) + int(turn.get(field) or 0)
        else:
            for filename in ("metrics.csv", "terms.csv"):
                if not (d / filename).exists():
                    continue
                with (d / filename).open(encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        for k, v in row.items():
                            if k not in {"character_id", "term", "review_status"} and v:
                                metric = row["term"] + "." + k if "term" in row else k
                                data.setdefault((row["character_id"], metric), {})[r["config"]] = float(v)
    names = [r["config"] for r in runs]
    columns = ["character_id", "metric", *names, "review"]
    md = [
        "# 风格对照",
        "",
        "输入和探针版本相同。第一组为对照基线。",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    rows = []
    for (cid, field), group in data.items():
        delta = THRESHOLDS.get(field)
        marked = (
            delta and names[0] in group and any(abs(v - group[names[0]]) >= delta for v in group.values())
        )
        row = {
            "character_id": cid,
            "metric": field,
            **{k: group.get(k) for k in names},
            "review": "复核" if marked else "",
        }
        rows.append(row)
        md.append(
            "| "
            + " | ".join(
                f"{v:.5g}" if isinstance(v, float) else str(v or (0 if v == 0 else "")) for v in row.values()
            )
            + " |"
        )
    if first["dry_run"]:
        md.extend(["", "使用合成回复进行流程估算。模型表现待实际生成后评估。"])
    else:
        md.extend(["", "复核标记阈值（绝对差值）", "", *(f"- {k} ≥ {v}" for k, v in THRESHOLDS.items())])
    table(output / "comparison.csv", rows)
    (output / "comparison.md").write_text("\n".join(md) + "\n", encoding="utf8")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("runs", type=Path, nargs="+")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    compare(args.runs, args.out)
