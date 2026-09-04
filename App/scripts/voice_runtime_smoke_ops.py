# ruff: noqa: E501
"""Prepare, execute, validate, and review the five-slot local TTS smoke run.

Every live request receives an immutable attempt record before a WebSocket is
opened.  A missing result blocks automatic retry.  The paused slot is excluded,
the whole run is capped at USD 0.005, and all command output is redacted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import secrets
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend.snow_app import local_voice

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_provider_blind_test_ops as blind
    from . import voice_provider_enrollment_ops as enrollment
    from . import voice_runtime_profile_ops as profile_ops
else:
    import voice_paralinguistic_ops as base
    import voice_provider_blind_test_ops as blind
    import voice_provider_enrollment_ops as enrollment
    import voice_runtime_profile_ops as profile_ops


SCHEMA = "project-snow-private-local-voice-runtime-smoke-run-1"
AUDIT_SCHEMA = "project-snow-private-local-voice-runtime-smoke-audit-1"
REVIEW_SCHEMA = "project-snow-local-voice-runtime-smoke-review-1"
POLICY_VERSION = "project-snow-five-locked-style-slot-smoke-1"
OUTPUT_DIRECTORY = "tts_runtime_smoke_tests"
RUN_ID_PATTERN = re.compile(r"voice-runtime-smoke-run-[0-9a-f]{20}\Z")
OUTPUT_ID_PATTERN = re.compile(r"runtime-smoke-output-[0-9a-f]{16}\Z")
ATTEMPT_ID_PATTERN = re.compile(r"runtime-smoke-attempt-[0-9a-f]{32}\Z")
DEFAULT_PROFILE_ID = "voice-runtime-profile-e95e7d8e42cdc7c3d241"
EXPECTED_OUTPUT_COUNT = 5
COST_CEILING_USD = Decimal("0.005")

SMOKE_TEXTS = {
    "vidya-neutral-short": "今天的安排很清楚，我们按顺序继续就好。",
    "vidya-heightened": "别移开视线！这一次，我绝不会让你逃走！",
    "chenxing-neutral-short": "检查已经完成，接下来可以安心推进。",
    "chenxing-breathy-lexical": "声音放轻些，我就在你身边，不必惊动其他人。",
    "chenxing-heightened": "抓紧我的手！别回头，我们现在就离开这里！",
}


class VoiceRuntimeSmokeError(base.VoiceParalinguisticError):
    """Raised when the runtime smoke contract cannot be proven safe."""


def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoiceRuntimeSmokeError(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoiceRuntimeSmokeError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoiceRuntimeSmokeError(f"{label} must be an array")
    return value


def _string(value: Any, *, label: str) -> str:
    try:
        return base._require_string(value, label=label)
    except base.VoiceParalinguisticError as error:
        raise VoiceRuntimeSmokeError(str(error)) from error


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _timestamp(value: Any, *, label: str) -> str:
    text = _string(value, label=label)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise VoiceRuntimeSmokeError(f"{label} must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VoiceRuntimeSmokeError(f"{label} must include a UTC offset")
    return text


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _profile(
    root: Path, profile_id: str
) -> tuple[Path, dict[str, Any], bytes, local_voice.LocalVoiceRuntime]:
    safe_id = base._safe_identifier(profile_id, profile_ops.PROFILE_ID_PATTERN, label="runtime profile ID")
    validation = profile_ops.validate_profile(root, safe_id)
    path = root / profile_ops.OUTPUT_DIRECTORY / safe_id / "manifest.json"
    document, payload = base._read_json(root, path, label="runtime profile manifest")
    _expect(
        validation.get("manifest_sha256"),
        document.get("manifest_sha256"),
        label="runtime profile validation",
    )
    runtime = local_voice.LocalVoiceRuntime(path, api_key="offline-not-read")
    return path, document, payload, runtime


def build_run(
    voice_root: Path,
    *,
    profile_id: str,
    prepared_at: str,
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    timestamp = _timestamp(prepared_at, label="prepared_at")
    _path, profile, profile_payload, runtime = _profile(root, profile_id)
    planned: list[dict[str, Any]] = []
    for case_id, text in SMOKE_TEXTS.items():
        matches = [
            route
            for route in runtime.routes.values()
            if route.case_id == case_id and route.status == "locked"
        ]
        _expect(len(matches), 1, label=f"{case_id} locked route count")
        route = matches[0]
        output_identity = {
            "profile_id": runtime.profile_id,
            "case_id": case_id,
            "style": route.style,
            "text_sha256": base._sha256_bytes(text.encode("utf-8")),
        }
        output_id = "runtime-smoke-output-" + base._semantic_sha256(output_identity)[:16]
        planned.append(
            {
                "output_id": output_id,
                "sequence": len(planned) + 1,
                "case_id": case_id,
                "character_id": route.character_id,
                "character_slug": route.character_slug,
                "runtime_character_name": route.character_name,
                "style": route.style,
                "text": text,
                "text_sha256": output_identity["text_sha256"],
                "input_character_count": len(text),
                "billing_character_count": blind._billing_character_count(text),
                "private_audio_relative_path": (
                    f"audio/{route.character_slug}/{len(planned) + 1:02d}-{output_id}.wav"
                ),
            }
        )
    _expect(len(planned), EXPECTED_OUTPUT_COUNT, label="planned smoke output count")
    _expect(len({item["output_id"] for item in planned}), len(planned), label="output ID uniqueness")
    _expect(
        "vidya-breathy-lexical" not in {item["case_id"] for item in planned},
        True,
        label="paused slot exclusion",
    )
    billable = sum(item["billing_character_count"] for item in planned)
    estimated = blind._estimated_cost(billable, blind.PRICE_USD_PER_10K_CHARACTERS)
    if estimated > COST_CEILING_USD:
        raise VoiceRuntimeSmokeError("planned smoke cost exceeds the authorized ceiling")
    identity = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "profile_manifest_sha256": profile["manifest_sha256"],
        "prepared_at": timestamp,
        "planned_outputs": planned,
        "cost_ceiling_usd": str(COST_CEILING_USD),
    }
    run_id = "voice-runtime-smoke-run-" + base._semantic_sha256(identity)[:20]
    document: dict[str, Any] = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "run_id": run_id,
        "prepared_at": timestamp,
        "status": "prepared_for_authorized_five_slot_smoke",
        "source_profile": {
            "profile_id": profile["profile_id"],
            "manifest_sha256": profile["manifest_sha256"],
            "manifest_byte_sha256": base._sha256_bytes(profile_payload),
        },
        "provider_contract": {
            "region": local_voice.REGION,
            "target_model": local_voice.MODEL,
            "websocket_endpoint": local_voice.WEBSOCKET_ENDPOINT,
            "response_format": local_voice.RESPONSE_FORMAT,
            "sample_rate_hz": local_voice.SAMPLE_RATE_HZ,
            "channels": local_voice.CHANNELS,
            "sample_width_bytes": local_voice.SAMPLE_WIDTH_BYTES,
            "instructions_sent": False,
        },
        "pricing_contract": {
            "planned_output_count": len(planned),
            "planned_billable_characters": billable,
            "estimated_cost_usd": str(estimated),
            "hard_cost_ceiling_usd": str(COST_CEILING_USD),
        },
        "authorization_contract": {
            "authorization_context": "direct_continuation_of_explicit_usd_0_005_scope",
            "five_locked_slot_smoke_authorized": True,
            "paused_slot_authorized": False,
            "automatic_retry_authorized": False,
            "voice_creation_authorized": False,
            "voice_deletion_authorized": False,
            "training_or_fine_tuning_authorized": False,
            "publication_or_rollout_authorized": False,
        },
        "planned_outputs": planned,
        "privacy_contract": {
            "provider_voice_ids_in_manifest": False,
            "workspace_id_in_manifest": False,
            "provider_voice_ids_in_command_output": False,
            "workspace_id_in_command_output": False,
            "local_review_only": True,
        },
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    return document, root / OUTPUT_DIRECTORY / run_id


def _write_or_verify(root: Path, path: Path, payload: bytes, *, label: str) -> str:
    if path.exists():
        existing = base._read_stable_bytes(root, path, label=label)
        _expect(existing, payload, label=label)
        return "existing_identical"
    blind._write_atomic_new(path, payload, label=label)
    return "written"


def write_run(voice_root: Path, document: dict[str, Any], destination: Path) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    parent = root / OUTPUT_DIRECTORY
    parent.mkdir(exist_ok=True)
    base._require_safe_existing_path(root, parent, label="smoke run root", directory=True)
    _expect(destination.parent, parent, label="smoke destination parent")
    destination.mkdir(exist_ok=True)
    base._require_safe_existing_path(root, destination, label="smoke run directory", directory=True)
    for relative in ("audits", "audio", "audio/vidya", "audio/chenxing", "review"):
        path = destination.joinpath(*relative.split("/"))
        path.mkdir(exist_ok=True)
        base._require_safe_existing_path(root, path, label=relative, directory=True)
    state = _write_or_verify(
        root,
        destination / "manifest.json",
        _pretty_json_bytes(document),
        label="smoke run manifest",
    )
    pricing = _object(document.get("pricing_contract"), label="pricing contract")
    return {
        "status": document["status"],
        "write_status": state,
        "run_id": document["run_id"],
        "path": str(destination),
        "manifest_sha256": document["manifest_sha256"],
        "planned_output_count": pricing["planned_output_count"],
        "planned_billable_characters": pricing["planned_billable_characters"],
        "estimated_cost_usd": pricing["estimated_cost_usd"],
        "hard_cost_ceiling_usd": pricing["hard_cost_ceiling_usd"],
        "paused_slot_included": False,
        "credentials_read": False,
        "network_calls_performed": False,
        "private_provider_identifiers_exposed_in_result": False,
    }


def _load_run(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    safe_id = base._safe_identifier(run_id, RUN_ID_PATTERN, label="smoke run ID")
    directory = root / OUTPUT_DIRECTORY / safe_id
    base._require_safe_existing_path(root, directory, label="smoke run directory", directory=True)
    manifest, payload = base._read_json(root, directory / "manifest.json", label="smoke manifest")
    _expect(manifest.get("schema_version"), SCHEMA, label="smoke schema")
    _expect(manifest.get("run_id"), safe_id, label="smoke run ID")
    _expect(manifest.get("status"), "prepared_for_authorized_five_slot_smoke", label="smoke status")
    base._verify_semantic_hash(manifest, field="manifest_sha256", label="smoke manifest")
    if expected_manifest_byte_sha256 is not None:
        _expect(
            base._sha256_bytes(payload),
            base._require_sha256(expected_manifest_byte_sha256, label="expected smoke manifest byte SHA-256"),
            label="smoke manifest byte SHA-256",
        )
    source = _object(manifest.get("source_profile"), label="source profile")
    profile_id = _string(source.get("profile_id"), label="source profile ID")
    profile_validation = profile_ops.validate_profile(
        root,
        profile_id,
        expected_manifest_byte_sha256=_string(
            source.get("manifest_byte_sha256"), label="source profile byte SHA-256"
        ),
    )
    _expect(
        source.get("manifest_sha256"),
        profile_validation["manifest_sha256"],
        label="source profile semantic SHA-256",
    )
    rebuilt, rebuilt_destination = build_run(
        root,
        profile_id=profile_id,
        prepared_at=_string(manifest.get("prepared_at"), label="prepared_at"),
    )
    _expect(rebuilt_destination, directory, label="rebuilt smoke directory")
    _expect(rebuilt, manifest, label="smoke manifest reconstruction")
    planned = {
        _string(item.get("output_id"), label="smoke output ID"): item
        for item in _array(manifest.get("planned_outputs"), label="planned outputs")
        if isinstance(item, dict)
    }
    _expect(len(planned), EXPECTED_OUTPUT_COUNT, label="planned output count")
    return directory, manifest, planned


def _write_audit(directory: Path, record: dict[str, Any], filename: str) -> Path:
    if not re.fullmatch(r"runtime-smoke-attempt-[0-9a-f]{32}-(?:attempt|result)\.json", filename):
        raise VoiceRuntimeSmokeError("invalid smoke audit filename")
    document = dict(record)
    document["record_sha256"] = base._semantic_sha256(document)
    return blind._write_atomic_new(directory / filename, _pretty_json_bytes(document), label="smoke audit")


def _audit_state(root: Path, directory: Path, run_id: str) -> dict[str, Any]:
    attempts: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        record, _ = base._read_json(root, path, label="smoke audit")
        _expect(record.get("schema_version"), AUDIT_SCHEMA, label="smoke audit schema")
        base._verify_semantic_hash(record, field="record_sha256", label="smoke audit")
        _expect(record.get("run_id"), run_id, label="smoke audit run ID")
        attempt_id = base._safe_identifier(
            record.get("attempt_id"), ATTEMPT_ID_PATTERN, label="smoke attempt ID"
        )
        stage = record.get("stage")
        target = attempts if stage == "attempt_started" else results if stage == "result_committed" else None
        if target is None or attempt_id in target:
            raise VoiceRuntimeSmokeError("smoke audit history is ambiguous")
        target[attempt_id] = record
    if set(results) - set(attempts):
        raise VoiceRuntimeSmokeError("smoke result lacks an attempt")
    pending = [record for key, record in attempts.items() if key not in results]
    by_output: dict[str, dict[str, Any]] = {}
    for attempt_id, result in results.items():
        output_id = base._safe_identifier(
            result.get("output_id"), OUTPUT_ID_PATTERN, label="smoke result output ID"
        )
        _expect(attempts[attempt_id].get("output_id"), output_id, label="attempt/result output")
        _expect(result.get("outcome"), "audio_rendered", label="smoke result outcome")
        if output_id in by_output:
            raise VoiceRuntimeSmokeError("smoke output has multiple results")
        by_output[output_id] = result
    return {
        "attempts": attempts,
        "results": results,
        "pending": pending,
        "by_output": by_output,
    }


def _validate_audio(
    root: Path, directory: Path, output: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    _expect(
        result.get("private_audio_relative_path"),
        output["private_audio_relative_path"],
        label="smoke audio path",
    )
    relative = base._safe_relative_path(
        output["private_audio_relative_path"], label="smoke audio relative path"
    )
    payload = base._read_stable_bytes(root, directory.joinpath(*relative.parts), label="smoke WAV")
    metrics = blind._validate_wav_bytes(payload)
    _expect(result.get("audio_metrics"), metrics, label="smoke WAV metrics")
    return metrics


def validate_run(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, planned = _load_run(
        root, run_id, expected_manifest_byte_sha256=expected_manifest_byte_sha256
    )
    state = _audit_state(root, directory / "audits", run_id)
    if not set(state["by_output"]).issubset(planned):
        raise VoiceRuntimeSmokeError("smoke audit references an unplanned output")
    full_scale = 0
    for output_id, result in state["by_output"].items():
        full_scale += _validate_audio(root, directory, planned[output_id], result)["full_scale_sample_count"]
    usage = sum(int(item.get("provider_usage_characters") or 0) for item in state["by_output"].values())
    billed = sum(
        int(item.get("conservative_billing_characters") or 0) for item in state["by_output"].values()
    )
    estimated = blind._estimated_cost(billed, blind.PRICE_USD_PER_10K_CHARACTERS)
    if estimated > COST_CEILING_USD:
        raise VoiceRuntimeSmokeError("completed smoke cost exceeds the ceiling")
    return {
        "status": "valid",
        "run_id": run_id,
        "path": str(directory),
        "manifest_sha256": manifest["manifest_sha256"],
        "manifest_byte_sha256": base._sha256_bytes(
            base._read_stable_bytes(root, directory / "manifest.json", label="smoke manifest")
        ),
        "planned_output_count": len(planned),
        "successful_output_count": len(state["by_output"]),
        "pending_attempt_count": len(state["pending"]),
        "provider_usage_characters": usage,
        "conservative_billing_characters": billed,
        "estimated_cost_usd": str(estimated),
        "hard_cost_ceiling_usd": str(COST_CEILING_USD),
        "full_scale_sample_count": full_scale,
        "review_ready": len(state["by_output"]) == len(planned) and not state["pending"],
        "private_provider_identifiers_exposed_in_result": False,
    }


def render_all(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None,
    confirm_run_id: str,
    confirm_model: str,
    confirm_region: str,
    confirm_cost_ceiling_usd: str,
    confirm_five_locked_slot_smoke_authorized: bool,
    confirm_paused_slot_excluded: bool,
    dotenv_file: Path | None = None,
    provider: Callable[..., tuple[bytes, dict[str, Any]]] = local_voice.provider_synthesize_pcm,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, planned = _load_run(
        root, run_id, expected_manifest_byte_sha256=expected_manifest_byte_sha256
    )
    _expect(confirm_run_id, run_id, label="confirmed smoke run ID")
    _expect(confirm_model, local_voice.MODEL, label="confirmed model")
    _expect(confirm_region, local_voice.REGION, label="confirmed region")
    _expect(
        confirm_cost_ceiling_usd,
        str(COST_CEILING_USD),
        label="confirmed cost ceiling",
    )
    _expect(
        confirm_five_locked_slot_smoke_authorized,
        True,
        label="five locked-slot smoke authorization",
    )
    _expect(confirm_paused_slot_excluded, True, label="paused-slot exclusion")
    state = _audit_state(root, directory / "audits", run_id)
    if state["pending"]:
        raise VoiceRuntimeSmokeError(
            "an uncertain live attempt exists; reconcile it manually before any retry"
        )
    if set(state["by_output"]) - set(planned):
        raise VoiceRuntimeSmokeError("smoke audit references an unplanned output")
    source = _object(manifest.get("source_profile"), label="source profile")
    profile_path = (
        root
        / profile_ops.OUTPUT_DIRECTORY
        / _string(source.get("profile_id"), label="source profile ID")
        / "manifest.json"
    )
    api_key = enrollment._read_secret(
        None,
        voice_root=root,
        region=enrollment.CHINA_REGION,
        dotenv_file=dotenv_file,
    )
    runtime = local_voice.LocalVoiceRuntime(profile_path, api_key=api_key, provider=provider)
    completed = set(state["by_output"])
    for output in sorted(planned.values(), key=lambda item: int(item["sequence"])):
        output_id = output["output_id"]
        if output_id in completed:
            continue
        route = runtime.route(
            _string(output.get("character_id"), label="smoke character ID"),
            _string(output.get("text"), label="smoke text"),
            style=_string(output.get("style"), label="smoke style"),
        )
        if route.status != "locked" or route.provider_voice_id is None:
            raise VoiceRuntimeSmokeError("smoke output does not resolve to a locked route")
        attempt_id = "runtime-smoke-attempt-" + secrets.token_hex(16)
        attempt = {
            "schema_version": AUDIT_SCHEMA,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "stage": "attempt_started",
            "started_at": _now(),
            "output_id": output_id,
            "case_id": output["case_id"],
            "style": output["style"],
            "text_sha256": output["text_sha256"],
            "provider_voice_id_sha256": base._sha256_bytes(route.provider_voice_id.encode("utf-8")),
            "target_model": local_voice.MODEL,
            "region": local_voice.REGION,
            "automatic_retry_authorized": False,
        }
        _write_audit(directory / "audits", attempt, f"{attempt_id}-attempt.json")
        synthesized = runtime.synthesize(output["character_id"], output["text"], style=output["style"])
        _expect(synthesized.get("case_id"), output["case_id"], label="synthesized case ID")
        _expect(synthesized.get("style"), output["style"], label="synthesized style")
        wav = synthesized.pop("audio_bytes")
        relative = base._safe_relative_path(output["private_audio_relative_path"], label="smoke audio path")
        blind._write_atomic_new(directory.joinpath(*relative.parts), wav, label="smoke WAV")
        metrics = blind._validate_wav_bytes(wav)
        provider_usage = synthesized.get("provider_usage_characters")
        conservative_billing = max(
            int(output["billing_character_count"]),
            provider_usage if isinstance(provider_usage, int) else 0,
        )
        result = {
            "schema_version": AUDIT_SCHEMA,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "stage": "result_committed",
            "completed_at": _now(),
            "output_id": output_id,
            "case_id": output["case_id"],
            "style": output["style"],
            "outcome": "audio_rendered",
            "private_audio_relative_path": output["private_audio_relative_path"],
            "audio_metrics": metrics,
            "provider_usage_characters": (provider_usage if isinstance(provider_usage, int) else None),
            "conservative_billing_characters": conservative_billing,
            "private_provider_identifiers_embedded": False,
        }
        _write_audit(directory / "audits", result, f"{attempt_id}-result.json")
        completed.add(output_id)
    validation = validate_run(root, run_id, expected_manifest_byte_sha256=expected_manifest_byte_sha256)
    _expect(validation.get("review_ready"), True, label="smoke review readiness")
    return validation


def _review_html(document: dict[str, Any]) -> str:
    cards = []
    labels = {"neutral": "中性", "breathy": "亲密轻声（有词）", "heightened": "高情绪"}
    for item in document["samples"]:
        cards.append(
            "<article><h2>"
            + html.escape(f"{item['runtime_character_name']} · {labels[item['style']]}")
            + "</h2><p>"
            + html.escape(item["text"])
            + '</p><audio controls preload="metadata" src="'
            + html.escape(item["audio_relative_path"], quote=True)
            + '"></audio><p class="meta">'
            + html.escape(f"{item['duration_seconds']:.3f}s · 24 kHz · 峰值 {item['peak_dbfs']} dBFS")
            + "</p></article>"
        )
    return (
        """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Snow 运行时语音冒烟试听</title><style>
