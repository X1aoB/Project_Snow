"""Reproduce Snow ADV's LF, separate gzip -9 first-screen release budget."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "App/docs/evidence/snow-adv-features-2-logic-size.json"
FIRST_UI = "95005fe2efade3e9c3b916e8c47c73fced5b19e0"


def content(path: str, ref: str | None = None) -> bytes:
    raw = subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=ROOT) if ref else (ROOT / path).read_bytes()
    return raw.replace(b"\r\n", b"\n")


def compressed(raw: bytes) -> int:
    return len(gzip.compress(raw, compresslevel=9, mtime=0))


def measure(files: list[dict], ref: str | None = None) -> dict:
    rows = []
    for entry in files:
        path = entry["path"]
        raw = content(path, ref)
        rows.append({"path": path, "lf_bytes": len(raw), "lf_sha256": hashlib.sha256(raw).hexdigest(),
                     "gzip_bytes": compressed(raw)})
    return {"file_count": len(rows), "files": rows, "gzip_sum_bytes": sum(row["gzip_bytes"] for row in rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", help="Measure this committed tree instead of the working tree")
    parser.add_argument("--write", action="store_true", help="Update the checked-in evidence")
    parser.add_argument("--check", action="store_true", help="Require an exact match with checked-in evidence")
    args = parser.parse_args()
    expected = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    report = dict(expected)
    report["baseline"] = measure(expected["baseline"]["files"], expected["baseline_ref"])
    report["candidate"] = measure(expected["candidate"]["files"], args.ref)
    if report["candidate"]["file_count"] != 14:
        raise SystemExit("Expected the approved 14-file first-screen inventory")
    report["increase_bytes"] = report["candidate"]["gzip_sum_bytes"] - report["baseline"]["gzip_sum_bytes"]
    report["remaining_bytes"] = report["budget_bytes"] - report["increase_bytes"]
    path = "App/public_frontend/app.js"
    first_hook = compressed(content(path, FIRST_UI)) - compressed(content(path, report["logic_parent_ref"]))
    report.update(second_batch_parent_ref=FIRST_UI, first_batch_increase_bytes=57474,
                  second_batch_hook_budget_bytes=3966, app_hook_budget_bytes=first_hook + 3966)
    report["app_hook_increase_bytes"] = compressed(content(path, args.ref)) - compressed(content(path, report["logic_parent_ref"]))
    report["second_batch_hook_increase_bytes"] = report["app_hook_increase_bytes"] - first_hook
    report["passed"] = report["remaining_bytes"] >= 0 and report["second_batch_hook_increase_bytes"] <= 3966
    if args.write:
        EVIDENCE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: value for key, value in report.items() if key not in {"baseline", "candidate"}}, ensure_ascii=False))
    if args.check and report != expected:
        raise SystemExit("Measured files differ from checked-in evidence")
    if not report["passed"]:
        raise SystemExit("First-screen budget exceeded")


if __name__ == "__main__":
    main()
