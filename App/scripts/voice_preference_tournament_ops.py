# ruff: noqa: E501
"""Build, validate, and receive a local pairwise voice-preference round.

Round one deliberately reuses a narrow, representative subset of an existing
blind-test package. It never contacts the Provider, synthesizes audio, reads
credentials, reveals the private A/B mapping, or mutates the source package.
Human decisions from this round are the required input for any later paid
challenger generation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__:
    from . import voice_paralinguistic_ops as base
    from . import voice_provider_blind_test_ops as blind
else:
    import voice_paralinguistic_ops as base
    import voice_provider_blind_test_ops as blind


SCHEMA = "project-snow-local-voice-preference-round-1"
SUBMISSION_SCHEMA = "project-snow-local-voice-preference-decision-1"
RECEIPT_SCHEMA = "project-snow-local-voice-preference-decision-receipt-1"
POLICY_VERSION = "project-snow-pairwise-rejection-tournament-1"
OUTPUT_DIRECTORY = "tts_preference_tournaments"
ROUND_ID_PATTERN = re.compile(r"voice-preference-round-[0-9a-f]{20}\Z")

DEFAULT_SOURCE_RUN_ID = "voice-provider-blind-test-run-752a4dd81a874a263de7"
DEFAULT_SOURCE_MANIFEST_SHA256 = (
    "33ae35b8795f87252454da16bbe59ef5c1e79495dc30fda02cb911f9678fd13b"
)
DEFAULT_SOURCE_MANIFEST_BYTE_SHA256 = (
    "90eb099af8abf02ad19cb5931ad008ce200a0e941d3c4de08d2fc509d22a3c95"
)

ROUND_ONE_CASE_IDS = (
    "vidya-neutral-short",
    "vidya-breathy-lexical",
    "vidya-heightened",
    "chenxing-neutral-short",
    "chenxing-breathy-lexical",
    "chenxing-heightened",
)
EXPECTED_CHARACTER_ORDER = ("vidya", "chenxing")
EXPECTED_STYLE_ORDER = ("neutral", "restrained_breathy_lexical", "heightened")
CHOICES = ("first_sample", "second_sample", "reject_both")
USABILITY_VALUES = ("usable", "not_usable")
REJECTION_REASONS = (
    "wrong_voice_identity",
    "wrong_expression_or_character_fit",
    "pronunciation_or_segmentation_error",
    "synthesis_artifact",
)


class VoicePreferenceTournamentError(base.VoiceParalinguisticError):
    """Raised when a preference-round package violates its local contract."""



def _expect(actual: Any, expected: Any, *, label: str) -> None:
    if actual != expected:
        raise VoicePreferenceTournamentError(
            f"{label} mismatch: expected {expected!r}, got {actual!r}"
        )


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VoicePreferenceTournamentError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise VoicePreferenceTournamentError(f"{label} must be an array")
    return value


def _pretty_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_external_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise VoicePreferenceTournamentError(f"{label} does not exist") from error
    if not resolved.is_file():
        raise VoicePreferenceTournamentError(f"{label} must be a file")
    before = resolved.stat()
    if before.st_size > 1024 * 1024:
        raise VoicePreferenceTournamentError(f"{label} is unexpectedly large")
    payload = resolved.read_bytes()
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise VoicePreferenceTournamentError(f"{label} changed while it was read")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VoicePreferenceTournamentError(f"{label} must be UTF-8 JSON") from error
    return _object(document, label=label), payload


def _validate_timestamp(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VoicePreferenceTournamentError(
            "prepared-at must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VoicePreferenceTournamentError("prepared-at must include a UTC offset")
    return value


def _new_directory(root: Path, path: Path, *, label: str) -> Path:
    if not path.exists():
        try:
            path.mkdir()
        except FileExistsError:
            pass
    try:
        return base._require_safe_existing_path(root, path, label=label, directory=True)
    except base.VoiceParalinguisticError as error:
        raise VoicePreferenceTournamentError(str(error)) from error


def _write_or_verify(root: Path, path: Path, payload: bytes, *, label: str) -> str:
    if path.exists():
        existing = base._read_stable_bytes(root, path, label=label)
        _expect(existing, payload, label=label)
        return "existing_identical"
    blind._write_atomic_new(path, payload, label=label)
    return "written"


def _source_review_path(root: Path, source_run_id: str) -> Path:
    if not blind.RUN_ID_PATTERN.fullmatch(source_run_id):
        raise VoicePreferenceTournamentError("invalid source blind-test run ID")
    return root / blind.OUTPUT_DIRECTORY / source_run_id / "review"


def _load_source(
    root: Path,
    source_run_id: str,
    *,
    expected_manifest_sha256: str,
    expected_manifest_byte_sha256: str,
) -> tuple[Path, dict[str, Any], bytes]:
    review_directory = _source_review_path(root, source_run_id)
    base._require_safe_existing_path(
        root, review_directory, label="source review directory", directory=True
    )
    manifest_path = review_directory / "manifest.json"
    source, payload = base._read_json(root, manifest_path, label="source review manifest")
    _expect(source.get("schema_version"), blind.PUBLIC_SCHEMA, label="source schema")
    _expect(source.get("blind_test_run_id"), source_run_id, label="source run ID")
    _expect(
        source.get("status"),
        "ready_for_local_human_blind_review",
        label="source review status",
    )
    verified_semantic = base._verify_semantic_hash(
        source, field="manifest_sha256", label="source review manifest"
    )
    _expect(
        verified_semantic,
        base._require_sha256(expected_manifest_sha256, label="expected source semantic SHA-256"),
        label="source manifest semantic SHA-256",
    )
    _expect(
        base._sha256_bytes(payload),
        base._require_sha256(
            expected_manifest_byte_sha256, label="expected source byte SHA-256"
        ),
        label="source manifest byte SHA-256",
    )
    privacy = _object(source.get("privacy_contract"), label="source privacy contract")
    for key in (
        "candidate_mapping_included",
        "provider_voice_ids_included",
        "candidate_a_b_labels_included",
        "publication_authorized",
    ):
        _expect(privacy.get(key), False, label=f"source privacy {key}")
    return review_directory, source, payload


def _style(case: dict[str, Any]) -> str:
    category = str(case.get("category", ""))
    if category == "neutral_short":
        return "neutral"
    if category == "restrained_breathy_lexical":
        return "restrained_breathy_lexical"
    if category.startswith("heightened_"):
        return "heightened"
    raise VoicePreferenceTournamentError(f"unexpected anchor category: {category!r}")


def _selected_cases(
    root: Path,
    source_review_directory: Path,
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    source_cases = _array(source.get("cases"), label="source cases")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in source_cases:
        case = _object(raw, label="source case")
        case_id = base._require_string(case.get("case_id"), label="source case ID")
        if case_id in by_id:
            raise VoicePreferenceTournamentError(f"duplicate source case ID: {case_id}")
        by_id[case_id] = case

    selected: list[dict[str, Any]] = []
    audio_payloads: dict[str, bytes] = {}
    for sequence, case_id in enumerate(ROUND_ONE_CASE_IDS, start=1):
        if case_id not in by_id:
            raise VoicePreferenceTournamentError(f"source case is missing: {case_id}")
        case = by_id[case_id]
        samples = _array(case.get("samples"), label=f"{case_id} samples")
        _expect(len(samples), 2, label=f"{case_id} sample count")
        output_samples: list[dict[str, Any]] = []
        for display_order, raw_sample in enumerate(samples, start=1):
            sample = _object(raw_sample, label=f"{case_id} sample")
            opaque_id = base._require_string(
                sample.get("opaque_label_id"), label=f"{case_id} opaque label"
            )
            if not blind.OPAQUE_ID_PATTERN.fullmatch(opaque_id):
                raise VoicePreferenceTournamentError(f"invalid opaque label: {opaque_id!r}")
            source_relative = base._safe_relative_path(
                sample.get("audio_relative_path"), label=f"{case_id} source audio path"
            )
            source_path = source_review_directory.joinpath(*source_relative.parts)
            payload = base._read_stable_bytes(root, source_path, label=f"{case_id} source audio")
            wav_sha256 = base._require_sha256(
                sample.get("wav_sha256"), label=f"{case_id} WAV SHA-256"
            )
            _expect(base._sha256_bytes(payload), wav_sha256, label=f"{case_id} WAV SHA-256")
            metrics = blind._validate_wav_bytes(payload)
            _expect(
                metrics["full_scale_sample_count"],
                int(sample.get("full_scale_sample_count", -1)),
                label=f"{case_id} full-scale sample count",
            )
            output_relative = (
                f"audio/{case.get('character_slug')}/{sequence:02d}-{opaque_id}.wav"
            )
            if output_relative in audio_payloads:
                raise VoicePreferenceTournamentError("duplicate preference-round audio path")
            audio_payloads[output_relative] = payload
            output_samples.append(
                {
                    "display_order": display_order,
                    "opaque_label_id": opaque_id,
                    "display_label": sample.get("display_label"),
                    "audio_relative_path": output_relative,
                    "duration_seconds": metrics["duration_seconds"],
                    "wav_sha256": wav_sha256,
                    "full_scale_sample_count": metrics["full_scale_sample_count"],
                }
            )
        selected.append(
            {
                "sequence": sequence,
                "character_slug": case.get("character_slug"),
                "runtime_character_name": case.get("runtime_character_name"),
                "case_id": case_id,
                "source_case_index": case.get("case_index"),
                "style_anchor": _style(case),
                "text": case.get("text"),
                "samples": output_samples,
            }
        )

    character_order = tuple(dict.fromkeys(item["character_slug"] for item in selected))
    _expect(character_order, EXPECTED_CHARACTER_ORDER, label="character order")
    for character in EXPECTED_CHARACTER_ORDER:
        styles = tuple(
            item["style_anchor"] for item in selected if item["character_slug"] == character
        )
        _expect(styles, EXPECTED_STYLE_ORDER, label=f"{character} style anchors")
    _expect(len(audio_payloads), 12, label="preference-round audio count")
    _expect(
        len({base._sha256_bytes(payload) for payload in audio_payloads.values()}),
        12,
        label="preference-round unique audio count",
    )
    return selected, audio_payloads


def build_round(
    voice_root: Path,
    *,
    source_run_id: str,
    expected_source_manifest_sha256: str,
    expected_source_manifest_byte_sha256: str,
    prepared_at: str,
) -> tuple[dict[str, Any], dict[str, bytes], Path]:
    root = base._absolute_lexical(voice_root)
    base._require_safe_existing_path(root, root, label="voice root", directory=True)
    timestamp = _validate_timestamp(prepared_at)
    source_directory, source, source_payload = _load_source(
        root,
        source_run_id,
        expected_manifest_sha256=expected_source_manifest_sha256,
        expected_manifest_byte_sha256=expected_source_manifest_byte_sha256,
    )
    cases, audio_payloads = _selected_cases(root, source_directory, source)
    identity_basis = {
        "schema_version": SCHEMA,
        "policy_version": POLICY_VERSION,
        "source_manifest_sha256": source["manifest_sha256"],
        "round_index": 1,
        "case_ids": list(ROUND_ONE_CASE_IDS),
        "prepared_at": timestamp,
    }
    round_id = f"voice-preference-round-{base._semantic_sha256(identity_basis)[:20]}"
    _expect(bool(ROUND_ID_PATTERN.fullmatch(round_id)), True, label="round ID")
    document: dict[str, Any] = {
        "schema_version": SCHEMA,
        "decision_submission_schema_version": SUBMISSION_SCHEMA,
        "policy_version": POLICY_VERSION,
        "round_id": round_id,
        "round_index": 1,
        "prepared_at": timestamp,
        "status": "awaiting_local_pairwise_rejection_decisions",
        "source": {
            "blind_test_run_id": source_run_id,
            "review_manifest_relative_path": (
                f"{blind.OUTPUT_DIRECTORY}/{source_run_id}/review/manifest.json"
            ),
            "review_manifest_sha256": source["manifest_sha256"],
            "review_manifest_byte_sha256": base._sha256_bytes(source_payload),
        },
        "privacy_contract": {
            "candidate_mapping_included": False,
            "provider_voice_ids_included": False,
            "candidate_a_b_labels_included": False,
            "workspace_id_included": False,
            "local_review_only": True,
            "publication_authorized": False,
        },
        "generation_contract": {
            "provider_calls_performed_for_this_round": False,
            "new_synthesis_outputs_created": 0,
            "reused_existing_blind_outputs": 12,
            "incremental_provider_cost_usd": "0",
            "next_provider_generation_requires_complete_human_submission": True,
            "provider_learns_from_rejection": False,
        },
        "decision_contract": {
            "decision_type": "pairwise_preference_with_absolute_usability_gate",
            "choices": list(CHOICES),
            "usability_values": list(USABILITY_VALUES),
            "rejection_reasons": list(REJECTION_REASONS),
            "numeric_scoring_used": False,
            "relative_winner_may_still_be_not_usable": True,
            "consecutive_champion_wins_required_before_final_acceptance": 3,
            "unseen_validation_required_before_final_acceptance": True,
            "same_voice_identity_rejection_limit_before_reenrollment": 2,
        },
        "paralinguistic_event_lane": {
            "ordinals": [2, 3],
            "included": False,
            "event_bank_eligibility": "pending_human_event_qa",
        },
        "cases": cases,
    }
    document["manifest_sha256"] = base._semantic_sha256(document)
    destination = root / OUTPUT_DIRECTORY / round_id / "review"
    return document, audio_payloads, destination


def _review_html(document: dict[str, Any]) -> str:
    embedded = json.dumps(document, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    title = html.escape(f"Project Snow 相对音色判断 · 第 {document['round_index']} 轮")
    if document["round_index"] == 1:
        round_note = (
            "本轮复用既有盲测中的中性、轻声、激动三类锚点，没有新增百炼调用或费用。"
            "完成并导出判断后，下一轮才会依据否定原因定向生成挑战者。"
        )
        submission_binding = "review.source.review_manifest_sha256"
    else:
        generation = _object(
            document.get("generation_contract"), label="round generation contract"
        )
        if generation.get("unseen_validation_text") is True:
            round_note = (
                "本轮是当前两个现有克隆的终局未见台词验证。提交后不再自动生成同类 A/B 轮次："
                "可用的相对胜者直接锁定为对应角色与语态槽位的候选；相对胜者仍不可用或两条都否时，"
                "该槽位记为暂不合格并暂停，不以再次抽样规避结论。只有更换源素材、模型，或用户明确"
                "要求重开时才建立新实验。百炼不会读取本地判断，本地流程只据此形成最终采用结论。"
            )
        else:
            round_note = (
                f"本轮将上一轮保留样本与 {int(generation.get('new_synthesis_outputs_created', 0))} "
                "条新挑战者重新盲排比较。百炼不会读取本地否定判断；判断只决定下一次本地选择与生成动作。"
            )
        submission_binding = "review.manifest_sha256"
    template = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f3f6fb;--card:#fff;--line:#dce3ee;--ink:#172033;--muted:#5d687b;--accent:#3157d5;--bad:#a93030;--ok:#19764a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,sans-serif}
main{max-width:1080px;margin:28px auto;padding:0 18px 56px}h1{font-size:25px}h2{margin:0 0 5px}
.notice{background:#fff5d9;border-left:4px solid #ce8b00;padding:13px 15px;border-radius:8px;margin:14px 0}
.toolbar{position:sticky;top:0;z-index:5;background:rgba(243,246,251,.95);padding:10px 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
button{border:0;border-radius:8px;background:var(--accent);color:#fff;padding:9px 14px;cursor:pointer}button.secondary{background:#65718a}
section.case{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:18px 0}.meta{color:var(--muted);font-size:13px}
.prompt{font-size:17px;background:#f7f9fd;padding:12px;border-radius:8px}.pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
article.sample{border:1px solid var(--line);border-radius:11px;padding:14px}audio{width:100%}.choice-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:15px}
.choice{display:flex;align-items:center;justify-content:center;gap:7px;border:1px solid #aeb9cb;border-radius:9px;padding:10px;background:#f9fbff;cursor:pointer}.choice.reject{border-color:#d49a9a;color:var(--bad)}
.absolute,.reasons{margin-top:13px;padding-top:12px;border-top:1px solid var(--line)}.absolute label,.reasons label{display:inline-flex;align-items:center;gap:6px;margin:5px 15px 5px 0}
.case-error{color:var(--bad);font-weight:600;min-height:1.5em}.status{color:var(--muted)}.progress{font-weight:600}.complete{color:var(--ok)}
@media(max-width:700px){.pair{grid-template-columns:1fr}.choice-grid{grid-template-columns:1fr}}
</style></head><body><main>
<h1>__TITLE__</h1>
<div class="notice">不需要打分。每题只判断哪一条相对更好，或两条都否；相对更好不等于已经可用。页面不含 A/B 映射、Provider 音色 ID 或 Workspace ID。</div>
<p>__ROUND_NOTE__</p>
<div class="toolbar"><button id="save">保存到本机</button><button id="export">验证并导出判断 JSON</button><button class="secondary" id="clear">清空本轮</button><span class="progress" id="progress"></span><span class="status" id="status"></span></div>
<div id="cases"></div>
<script type="application/json" id="review-data">__EMBEDDED__</script>
<script>
const review=JSON.parse(document.getElementById('review-data').textContent);
const key='project-snow-preference:'+review.round_id;
const root=document.getElementById('cases');
const esc=s=>String(s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
const styleLabels={neutral:'中性',restrained_breathy_lexical:'亲密轻声（有词）',heightened:'激动'};
const reasonLabels={wrong_voice_identity:'不像本人',wrong_expression_or_character_fit:'语气或角色感不对',pronunciation_or_segmentation_error:'咬字或断句错误',synthesis_artifact:'机械感、爆音或接缝'};
for(const c of review.cases){
 const section=document.createElement('section');section.className='case';section.dataset.caseId=c.case_id;
 const samples=c.samples.map((s,i)=>`<article class="sample" data-label="${esc(s.opaque_label_id)}"><h3>${i+1} · ${esc(s.display_label)}</h3><audio controls preload="metadata" src="${esc(s.audio_relative_path)}"></audio><p class="meta">${Number(s.duration_seconds).toFixed(3)} 秒 · WAV ${esc(s.wav_sha256.slice(0,12))}…</p></article>`).join('');
 const reasons=review.decision_contract.rejection_reasons.map(r=>`<label><input type="checkbox" data-reason="${esc(r)}"> ${esc(reasonLabels[r]||r)}</label>`).join('');
 section.innerHTML=`<h2>${esc(c.runtime_character_name)} · ${esc(styleLabels[c.style_anchor]||c.style_anchor)}</h2><p class="meta">${esc(c.case_id)}</p><p class="prompt">${esc(c.text)}</p><div class="pair">${samples}</div><div class="choice-grid"><label class="choice"><input type="radio" name="choice-${esc(c.case_id)}" value="first_sample"> 第一个更好</label><label class="choice"><input type="radio" name="choice-${esc(c.case_id)}" value="second_sample"> 第二个更好</label><label class="choice reject"><input type="radio" name="choice-${esc(c.case_id)}" value="reject_both"> 两个都否</label></div><div class="absolute"><strong>相对胜者能否直接用于项目？</strong><br><label><input type="radio" name="usable-${esc(c.case_id)}" value="usable"> 能</label><label><input type="radio" name="usable-${esc(c.case_id)}" value="not_usable"> 不能</label></div><div class="reasons"><strong>如果不能用，请选择原因</strong><br>${reasons}</div><div class="case-error" data-error></div>`;
 section.querySelectorAll('input').forEach(input=>input.addEventListener('change',()=>{syncCase(section);updateProgress()}));
 root.appendChild(section);
}
function selected(section,name){return section.querySelector(`input[name="${name}-${CSS.escape(section.dataset.caseId)}"]:checked`)?.value||''}
function syncCase(section){const choice=selected(section,'choice');const usability=[...section.querySelectorAll(`input[name="usable-${CSS.escape(section.dataset.caseId)}"]`)];if(choice==='reject_both'){usability.forEach(x=>{x.checked=x.value==='not_usable';x.disabled=true})}else{usability.forEach(x=>x.disabled=false)}section.querySelector('[data-error]').textContent=''}
function collect(){const decisions={};document.querySelectorAll('section.case').forEach(section=>{const choice=selected(section,'choice');const samples=[...section.querySelectorAll('article.sample')].map(x=>x.dataset.label);const selectedLabel=choice==='first_sample'?samples[0]:choice==='second_sample'?samples[1]:null;decisions[section.dataset.caseId]={relative_choice:choice||null,selected_opaque_label_id:selectedLabel,winning_sample_usable:selected(section,'usable')||null,rejection_reasons:[...section.querySelectorAll('[data-reason]:checked')].map(x=>x.dataset.reason)}});return {schema_version:review.decision_submission_schema_version,round_id:review.round_id,source_review_manifest_sha256:__SUBMISSION_BINDING__,saved_at:new Date().toISOString(),decisions}}
function validate(data,show){const errors=[];for(const c of review.cases){const item=data.decisions[c.case_id];let message='';if(!item?.relative_choice)message='请选择相对结果。';else if(!item.winning_sample_usable)message='请选择相对胜者能否使用。';else if((item.relative_choice==='reject_both'||item.winning_sample_usable==='not_usable')&&!(item.rejection_reasons||[]).length)message='不能使用时至少选择一个原因。';if(message)errors.push(c.case_id);if(show)document.querySelector(`section[data-case-id="${CSS.escape(c.case_id)}"] [data-error]`).textContent=message}return errors}
function apply(data){if(!data||data.round_id!==review.round_id)return;for(const [caseId,item] of Object.entries(data.decisions||{})){const section=document.querySelector(`section[data-case-id="${CSS.escape(caseId)}"]`);if(!section)continue;const choice=section.querySelector(`input[name="choice-${CSS.escape(caseId)}"][value="${CSS.escape(item.relative_choice||'')}"]`);if(choice)choice.checked=true;const usable=section.querySelector(`input[name="usable-${CSS.escape(caseId)}"][value="${CSS.escape(item.winning_sample_usable||'')}"]`);if(usable)usable.checked=true;section.querySelectorAll('[data-reason]').forEach(x=>x.checked=(item.rejection_reasons||[]).includes(x.dataset.reason));syncCase(section)}}
function updateProgress(){const data=collect();const done=review.cases.length-validate(data,false).length;const p=document.getElementById('progress');p.textContent=`已完成 ${done}/${review.cases.length}`;p.className='progress'+(done===review.cases.length?' complete':'')}
function setStatus(text){document.getElementById('status').textContent=text}
document.getElementById('save').onclick=()=>{localStorage.setItem(key,JSON.stringify(collect()));setStatus('已保存到本机');updateProgress()};
document.getElementById('export').onclick=()=>{const data=collect();const errors=validate(data,true);if(errors.length){setStatus(`还有 ${errors.length} 题未完成`);document.querySelector(`section[data-case-id="${CSS.escape(errors[0])}"]`).scrollIntoView({behavior:'smooth',block:'center'});return}localStorage.setItem(key,JSON.stringify(data));const blob=new Blob([JSON.stringify(data,null,2)+'\\n'],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=review.round_id+'-decisions.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);setStatus('判断 JSON 已导出')};
document.getElementById('clear').onclick=()=>{if(confirm('确认清空本轮判断？')){localStorage.removeItem(key);location.reload()}};
try{const saved=localStorage.getItem(key);if(saved)apply(JSON.parse(saved));setStatus(saved?'已载入本机判断':'尚未保存判断')}catch(error){setStatus('本机判断无法读取')}updateProgress();
</script></main></body></html>
"""
    return (
        template.replace("__TITLE__", title)
        .replace("__ROUND_NOTE__", html.escape(round_note))
        .replace("__SUBMISSION_BINDING__", submission_binding)
        .replace("__EMBEDDED__", embedded)
    )