body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:900px;margin:32px auto;padding:0 18px;background:#f5f7fb;color:#172033}
header,article{background:white;border:1px solid #dfe5ef;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 4px 18px #27334a12}
h1{margin-top:0}h2{font-size:1.1rem}audio{width:100%}.meta{color:#657087;font-size:.9rem}.ok{color:#176b45;font-weight:700}
</style></head><body><header><h1>运行时语音冒烟试听</h1>
<p class="ok">5/5 已生成并通过文件级校验；暂停槽位未调用。</p>
<p>这里只验证最终运行路由与真实合成链路，不开启新一轮 A/B 选择，也不改变终局结论。</p></header>
"""
        + "".join(cards)
        + "</body></html>\n"
    )


def finalize_review(
    voice_root: Path,
    run_id: str,
    *,
    expected_manifest_byte_sha256: str | None = None,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    directory, manifest, planned = _load_run(
        root, run_id, expected_manifest_byte_sha256=expected_manifest_byte_sha256
    )
    validation = validate_run(root, run_id, expected_manifest_byte_sha256=expected_manifest_byte_sha256)
    _expect(validation.get("review_ready"), True, label="smoke review readiness")
    state = _audit_state(root, directory / "audits", run_id)
    samples = []
    for output in sorted(planned.values(), key=lambda item: int(item["sequence"])):
        result = state["by_output"][output["output_id"]]
        metrics = _object(result.get("audio_metrics"), label="smoke audio metrics")
        relative = base._safe_relative_path(output["private_audio_relative_path"], label="smoke audio path")
        samples.append(
            {
                "sequence": output["sequence"],
                "case_id": output["case_id"],
                "runtime_character_name": output["runtime_character_name"],
                "style": output["style"],
                "text": output["text"],
                "audio_relative_path": "../" + relative.as_posix(),
                "duration_seconds": metrics["duration_seconds"],
                "peak_dbfs": metrics["peak_dbfs"],
                "full_scale_sample_count": metrics["full_scale_sample_count"],
                "wav_sha256": metrics["wav_sha256"],
            }
        )
    review: dict[str, Any] = {
        "schema_version": REVIEW_SCHEMA,
        "run_id": run_id,
        "status": "ready_for_local_runtime_smoke_listening",
        "source_manifest_sha256": manifest["manifest_sha256"],
        "summary": {
            "sample_count": len(samples),
            "paused_slot_called": False,
            "new_preference_round_created": False,
        },
        "privacy_contract": {
            "provider_voice_ids_included": False,
            "workspace_id_included": False,
            "local_review_only": True,
        },
        "samples": samples,
    }
    review["manifest_sha256"] = base._semantic_sha256(review)
    review_directory = directory / "review"
    manifest_state = _write_or_verify(
        root,
        review_directory / "manifest.json",
        _pretty_json_bytes(review),
        label="smoke review manifest",
    )
    html_state = _write_or_verify(
        root,
        review_directory / "review.html",
        _review_html(review).encode("utf-8"),
        label="smoke review HTML",
    )
    return {
        "status": review["status"],
        "manifest_write_status": manifest_state,
        "html_write_status": html_state,
        "run_id": run_id,
        "review_html_path": str(review_directory / "review.html"),
        "review_manifest_path": str(review_directory / "manifest.json"),
        "review_manifest_sha256": review["manifest_sha256"],
        "sample_count": len(samples),
        "paused_slot_called": False,
        "new_preference_round_created": False,
        "private_provider_identifiers_exposed_in_result": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", type=Path, required=True)
    parser.add_argument("--dotenv-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--execute", action="store_true")
    validate = commands.add_parser("validate")
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--expect-manifest-byte-sha256")
    render = commands.add_parser("render-all")
    render.add_argument("--run-id", required=True)
    render.add_argument("--expect-manifest-byte-sha256")
    render.add_argument("--confirm-run-id", default="")
    render.add_argument("--confirm-model", default="")
    render.add_argument("--confirm-region", default="")
    render.add_argument("--confirm-cost-ceiling-usd", default="")
    render.add_argument("--confirm-five-locked-slot-smoke-authorized", action="store_true")
    render.add_argument("--confirm-paused-slot-excluded", action="store_true")
    render.add_argument("--execute", action="store_true")
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--expect-manifest-byte-sha256")
    finalize.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            document, destination = build_run(
                arguments.voice_root,
                profile_id=arguments.profile_id,
                prepared_at=arguments.prepared_at,
            )
            result = (
                write_run(arguments.voice_root, document, destination)
                if arguments.execute
                else {
                    "status": "dry_run",
                    "run_id": document["run_id"],
                    "path": str(destination),
                    "manifest_sha256": document["manifest_sha256"],
                    "manifest_byte_sha256": base._sha256_bytes(_pretty_json_bytes(document)),
                    "planned_output_count": document["pricing_contract"]["planned_output_count"],
                    "planned_billable_characters": document["pricing_contract"][
                        "planned_billable_characters"
                    ],
                    "estimated_cost_usd": document["pricing_contract"]["estimated_cost_usd"],
                    "hard_cost_ceiling_usd": document["pricing_contract"]["hard_cost_ceiling_usd"],
                    "paused_slot_included": False,
                    "credentials_read": False,
                    "network_calls_performed": False,
                    "private_provider_identifiers_exposed_in_result": False,
                }
            )
        elif arguments.command == "validate":
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
            )
        elif arguments.command == "render-all" and not arguments.execute:
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
            )
            result["note"] = "render-all remains offline unless --execute is present"
        elif arguments.command == "render-all":
            result = render_all(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
                confirm_run_id=arguments.confirm_run_id,
                confirm_model=arguments.confirm_model,
                confirm_region=arguments.confirm_region,
                confirm_cost_ceiling_usd=arguments.confirm_cost_ceiling_usd,
                confirm_five_locked_slot_smoke_authorized=arguments.confirm_five_locked_slot_smoke_authorized,
                confirm_paused_slot_excluded=arguments.confirm_paused_slot_excluded,
                dotenv_file=arguments.dotenv_file,
            )
        elif not arguments.execute:
            result = validate_run(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
            )
            result["note"] = "finalize remains a dry run unless --execute is present"
        else:
            result = finalize_review(
                arguments.voice_root,
                arguments.run_id,
                expected_manifest_byte_sha256=arguments.expect_manifest_byte_sha256,
            )
    except (
        OSError,
        ValueError,
        VoiceRuntimeSmokeError,
        local_voice.LocalVoiceError,
        profile_ops.VoiceRuntimeProfileError,
        blind.VoiceProviderBlindTestError,
        enrollment.VoiceProviderEnrollmentError,
    ) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
