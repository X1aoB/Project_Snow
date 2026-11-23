"""Audit dialogue using synthetic sessions and the deployed prompt and guard path."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import httpx

from backend.snow_app.config import Settings
from backend.snow_app.dialogue_turn import parse_turn_frame
from backend.snow_app.mvp_policy import MVP_CHARACTERS, canonical_mvp_character
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_providers import PROVIDERS
from backend.snow_app.public_service import _public_immersive_thinking_decision
from backend.snow_app.repository import RuntimeRepository

from .common import APP_ROOT, DATA_ROOT, RUNTIME_ROOT, read_jsonl, write_json
from .style_metrics import metrics

PREFIX = "PROJECT_SNOW_STYLE_"
FLAGS = dict(
    EXAMPLES="static",
    SIGNATURE_GUARD="bubu_legacy",
    SIGNATURE_PROMPT="bubu_legacy",
    REGISTER_GUARD="fenny_legacy",
    REGISTER_PROMPT="fenny_legacy",
    SHARED_FACTS="futiya_legacy",
    SITUATIONAL_CUES="futiya_legacy",
    FALLBACK="legacy",
    COPY="measure",
    WINDOW="6",
    COEFFICIENT="1",
    EXAMPLE_COUNT="4",
    KEEP_REPEATED_LINES="true",
)
INPUTS = (
    "mvp/character_views.jsonl",
    "personas/dialogue_style_profiles.jsonl",
    "personas/persona_profiles.jsonl",
    "lakehouse/documents.jsonl",
    "indexes/lexical.sqlite3",
    "graph/nodes.jsonl",
    "graph/edges.jsonl",
)
PROBES = APP_ROOT / "config/style_probes_v1.json"


def preset(name):
    cfg = FLAGS.copy()
    if name in {"candidate-style", "candidate-all"}:
        cfg.update(EXAMPLES="retrieved", SIGNATURE_GUARD="data_driven", SIGNATURE_PROMPT="data_driven")
    if name == "candidate-all":
        cfg.update(
            REGISTER_GUARD="data_driven",
            REGISTER_PROMPT="data_driven",
            SHARED_FACTS="data_driven",
            SITUATIONAL_CUES="reviewed_table",
            FALLBACK="minimal_repair",
            COPY="rewrite",
        )
    if name not in {"current", "candidate-style", "candidate-all"}:
        overrides = json.loads(Path(name).read_text(encoding="utf8"))
        if not isinstance(overrides, dict) or set(overrides) - cfg.keys():
            raise ValueError("Unknown audit configuration flag")
        cfg.update({k: str(v) for k, v in overrides.items()})
    return {PREFIX + k: v for k, v in cfg.items()} | {"PUBLIC_OPENAI_PARAMETER_ADAPTER": "on"}


@contextmanager
def flags(values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def plan(characters, sessions, turns, style, seed):
    daily = json.loads(PROBES.read_text(encoding="utf8"))
    held = defaultdict(list)
    for p in read_jsonl(style / "dialogue_pairs.jsonl"):
        if p["split"] == "holdout" and p["spoken"]:
            held[p["character_id"]].append(p)
    shared = defaultdict(list)
    for t in read_jsonl(style / "shared_topics.jsonl"):
        shared[t["character_id"]].append(t)
    for cid in characters:
        rng = random.Random(f"{seed}:{cid}")
        for session, probes in enumerate(daily[:sessions]):
            for index, message in enumerate(probes[:turns]):
                yield cid, f"daily-{session}", index, message, None
        if held[cid]:
            p = rng.choice(held[cid])
            history = "\n".join(p["preceding"])
            yield cid, "held", 0, f"前文：{history}\n分析员：{p['analyst']}\n请只回一句。", p
        if shared[cid]:
            topic = rng.choice(shared[cid])
            other = canonical_mvp_character(topic["other_character_id"]).display_name
            yield cid, "shared", 0, f"你和{other}当时聊到{topic['term']}，还记得吗？", None


def table(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def prepare_runtime(source, temp):
    hashes = {}
    for filename in INPUTS:
        src, dest = source / filename, temp / filename
        if not src.is_file():
            raise ValueError(f"Missing corpus artifact {filename}")
        dest.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(src, dest)
        hashes[filename] = sha256(dest.read_bytes()).hexdigest()
    return hashes


def append_row(path, row):
    with path.open("a", encoding="utf8") as file:
        file.write(json.dumps(row, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())


def source_fingerprint():
    digest = sha256()
    for pattern in (
        "backend/snow_app/*.py",
        "backend/snow_app/*.json",
        "pipelines/*.py",
        "config/style_probes_v1.json",
    ):
        for path in sorted(APP_ROOT.glob(pattern)):
            digest.update(path.relative_to(APP_ROOT).as_posix().encode() + b"\0")
            digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def run(a):
    spec = PROVIDERS["openai"]
    key = os.environ.get("OPENAI_API_KEY") if a.execute else "synthetic"
    if not key:
        raise ValueError("Set OPENAI_API_KEY for execution")
    cfg = preset(a.config)
    style = a.style.resolve()
    cfg[PREFIX + "ROOT"] = str(style)
    ids = (
        [c.character_id for c in MVP_CHARACTERS]
        if a.characters == "all"
        else [canonical_mvp_character(c.strip()) for c in a.characters.split(",")]
    )
    if None in ids:
        raise ValueError("Unknown audit character")
    ids = list(dict.fromkeys(getattr(c, "character_id", c) for c in ids))
    tasks = list(plan(ids, a.sessions, a.turns, style, a.seed))
    corpus_hash = sha256((style / "manifest.json").read_bytes()).hexdigest()
    out = a.out.resolve()
    if out.is_relative_to(DATA_ROOT.resolve()) or out.is_relative_to(style):
        raise ValueError("Use a separate audit output directory")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("Output directory must be empty; reconcile previous attempts before running again")
    (out / ".gitignore").write_text("*\n", encoding="utf8")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=APP_ROOT, text=True).strip()
    manifest = dict(
        config=Path(a.config).name,
        flags=cfg,
        dry_run=not a.execute,
        model=a.model,
        seed=a.seed,
        commit=commit,
        source_fingerprint=source_fingerprint(),
        characters=ids,
        probes=a.probes,
        sessions=a.sessions,
        turns=a.turns,
        probe_sha256=sha256(PROBES.read_bytes()).hexdigest(),
        corpus_sha256=corpus_hash,
        style_files={f.name: sha256(f.read_bytes()).hexdigest() for f in style.glob("*.jsonl")},
        planned_turns=len(tasks),
        max_http_calls=len(tasks) * 2,
        status="started",
        token_estimator="ceil(UTF-8 bytes of actual messages JSON / 3)",
        simulated_history=not a.execute,
    )
    write_json(out / "manifest.json", manifest)
    rows = []
    with tempfile.TemporaryDirectory(dir=out, prefix="sessions-") as tmp, flags(cfg):
        root = Path(tmp)
        manifest["runtime_inputs"] = prepare_runtime(a.runtime, root)
        settings = Settings(DATA_ROOT, root, False, "", [], mvp_chat_enabled=True)
        repo = RuntimeRepository(settings)
        svc = MVPService(
            settings,
            repo,
            force_chat_enabled=True,
            conversation_database_path=root / "chat" / "audit.sqlite3",
        )
        svc._model_http_client.close()
        estimated = []

        def observe(req):
            payload = json.loads(req.content)
            estimated.append(math.ceil(len(json.dumps(payload["messages"], ensure_ascii=False).encode()) / 3))

        def synthetic(req):
            content = json.dumps(
                {
                    "content_blocks": [{"type": "message", "text": "嗯，我在听，你可以慢慢说。"}],
                    "state_updates": [],
                    "expression_state": "neutral",
                },
                ensure_ascii=False,
            )
            return httpx.Response(
                200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
            )

        client = httpx.Client(
            event_hooks={"request": [observe]},
            follow_redirects=False,
            transport=None if a.execute else httpx.MockTransport(synthetic),
        )
        svc._model_http_client = client
        decision = _public_immersive_thinking_decision(spec, 2, model=a.model)
        try:
            for cid, session, turn, message, ref in tasks:
                estimated.clear()
                svc.reset_generation_diagnostics()
                unit = dict(character_id=cid, session=session, turn=turn)
                if a.execute:
                    append_row(out / "attempts.jsonl", {**unit, "status": "started"})
                try:
                    result = svc.chat(
                        cid,
                        message,
                        session_id=f"audit-{cid}-{session}",
                        world_session_id=f"audit-world-{cid}-{session}",
                        model_settings=(spec.base_url, key, a.model),
                        thinking_decision=decision,
                        communication_channel="text",
                        allow_dialogue_channel_transition=False,
                        turn_frame=parse_turn_frame(message, communication_channel="text"),
                        max_tokens_override=1600,
                    )
                except Exception as exc:
                    manifest.update(
                        status="stopped",
                        last_unit=unit,
                        error_type=type(exc).__name__,
                        completed_turns=len(rows),
                        requires_reconciliation=bool(a.execute and estimated),
                    )
                    write_json(out / "manifest.json", manifest)
                    raise RuntimeError(f"Stopped at {unit}: {type(exc).__name__}") from None
                diagnostics = svc.generation_diagnostics()
                adjustments = result["response_adjustments"]
                guard_code = result.get("guard_code", "")
                usage = result.get("usage") or {}
                row = dict(
                    **unit,
                    answer=result["answer"],
                    estimate_calls=len(estimated),
                    estimated_prompt_tokens=sum(estimated),
                    guard=bool(result["guard_violation_count"]),
                    rewrite="answer_guardrail_retry" in adjustments,
                    fallback=result["retrieval"]["diagnostics"]["guard_fallback"],
                    register=bool(
                        diagnostics.get("style_register_mismatch_count")
                        or "register_mismatch" in guard_code
                        or "fenny_daily_harshness" in guard_code
                    ),
                    shared_denial=bool(
                        diagnostics.get("style_cross_character_fact_denial_count")
                        or "cross_character_fact" in guard_code
                    ),
                    style_example_token_delta_estimate=diagnostics.get(
                        "style_example_token_delta_estimate", 0
                    ),
                    http_calls=diagnostics["provider_http_calls"] if a.execute else 0,
                    compat_retries=max(0, diagnostics["provider_http_calls"] - diagnostics["model_calls"]),
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                )
                rows.append(row)
                if a.execute:
                    append_row(
                        out / "responses.jsonl",
                        {
                            **row,
                            "message": message,
                            "reference": ref["reply"] if ref else None,
                            "reference_page": ref["page_id"] if ref else None,
                        },
                    )
                    append_row(out / "attempts.jsonl", {**unit, "status": "completed"})
        finally:
            client.close()
    if a.execute:
        scores, terms = metrics(
            rows,
            list(read_jsonl(style / "character_lines.jsonl")),
            list(read_jsonl(style / "signature_terms.jsonl")),
            a.seed,
            a.copy_threshold,
        )
        table(out / "metrics.csv", scores)
        table(out / "terms.csv", terms)
    table(out / "turns.csv", [{k: v for k, v in r.items() if k != "answer"} for r in rows])
    for k in ("http_calls", "estimated_prompt_tokens", "estimate_calls", "input_tokens", "output_tokens"):
        manifest[k] = sum(r[k] for r in rows)
    estimated_input = manifest["input_tokens"] if a.execute else manifest["estimated_prompt_tokens"]
    estimated_output = (
        manifest["output_tokens"] if a.execute else manifest["estimate_calls"] * a.projected_output
    )
    manifest["projected_output_tokens"] = estimated_output
    manifest["estimated_usd"] = (
        (estimated_input * a.input_price + estimated_output * a.output_price) / 1e6
        if a.input_price is not None and a.output_price is not None
        else None
    )
    manifest.update(
        status="complete",
        completed_turns=len(rows),
        input_usd_per_million=a.input_price,
        output_usd_per_million=a.output_price,
    )
    write_json(out / "manifest.json", manifest)
    report = [
        f"# 风格审计 · {manifest['config']}",
        "",
        f"模型 {a.model}，探针 {a.probes}，随机种子 {a.seed}。",
        f"完成 {len(rows)} 轮，实际 HTTP 请求 {manifest['http_calls']} 次，"
        f"按完整请求估计输入 {manifest['estimated_prompt_tokens']} token。",
    ]
    if manifest["estimated_usd"] is not None:
        report += [
            f"按传入单价估算 {manifest['estimated_usd']:.4f} 美元，输出按 {estimated_output} token 计。"
        ]
    if not a.execute:
        report += ["", "本次使用合成回复承接多轮对话。质量指标待实际生成后计算。"]
    else:
        report += ["", "| 角色 | 辨识率 | 留出辨识率 | 改写率 | 兜底率 |", "|---|---:|---:|---:|---:|"]
        for s in scores:
            report.append(
                "| "
                + " | ".join(
                    str(s[k])
                    for k in (
                        "character_id",
                        "recognition",
                        "held_recognition",
                        "rewrite_rate",
                        "fallback_rate",
                    )
                )
                + " |"
            )
    (out / "report.md").write_text("\n".join(report) + "\n", encoding="utf8")
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="current")
    p.add_argument("--model", required=True)
    p.add_argument("--characters", default="all")
    p.add_argument("--probes", choices=["v1"], default="v1")
    p.add_argument("--sessions", type=int, choices=range(1, 6), default=3)
    p.add_argument("--turns", type=int, choices=range(1, 11), default=10)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--runtime", type=Path, default=RUNTIME_ROOT)
    p.add_argument("--style", type=Path, default=RUNTIME_ROOT / "style/1.4.0")
    p.add_argument("--seed", type=int, default=140)
    p.add_argument("--copy-threshold", type=int, default=14)
    p.add_argument("--input-price", type=float, help="USD per million input tokens")
    p.add_argument("--output-price", type=float, help="USD per million output tokens")
    p.add_argument(
        "--projected-output",
        type=int,
        choices=range(1, 1601),
        metavar="1..1600",
        default=256,
        help="Expected output tokens per generation for dry-run price projections",
    )
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    if a.copy_threshold <= 0 or any(x is not None and x < 0 for x in (a.input_price, a.output_price)):
        p.error("Threshold must be positive and prices non-negative")
    result = run(a)
    print(
        json.dumps(
            {k: result[k] for k in ("status", "completed_turns", "http_calls", "estimated_prompt_tokens")}
        )
    )


if __name__ == "__main__":
    main()