def _assert_public_privacy(payloads: list[bytes]) -> None:
    combined = b"\n".join(payloads).decode("utf-8")
    forbidden = (
        '"provider_voice_id"',
        '"operator_only_candidate_mapping"',
        '"operator_only_candidate_map"',
        '"workspace_id"',
        '"vidya-a"',
        '"vidya-b"',
        '"chenxing-a"',
        '"chenxing-b"',
    )
    for token in forbidden:
        if token in combined:
            raise VoicePreferenceTournamentError(
                "preference-round package contains private operator data"
            )


def write_round(
    voice_root: Path,
    document: dict[str, Any],
    audio_payloads: dict[str, bytes],
    destination: Path,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    output_root = _new_directory(root, root / OUTPUT_DIRECTORY, label="tournament root")
    run_directory = _new_directory(
        root, output_root / document["round_id"], label="tournament run"
    )
    review_directory = _new_directory(root, run_directory / "review", label="round review")
    for character in EXPECTED_CHARACTER_ORDER:
        audio_root = _new_directory(root, review_directory / "audio", label="audio root")
        _new_directory(root, audio_root / character, label=f"{character} audio directory")

    manifest_payload = _pretty_json_bytes(document)
    html_payload = _review_html(document).encode("utf-8")
    _assert_public_privacy([manifest_payload, html_payload])
    write_states: list[str] = []
    for relative, payload in sorted(audio_payloads.items()):
        path = review_directory.joinpath(*relative.split("/"))
        write_states.append(
            _write_or_verify(root, path, payload, label=f"round audio {relative}")
        )
    write_states.append(
        _write_or_verify(
            root, review_directory / "manifest.json", manifest_payload, label="round manifest"
        )
    )
    write_states.append(
        _write_or_verify(root, review_directory / "review.html", html_payload, label="round HTML")
    )
    return {
        "status": document["status"],
        "write_status": (
            "existing_identical" if set(write_states) == {"existing_identical"} else "written"
        ),
        "round_id": document["round_id"],
        "review_html_path": str(review_directory / "review.html"),
        "review_manifest_path": str(review_directory / "manifest.json"),
        "review_manifest_sha256": document["manifest_sha256"],
        "reused_audio_count": len(audio_payloads),
        "new_provider_outputs": 0,
        "provider_calls_performed": False,
        "incremental_provider_cost_usd": "0",
    }


def validate_round(voice_root: Path, round_id: str) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    if not ROUND_ID_PATTERN.fullmatch(round_id):
        raise VoicePreferenceTournamentError("invalid preference round ID")
    review_directory = root / OUTPUT_DIRECTORY / round_id / "review"
    manifest, payload = base._read_json(
        root, review_directory / "manifest.json", label="round manifest"
    )
    _expect(manifest.get("schema_version"), SCHEMA, label="round schema")
    _expect(manifest.get("round_id"), round_id, label="round ID")
    semantic = base._verify_semantic_hash(
        manifest, field="manifest_sha256", label="round manifest"
    )
    cases = _array(manifest.get("cases"), label="round cases")
    _expect(len(cases), 6, label="round case count")
    wav_hashes: set[str] = set()
    full_scale_count = 0
    for case in cases:
        case_object = _object(case, label="round case")
        samples = _array(case_object.get("samples"), label="round samples")
        _expect(len(samples), 2, label="round pair size")
        for sample in samples:
            sample_object = _object(sample, label="round sample")
            relative = base._safe_relative_path(
                sample_object.get("audio_relative_path"), label="round audio path"
            )
            wav_payload = base._read_stable_bytes(
                root,
                review_directory.joinpath(*relative.parts),
                label="round audio",
            )
            wav_sha = base._sha256_bytes(wav_payload)
            _expect(wav_sha, sample_object.get("wav_sha256"), label="round audio SHA-256")
            metrics = blind._validate_wav_bytes(wav_payload)
            _expect(
                metrics["full_scale_sample_count"],
                sample_object.get("full_scale_sample_count"),
                label="round audio full-scale count",
            )
            wav_hashes.add(wav_sha)
            full_scale_count += metrics["full_scale_sample_count"]
    _expect(len(wav_hashes), 12, label="unique round WAV count")
    expected_html = _review_html(manifest).encode("utf-8")
    actual_html = base._read_stable_bytes(
        root, review_directory / "review.html", label="round HTML"
    )
    _expect(actual_html, expected_html, label="round HTML")
    _assert_public_privacy([payload, actual_html])
    generation = _object(
        manifest.get("generation_contract"), label="round generation contract"
    )
    return {
        "status": manifest["status"],
        "round_id": round_id,
        "manifest_sha256": semantic,
        "manifest_byte_sha256": base._sha256_bytes(payload),
        "case_count": len(cases),
        "audio_count": sum(len(item["samples"]) for item in cases),
        "unique_wav_count": len(wav_hashes),
        "full_scale_sample_count": full_scale_count,
        "numeric_scoring_used": False,
        "provider_calls_performed": bool(
            generation.get("provider_calls_performed_for_this_round")
        ),
        "incremental_provider_cost_usd": str(
            generation.get("incremental_provider_cost_usd", "0")
        ),
    }


def _load_round_manifest(root: Path, round_id: str) -> tuple[dict[str, Any], bytes, Path]:
    if not ROUND_ID_PATTERN.fullmatch(round_id):
        raise VoicePreferenceTournamentError("invalid preference round ID")
    review_directory = root / OUTPUT_DIRECTORY / round_id / "review"
    manifest, payload = base._read_json(
        root, review_directory / "manifest.json", label="round manifest"
    )
    _expect(manifest.get("schema_version"), SCHEMA, label="round schema")
    _expect(manifest.get("round_id"), round_id, label="round ID")
    base._verify_semantic_hash(manifest, field="manifest_sha256", label="round manifest")
    return manifest, payload, review_directory


def _submission_binding(manifest: dict[str, Any]) -> str:
    if int(manifest.get("round_index", 0)) == 1:
        source = _object(manifest.get("source"), label="round source")
        return base._require_sha256(
            source.get("review_manifest_sha256"),
            label="round source review manifest SHA-256",
        )
    return base._require_sha256(
        manifest.get("manifest_sha256"), label="round manifest SHA-256"
    )


def _normalize_decisions(
    manifest: dict[str, Any], raw_decisions: Any
) -> dict[str, dict[str, Any]]:
    decisions = _object(raw_decisions, label="decision set")
    cases = _array(manifest.get("cases"), label="round cases")
    case_ids = [
        base._require_string(_object(item, label="round case").get("case_id"), label="case ID")
        for item in cases
    ]
    _expect(set(decisions), set(case_ids), label="decision case IDs")
    normalized: dict[str, dict[str, Any]] = {}
    for case in cases:
        case_object = _object(case, label="round case")
        case_id = base._require_string(case_object.get("case_id"), label="case ID")
        item = _object(decisions.get(case_id), label=f"{case_id} decision")
        _expect(
            set(item),
            {
                "relative_choice",
                "selected_opaque_label_id",
                "winning_sample_usable",
                "rejection_reasons",
            },
            label=f"{case_id} decision fields",
        )
        choice = base._require_string(
            item.get("relative_choice"), label=f"{case_id} relative choice"
        )
        if choice not in CHOICES:
            raise VoicePreferenceTournamentError(f"{case_id} has an invalid relative choice")
        usability = base._require_string(
            item.get("winning_sample_usable"), label=f"{case_id} usability"
        )
        if usability not in USABILITY_VALUES:
            raise VoicePreferenceTournamentError(f"{case_id} has an invalid usability value")
        reasons = _array(item.get("rejection_reasons"), label=f"{case_id} rejection reasons")
        if len(reasons) != len(set(reasons)):
            raise VoicePreferenceTournamentError(f"{case_id} has duplicate rejection reasons")
        if any(reason not in REJECTION_REASONS for reason in reasons):
            raise VoicePreferenceTournamentError(f"{case_id} has an invalid rejection reason")
        ordered_reasons = [reason for reason in REJECTION_REASONS if reason in reasons]
        samples = _array(case_object.get("samples"), label=f"{case_id} samples")
        _expect(len(samples), 2, label=f"{case_id} pair size")
        labels = [
            base._require_string(
                _object(sample, label=f"{case_id} sample").get("opaque_label_id"),
                label=f"{case_id} opaque label",
            )
            for sample in samples
        ]
        expected_selected = None
        if choice == "first_sample":
            expected_selected = labels[0]
        elif choice == "second_sample":
            expected_selected = labels[1]
        _expect(
            item.get("selected_opaque_label_id"),
            expected_selected,
            label=f"{case_id} selected opaque label",
        )
        if choice == "reject_both" and usability != "not_usable":
            raise VoicePreferenceTournamentError(
                f"{case_id} reject-both decision must be marked not usable"
            )
        if usability == "not_usable" and not ordered_reasons:
            raise VoicePreferenceTournamentError(
                f"{case_id} not-usable decision requires a rejection reason"
            )
        if usability == "usable" and ordered_reasons:
            raise VoicePreferenceTournamentError(
                f"{case_id} usable decision must not include rejection reasons"
            )
        normalized[case_id] = {
            "relative_choice": choice,
            "selected_opaque_label_id": expected_selected,
            "winning_sample_usable": usability,
            "rejection_reasons": ordered_reasons,
        }
    return normalized


def _decision_summary(decisions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "case_count": len(decisions),
        "relative_choice_counts": {
            choice: sum(item["relative_choice"] == choice for item in decisions.values())
            for choice in CHOICES
        },
        "usable_case_count": sum(
            item["winning_sample_usable"] == "usable" for item in decisions.values()
        ),
        "not_usable_case_count": sum(
            item["winning_sample_usable"] == "not_usable"
            for item in decisions.values()
        ),
        "rejection_reason_counts": {
            reason: sum(reason in item["rejection_reasons"] for item in decisions.values())
            for reason in REJECTION_REASONS
        },
    }


def build_decision_receipt(
    voice_root: Path,
    round_id: str,
    submission_path: Path,
) -> tuple[dict[str, Any], Path]:
    root = base._absolute_lexical(voice_root)
    validate_round(root, round_id)
    manifest, round_payload, _ = _load_round_manifest(root, round_id)
    submission, submission_payload = _read_external_json(
        submission_path, label="decision submission"
    )
    _expect(
        submission.get("schema_version"), SUBMISSION_SCHEMA, label="submission schema"
    )
    _expect(submission.get("round_id"), round_id, label="submission round ID")
    _expect(
        submission.get("source_review_manifest_sha256"),
        _submission_binding(manifest),
        label="submission manifest binding",
    )
    saved_at = base._require_string(
        submission.get("saved_at"), label="submission saved timestamp"
    )
    _validate_timestamp(saved_at)
    decisions = _normalize_decisions(manifest, submission.get("decisions"))
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "round_id": round_id,
        "round_index": int(manifest["round_index"]),
        "source_round_manifest_sha256": manifest["manifest_sha256"],
        "source_round_manifest_byte_sha256": base._sha256_bytes(round_payload),
        "submission_schema_version": SUBMISSION_SCHEMA,
        "submission_manifest_binding_sha256": _submission_binding(manifest),
        "submission_saved_at": saved_at,
        "submission_byte_sha256": base._sha256_bytes(submission_payload),
        "decision_set_sha256": base._semantic_sha256(decisions),
        "decisions": decisions,
        "summary": _decision_summary(decisions),
    }
    receipt["receipt_sha256"] = base._semantic_sha256(receipt)
    destination = root / OUTPUT_DIRECTORY / round_id / "operator" / "decision-receipt.json"
    return receipt, destination


def ingest_decision_receipt(
    voice_root: Path,
    round_id: str,
    submission_path: Path,
) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    receipt, destination = build_decision_receipt(root, round_id, submission_path)
    operator_directory = _new_directory(
        root, destination.parent, label="round operator directory"
    )
    _expect(operator_directory, destination.parent, label="round operator directory")
    state = _write_or_verify(
        root,
        destination,
        _pretty_json_bytes(receipt),
        label="decision receipt",
    )
    return {
        "status": "complete_human_decision_received",
        "write_status": state,
        "round_id": round_id,
        "receipt_path": str(destination),
        "receipt_sha256": receipt["receipt_sha256"],
        "decision_set_sha256": receipt["decision_set_sha256"],
        "summary": receipt["summary"],
        "provider_calls_performed": False,
        "incremental_provider_cost_usd": "0",
    }


def validate_decision_receipt(voice_root: Path, round_id: str) -> dict[str, Any]:
    root = base._absolute_lexical(voice_root)
    manifest, round_payload, _ = _load_round_manifest(root, round_id)
    path = root / OUTPUT_DIRECTORY / round_id / "operator" / "decision-receipt.json"
    receipt, payload = base._read_json(root, path, label="decision receipt")
    _expect(receipt.get("schema_version"), RECEIPT_SCHEMA, label="receipt schema")
    _expect(receipt.get("round_id"), round_id, label="receipt round ID")
    _expect(
        receipt.get("round_index"), int(manifest["round_index"]), label="receipt round index"
    )
    _expect(
        receipt.get("source_round_manifest_sha256"),
        manifest["manifest_sha256"],
        label="receipt source manifest SHA-256",
    )
    _expect(
        receipt.get("source_round_manifest_byte_sha256"),
        base._sha256_bytes(round_payload),
        label="receipt source manifest byte SHA-256",
    )
    _expect(
        receipt.get("submission_manifest_binding_sha256"),
        _submission_binding(manifest),
        label="receipt submission binding",
    )
    decisions = _normalize_decisions(manifest, receipt.get("decisions"))
    _expect(
        receipt.get("decision_set_sha256"),
        base._semantic_sha256(decisions),
        label="receipt decision-set SHA-256",
    )
    _expect(receipt.get("summary"), _decision_summary(decisions), label="receipt summary")
    semantic = base._verify_semantic_hash(
        receipt, field="receipt_sha256", label="decision receipt"
    )
    return {
        "status": "complete_human_decision_received",
        "round_id": round_id,
        "receipt_path": str(path),
        "receipt_sha256": semantic,
        "receipt_byte_sha256": base._sha256_bytes(payload),
        "decision_set_sha256": receipt["decision_set_sha256"],
        "summary": receipt["summary"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="prepare local preference round one")
    prepare.add_argument("--source-run-id", default=DEFAULT_SOURCE_RUN_ID)
    prepare.add_argument(
        "--expect-source-manifest-sha256", default=DEFAULT_SOURCE_MANIFEST_SHA256
    )
    prepare.add_argument(
        "--expect-source-manifest-byte-sha256",
        default=DEFAULT_SOURCE_MANIFEST_BYTE_SHA256,
    )
    prepare.add_argument("--prepared-at", required=True)
    prepare.add_argument("--execute", action="store_true")
    validate = commands.add_parser("validate", help="validate a local preference round")
    validate.add_argument("--round-id", required=True)
    ingest = commands.add_parser("ingest", help="validate and receive an exported decision JSON")
    ingest.add_argument("--round-id", required=True)
    ingest.add_argument("--submission", type=Path, required=True)
    ingest.add_argument("--execute", action="store_true")
    validate_receipt = commands.add_parser(
        "validate-receipt", help="validate the immutable decision receipt"
    )
    validate_receipt.add_argument("--round-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            document, audio_payloads, destination = build_round(
                arguments.voice_root,
                source_run_id=arguments.source_run_id,
                expected_source_manifest_sha256=arguments.expect_source_manifest_sha256,
                expected_source_manifest_byte_sha256=(
                    arguments.expect_source_manifest_byte_sha256
                ),
                prepared_at=arguments.prepared_at,
            )
            if arguments.execute:
                result = write_round(
                    arguments.voice_root, document, audio_payloads, destination
                )
            else:
                result = {
                    "status": "dry_run",
                    "round_id": document["round_id"],
                    "review_path": str(destination / "review.html"),
                    "manifest_sha256": document["manifest_sha256"],
                    "reused_audio_count": len(audio_payloads),
                    "new_provider_outputs": 0,
                    "provider_calls_performed": False,
                    "incremental_provider_cost_usd": "0",
                }
        elif arguments.command == "validate":
            result = validate_round(arguments.voice_root, arguments.round_id)
        elif arguments.command == "ingest" and not arguments.execute:
            receipt, destination = build_decision_receipt(
                arguments.voice_root, arguments.round_id, arguments.submission
            )
            result = {
                "status": "dry_run_complete_human_decision",
                "round_id": arguments.round_id,
                "receipt_path": str(destination),
                "receipt_sha256": receipt["receipt_sha256"],
                "decision_set_sha256": receipt["decision_set_sha256"],
                "summary": receipt["summary"],
                "provider_calls_performed": False,
                "incremental_provider_cost_usd": "0",
            }
        elif arguments.command == "ingest":
            result = ingest_decision_receipt(
                arguments.voice_root, arguments.round_id, arguments.submission
            )
        else:
            result = validate_decision_receipt(arguments.voice_root, arguments.round_id)
    except (OSError, VoicePreferenceTournamentError, blind.VoiceProviderBlindTestError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
