"""Independent, evidence-constrained second review for relation candidates.

This pipeline is intentionally separated from extraction and human approval:

* it reads only ``App/runtime`` artifacts;
* it writes model-review reports below ``App/runtime/review``;
* it never changes candidate ``review_status`` values; and
* it never writes a graph edge.

An OpenAI-compatible provider such as DeepSeek can be configured with the
``RELATION_REVIEW_*`` variables.  The provider receives a proposed triple and
its source evidence, not the first model's confidence or rationale, so it can
act as an independent evidence judge rather than echoing the extractor.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from backend.snow_app.repository import _review_group_id

from .common import RUNTIME_ROOT, load_runtime_jsonl, stable_id, utc_now, write_json, write_jsonl
from .extract_relation_candidates import (
    ProviderCallFailure,
    _compact_text,
    _is_retriable_provider_error,
    _message_text,
    _parse_relation_payload,
    load_relation_environment,
)


REVIEW_POLICY_VERSION = "secondary-evidence-review-v4"
REPORT_FILENAME = "relation_model_review_reports.jsonl"
REPORT_SUMMARY_FILENAME = "review_relation_candidates.json"

VERDICTS = {"recommend_approve", "recommend_reject", "abstain"}
EVIDENCE_SUFFICIENCY = {"direct", "partial", "insufficient"}
IDENTITY_CONFIDENCE = {"exact_literal", "ambiguous", "unmapped"}
TEMPORAL_SCOPES = {"stable", "situational", "costume_specific", "unknown"}

_RELATION_TARGET_CONSTRAINTS = {
    "ALLY_OF": "客体必须是明确命名的角色、发件人或敌方实体。",
    "OPPOSES": "客体必须是明确命名的角色、发件人或敌方实体。",
    "HAS_RELATIONSHIP_CONTEXT": "客体必须是分析员或明确命名的角色、发件人或敌方实体。",
    "HAS_PREFERENCE": "客体必须是可指认的物品、活动、食物、地点或价值偏好，不能是角色或分析员。",
    "OWNS_ITEM": "客体必须是可指认的物品类实体。",
    "PARTICIPATES_IN_EVENT": "客体必须是已发生、可命名的剧情事件、行动或任务。",
    "VISITS_LOCATION": "客体必须是可指认的地点。",
    "MENTIONS": "仅表示被直接提及；它通常没有进入长期叙事图谱的价值。",
}
_CONTEXT_SENSITIVE_RELATIONS = {"ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT"}
_CONTEXT_SENSITIVE_SOURCE_TYPES = {
    "special_mail",
    "random_event",
    "character_costume",
    "birthday_content",
    "event_lore",
}


REVIEW_SYSTEM_PROMPT = """你是《尘白禁区》叙事知识图谱的独立证据审核员。

你的工作不是抽取新关系，也不是复述或相信上一阶段模型的判断。你只能审查输入中给出的一个候选三元组和它的原始证据。没有直接、明确、可定位的证据时，必须选择 abstain；不要依赖游戏常识、角色记忆、剧情常识、标题暗示、图片、网页导航、或未提供的上下文。

严格规则：
1. 只判断候选中的 subject - relation_type - object，不得改写实体、补全代词、合并别名、把昵称擅自映射为人物，或把“她/他/你/队长/郎君”等称谓补成实体。唯一例外是原文和输入上下文明确写为“分析员”。
2. 只能把原文中逐字可找到的内容作为 supporting_quote。不能改写、拼接、概括、截断成不完整短语，不能引用输入中不存在的文字。
3. 页面、章节、邮件、语音、随机事件、剧情标题和来源文件都不是关系端点，绝不能把它们当作人物、地点、物品或事件实体。
4. 不把抽卡、共鸣、概率、商城、价格、奖励、签到、兑换、版本、获取方式、活动等级、关卡解锁等机制/运营信息视为叙事事实。
5. 角色出现在卡池、奖励、商店或活动公告中，不等于参加该剧情事件，也不等于拥有该物品。
6. 角色正在做某事、坐在某物上、出现在某地点、与某人同场、互相称呼，通常不足以推出长期偏好、所有权、盟友、敌对或稳定关系。
7. 时装、生日、活动、随机事件和邮件往往只描述特定情境。除非原文明确是稳定设定，否则 temporal_scope 必须是 situational 或 costume_specific；对于 ALLY_OF、OPPOSES、HAS_RELATIONSHIP_CONTEXT，只要不能确认稳定关系就选择 abstain。
8. HAS_PREFERENCE 必须出现明确的喜欢、讨厌、偏爱、意愿等表达；OWNS_ITEM 必须明确拥有、携带、收藏、佩戴或个人使用；PARTICIPATES_IN_EVENT 必须明确已经参与实际发生的叙事行动；VISITS_LOCATION 必须明确到达/前往/访问该地点。
9. MENTIONS 即使原文成立，通常也应 recommend_reject，因为它对角色人格和长期关系图谱价值很低。
10. 任何身份、时间线、条件、关系强度或证据范围不明确的情况，一律 abstain，而不是猜测。

仅输出一个 JSON 对象，字段必须为：
{
  "candidate_id": "输入中的 candidate_id",
  "verdict": "recommend_approve | recommend_reject | abstain",
  "evidence_sufficiency": "direct | partial | insufficient",
  "relation_type_valid": true,
  "identity_mapping_confidence": "exact_literal | ambiguous | unmapped",
  "temporal_scope": "stable | situational | costume_specific | unknown",
  "risk_flags": ["简短风险标记"],
  "supporting_quote": "仅在证据直接支持时逐字复制原文；否则为空字符串",
  "verdict_rationale": "不超过160字，说明该结论仅如何由证据得出"
}

当 verdict 为 recommend_approve 时，必须同时满足：evidence_sufficiency=direct、relation_type_valid=true、identity_mapping_confidence=exact_literal，并给出完整且逐字可定位的 supporting_quote。
For recommend_approve, the supporting_quote itself must literally contain both the candidate subject and the candidate object (ignoring whitespace only). Do not rely on pronouns, aliases, titles, page metadata, or surrounding context to supply either endpoint; otherwise choose abstain.
For ALLY_OF, OPPOSES, and HAS_RELATIONSHIP_CONTEXT, choose abstain when the evidence is a special mail, random event, costume, birthday, event page, or explicitly costume-bound content. These sources can be shown to a human reviewer but cannot by themselves establish a stable base-character relationship.
A quote that merely places the two endpoints together or has one character call the other by name is not a relation assertion. A recommend_approve quote must itself include the relation predicate: e.g. friend/partner/superior/trust/love for HAS_RELATIONSHIP_CONTEXT; ally or enemy/opposition for ALLY_OF or OPPOSES; a direct possession phrase for OWNS_ITEM; a travel/location phrase for VISITS_LOCATION; and an explicit participation phrase for PARTICIPATES_IN_EVENT. Otherwise choose abstain."""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _optional_bool_env(name: str) -> bool | None:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    return None


def review_provider_settings() -> tuple[str, str, str, str]:
    """Read only the independent review provider settings from the local environment."""
    load_relation_environment()
    return (
        os.getenv("RELATION_REVIEW_PROVIDER", "disabled"),
        os.getenv("RELATION_REVIEW_BASE_URL", ""),
        os.getenv("RELATION_REVIEW_API_KEY", ""),
        os.getenv("RELATION_REVIEW_MODEL", ""),
    )


def _retry_settings() -> tuple[int, float]:
    return (
        _bounded_int_env("RELATION_REVIEW_MAX_ATTEMPTS", 3, 1, 5),
        _bounded_float_env("RELATION_REVIEW_RETRY_BACKOFF_SECONDS", 2.0, 0.0, 30.0),
    )


def _provider_timeout_seconds(prompt: str) -> float:
    short_timeout = _bounded_float_env("RELATION_REVIEW_TIMEOUT_SECONDS", 120.0, 30.0, 600.0)
    long_timeout = _bounded_float_env(
        "RELATION_REVIEW_LONG_PROMPT_TIMEOUT_SECONDS", 180.0, short_timeout, 600.0
    )
    long_prompt_chars = _bounded_int_env("RELATION_REVIEW_LONG_PROMPT_CHARS", 8_000, 1_000, 100_000)
    return long_timeout if len(prompt) >= long_prompt_chars else short_timeout


def _call_reviewer(base_url: str, api_key: str, model: str, prompt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call a compatible JSON endpoint without exposing the credential in artifacts."""
    url = base_url.rstrip("/") + "/chat/completions"
    request_body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    if _optional_bool_env("RELATION_REVIEW_JSON_MODE") is not False:
        request_body["response_format"] = {"type": "json_object"}
    enable_thinking = _optional_bool_env("RELATION_REVIEW_REQUEST_ENABLE_THINKING")
    if enable_thinking is not None:
        request_body["enable_thinking"] = enable_thinking

    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=request_body,
        timeout=_provider_timeout_seconds(prompt),
    )
    if response.is_error:
        preview = response.text.replace("\n", " ")[:500]
        raise RuntimeError(f"Provider HTTP {response.status_code}; response_preview={preview!r}")
    try:
        payload = response.json()
        choice = payload["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        preview = response.text.replace("\n", " ")[:500]
        raise ValueError(f"Provider returned an invalid OpenAI-compatible response; response_preview={preview!r}") from exc
    message = choice.get("message") or {}
    content = _message_text(message.get("content"))
    reasoning_content = _message_text(message.get("reasoning_content"))
    return _parse_relation_payload(content, choice.get("finish_reason"), reasoning_content), payload.get("usage") or {}


def _call_reviewer_with_retry(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    provider_call: Callable[[str, str, str, str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    """Retry only transient compatible-provider failures with bounded backoff."""
    max_attempts, base_backoff = _retry_settings()
    provider_call = provider_call or _call_reviewer
    for attempt in range(1, max_attempts + 1):
        try:
            response, usage = provider_call(base_url, api_key, model, prompt)
            return response, usage, {"attempts": attempt, "retries": attempt - 1}
        except Exception as error:
            retriable = _is_retriable_provider_error(error)
            if not retriable or attempt == max_attempts:
                raise ProviderCallFailure(
                    f"Independent review provider call failed after {attempt} attempt(s): {error}", attempt, retriable
                ) from error
            sleep(base_backoff * (2 ** (attempt - 1)))
    raise AssertionError("Retry loop exited unexpectedly.")


def _compact(value: Any) -> str:
    return _compact_text(value)


def _quote_in_documents(quote: str, documents: Iterable[dict[str, Any]]) -> bool:
    compact_quote = _compact(quote)
    return bool(compact_quote) and any(compact_quote in _compact(document.get("text")) for document in documents)


def _literal_endpoint_validation_flags(candidate: dict[str, Any], quote: str) -> list[str]:
    """Require an approval quote to state both proposed relation endpoints.

    This deliberately favors precision over recall.  A page-level subject,
    a pronoun, or an inferred alias is not enough for an automatically
    audit-eligible recommendation: the quoted text must state the literal
    candidate endpoint itself.  Human reviewers can still inspect all
    abstentions and decide otherwise with fuller context.
    """
    compact_quote = _compact(quote)
    flags: list[str] = []
    subject = _compact(candidate.get("subject"))
    object_ = _compact(candidate.get("object"))
    if not subject or subject not in compact_quote:
        flags.append("approve_subject_not_in_quote")
    if not object_ or object_ not in compact_quote:
        flags.append("approve_object_not_in_quote")
    return flags


def _requires_human_review_for_context_relation(
    candidate: dict[str, Any], evidence: Iterable[dict[str, Any]]
) -> bool:
    """Keep scene-bound relationship claims out of the auto-eligible pool.

    A special mail, costume, birthday, random event, or event page can contain
    meaningful dialogue, but is not by itself sufficient to assert a stable
    character relationship.  Such material remains visible to human reviewers
    and can still inform retrieval with explicit scene context.
    """
    relation_type = str(candidate.get("relation_type") or "").strip().upper()
    if relation_type not in _CONTEXT_SENSITIVE_RELATIONS:
        return False
    if str(candidate.get("source_type") or "").strip() in _CONTEXT_SENSITIVE_SOURCE_TYPES:
        return True
    return any(
        str(document.get("source_type") or "").strip() in _CONTEXT_SENSITIVE_SOURCE_TYPES
        or bool((document.get("metadata") or {}).get("requires_costume_context"))
        for document in evidence
    )


def _quote_has_relation_predicate(candidate: dict[str, Any], quote: str) -> bool:
    """Check that the quote expresses the proposed edge, not only its endpoints.

    The model can use the surrounding evidence to understand the passage, but
    an audit-eligible recommendation needs one self-contained, literal quote
    that communicates the relation itself.  The vocabulary below is intentionally
    narrow: missed valid claims become human-reviewable abstentions, whereas
    name-only co-occurrences never become false graph facts.
    """
    relation_type = str(candidate.get("relation_type") or "").strip().upper()
    compact_quote = _compact(quote)
    subject = _compact(candidate.get("subject"))
    object_ = _compact(candidate.get("object"))

    if relation_type == "HAS_RELATIONSHIP_CONTEXT":
        return any(
            cue in compact_quote
            for cue in (
                "友人", "朋友", "挚友", "恋人", "爱人", "伙伴", "同伴", "搭档", "队友", "战友",
                "上司", "下属", "部下", "同事", "家人", "姐妹", "兄弟", "师徒", "老师", "学生",
                "喜欢", "喜爱", "爱慕", "相爱", "爱你", "信任", "依赖", "亲近", "关心", "支持",
                "在乎", "安心", "friend", "partner", "lover", "superior", "trust", "love",
            )
        )
    if relation_type == "ALLY_OF":
        return any(
            cue in compact_quote
            for cue in ("盟友", "同盟", "结盟", "伙伴", "同伴", "队友", "战友", "ally", "allies")
        )
    if relation_type == "OPPOSES":
        return any(
            cue in compact_quote
            for cue in ("敌人", "敌对", "对手", "对抗", "反对", "攻击", "交战", "阻止", "追杀", "enemy", "oppose")
        )
    if relation_type == "HAS_PREFERENCE":
        return any(
            cue in compact_quote
            for cue in ("喜欢", "喜爱", "偏好", "偏爱", "钟爱", "热爱", "最爱", "爱吃", "想要", "想吃", "想去", "希望", "愿意", "宁愿", "讨厌", "不喜欢", "厌恶", "like", "prefer", "love", "hate")
        )
    if relation_type == "OWNS_ITEM":
        possession_patterns = (
            f"{subject}的{object_}",
            f"她的{object_}",
            f"他的{object_}",
            f"我的{object_}",
            f"{object_}是{subject}的",
        )
        return any(pattern in compact_quote for pattern in possession_patterns) or any(
            cue in compact_quote
            for cue in ("拥有", "持有", "携带", "随身", "佩戴", "收藏", "自用", "own", "belongs")
        )
    if relation_type == "VISITS_LOCATION":
        location_patterns = (
            f"来到{object_}", f"到达{object_}", f"前往{object_}", f"进入{object_}",
            f"位于{object_}", f"身处{object_}", f"在{object_}", f"{object_}中",
            f"{object_}内", f"{object_}里", f"{object_}附近", f"visit{object_}",
        )
        return any(pattern in compact_quote for pattern in location_patterns)
    if relation_type == "PARTICIPATES_IN_EVENT":
        event_patterns = (
            f"在{object_}中", f"{object_}中", f"参与{object_}", f"参加{object_}",
            f"加入{object_}", f"执行{object_}", f"进行{object_}", f"完成{object_}",
            f"participatein{object_}",
        )
        return any(pattern in compact_quote for pattern in event_patterns)
    # An unknown relation type should never become automatically eligible.
    return False


def _excerpt_around_quote(text: str, quote: str, maximum: int) -> tuple[str, bool]:
    """Preserve the direct quote and local context when a source chunk is long."""
    if len(text) <= maximum:
        return text, False
    position = text.find(quote)
    if position < 0:
        return text[:maximum], True
    before = max(0, position - maximum // 3)
    after = min(len(text), before + maximum)
    before = max(0, after - maximum)
    prefix = "…" if before else ""
    suffix = "…" if after < len(text) else ""
    return prefix + text[before:after] + suffix, True


def _safe_scope_metadata(document: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata") or {}
    selected = {
        key: metadata[key]
        for key in (
            "character_name",
            "armor_name",
            "costume_name",
            "requires_costume_context",
            "source_tier",
        )
        if key in metadata and metadata[key] not in (None, "", [])
    }
    if document.get("source_type") == "character_costume":
        selected["requires_costume_context"] = True
    return selected


def _build_review_input(candidate: dict[str, Any], documents: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str]:
    evidence_ids = [identifier for identifier in candidate.get("evidence_document_ids", []) if identifier in documents]
    evidence_documents = [documents[identifier] for identifier in evidence_ids]
    maximum = _bounded_int_env("RELATION_REVIEW_MAX_EVIDENCE_CHARS", 9_000, 1_000, 30_000)
    quote = str(candidate.get("evidence_quote") or "").strip()
    evidence: list[dict[str, Any]] = []
    remaining = maximum
    evidence_truncated = False
    for index, document in enumerate(evidence_documents):
        remaining_documents = len(evidence_documents) - index
        allowance = min(4_500, max(800, remaining // max(1, remaining_documents)))
        excerpt, truncated = _excerpt_around_quote(str(document.get("text") or ""), quote, allowance)
        remaining = max(0, remaining - len(excerpt))
        evidence_truncated = evidence_truncated or truncated
        evidence.append(
            {
                "document_id": document.get("document_id"),
                "page_id": document.get("page_id"),
                "title": document.get("title"),
                "source_type": document.get("source_type"),
                "scope_metadata": _safe_scope_metadata(document),
                "text": excerpt,
            }
        )
    relation_type = str(candidate.get("relation_type") or "").strip().upper()
    payload = {
        "review_policy_version": REVIEW_POLICY_VERSION,
        "candidate": {
            "candidate_id": candidate.get("candidate_id"),
            "subject": candidate.get("subject"),
            "relation_type": relation_type,
            "object": candidate.get("object"),
            "source_type": candidate.get("source_type"),
            "direct_quote_from_first_stage": quote,
        },
        "endpoint_constraint": _RELATION_TARGET_CONSTRAINTS.get(
            relation_type, "关系类型不在当前受控词表中；除非证据完全明确，否则选择 abstain。"
        ),
        "evidence_document_ids": evidence_ids,
        "missing_evidence_document_ids": [
            identifier for identifier in candidate.get("evidence_document_ids", []) if identifier not in documents
        ],
        "evidence_truncated": evidence_truncated,
        "evidence": evidence,
    }
    input_hash = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return payload, input_hash


def build_review_prompt(candidate: dict[str, Any], documents: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Return a provider prompt and stable hash of exactly what was reviewed."""
    payload, input_hash = _build_review_input(candidate, documents)
    return json.dumps(payload, ensure_ascii=False), input_hash


def _preflight_rejection_reason(
    candidate: dict[str, Any], input_payload: dict[str, Any], documents: dict[str, dict[str, Any]]
) -> str | None:
    if not str(candidate.get("candidate_id") or "").strip():
        return "missing_candidate_id"
    if not str(candidate.get("subject") or "").strip() or not str(candidate.get("object") or "").strip():
        return "missing_relation_entity"
    if str(candidate.get("relation_type") or "").strip().upper() == "MENTIONS":
        return "low_value_mention"
    evidence_ids = input_payload["evidence_document_ids"]
    if not evidence_ids:
        return "no_available_evidence"
    if input_payload["missing_evidence_document_ids"]:
        return "some_evidence_documents_missing"
    quote = str(candidate.get("evidence_quote") or "").strip()
    if len(_compact(quote)) < 8:
        return "evidence_quote_too_short"
    if not _quote_in_documents(quote, (documents[identifier] for identifier in evidence_ids)):
        return "evidence_quote_not_found"
    return None


def _clean_string_list(value: Any, maximum_items: int = 12, maximum_length: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        cleaned = str(item).strip().replace("\n", " ")[:maximum_length]
        if cleaned and cleaned not in output:
            output.append(cleaned)
        if len(output) >= maximum_items:
            break
    return output


def validate_review_response(
    response: dict[str, Any], candidate: dict[str, Any], documents: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Validate a model recommendation locally; invalid approvals become abstentions."""
    if not isinstance(response, dict):
        raise ValueError("Provider returned a review response that is not an object.")
    candidate_id = str(candidate.get("candidate_id") or "")
    if str(response.get("candidate_id") or "") != candidate_id:
        raise ValueError("Provider review candidate_id does not match the requested candidate.")

    model_verdict = str(response.get("verdict") or "").strip()
    if model_verdict not in VERDICTS:
        raise ValueError("Provider review verdict is outside the controlled vocabulary.")
    evidence_sufficiency = str(response.get("evidence_sufficiency") or "").strip()
    if evidence_sufficiency not in EVIDENCE_SUFFICIENCY:
        raise ValueError("Provider review evidence_sufficiency is outside the controlled vocabulary.")
    identity_confidence = str(response.get("identity_mapping_confidence") or "").strip()
    if identity_confidence not in IDENTITY_CONFIDENCE:
        raise ValueError("Provider review identity_mapping_confidence is outside the controlled vocabulary.")
    temporal_scope = str(response.get("temporal_scope") or "").strip()
    if temporal_scope not in TEMPORAL_SCOPES:
        raise ValueError("Provider review temporal_scope is outside the controlled vocabulary.")
    relation_type_valid = response.get("relation_type_valid")
    if not isinstance(relation_type_valid, bool):
        raise ValueError("Provider review relation_type_valid must be boolean.")

    supporting_quote = str(response.get("supporting_quote") or "").strip()
    rationale = str(response.get("verdict_rationale") or "").strip().replace("\n", " ")[:1_000]
    risk_flags = _clean_string_list(response.get("risk_flags"))
    validation_flags: list[str] = []
    verdict = model_verdict
    evidence = [
        documents[identifier]
        for identifier in candidate.get("evidence_document_ids", [])
        if identifier in documents
    ]

    if verdict == "recommend_approve":
        if evidence_sufficiency != "direct":
            validation_flags.append("approve_without_direct_evidence")
        if not relation_type_valid:
            validation_flags.append("approve_with_invalid_relation_type")
        if identity_confidence != "exact_literal":
            validation_flags.append("approve_with_ambiguous_identity")
        if len(_compact(supporting_quote)) < 8 or not _quote_in_documents(supporting_quote, evidence):
            validation_flags.append("approve_quote_not_found")
        validation_flags.extend(_literal_endpoint_validation_flags(candidate, supporting_quote))
        if not _quote_has_relation_predicate(candidate, supporting_quote):
            validation_flags.append("approve_relation_predicate_not_in_quote")
        relation_type = str(candidate.get("relation_type") or "").strip().upper()
        if relation_type in _CONTEXT_SENSITIVE_RELATIONS and temporal_scope != "stable":
            validation_flags.append("context_sensitive_relation_not_stable")
        if _requires_human_review_for_context_relation(candidate, evidence):
            validation_flags.append("context_sensitive_source_requires_human_review")
        if validation_flags:
            verdict = "abstain"

    audit_eligible = (
        verdict == "recommend_approve"
        and evidence_sufficiency == "direct"
        and relation_type_valid
        and identity_confidence == "exact_literal"
        and not validation_flags
    )
    return {
        "verdict": verdict,
        "model_verdict": model_verdict,
        "evidence_sufficiency": evidence_sufficiency,
        "relation_type_valid": relation_type_valid,
        "identity_mapping_confidence": identity_confidence,
        "temporal_scope": temporal_scope,
        "risk_flags": risk_flags,
        "validation_flags": validation_flags,
        "supporting_quote": supporting_quote,
        "verdict_rationale": rationale,
        "audit_eligible": audit_eligible,
    }


def _report_key(candidate_id: str, input_hash: str, provider: str, model: str) -> str:
    return "\x1f".join((candidate_id, input_hash, REVIEW_POLICY_VERSION, provider, model))


def _report_base(
    candidate: dict[str, Any],
    input_hash: str,
    provider: str,
    model: str,
    run_name: str,
) -> dict[str, Any]:
    candidate_id = str(candidate.get("candidate_id") or "")
    return {
        "report_id": stable_id(candidate_id, input_hash, REVIEW_POLICY_VERSION, provider, model, prefix="relation_model_review_"),
        "report_key": _report_key(candidate_id, input_hash, provider, model),
        "candidate_id": candidate_id,
        "review_group_id": _review_group_id(candidate),
        "source_type": candidate.get("source_type"),
        "relation_type": candidate.get("relation_type"),
        "evidence_document_ids": candidate.get("evidence_document_ids", []),
        "input_hash": input_hash,
        "review_policy_version": REVIEW_POLICY_VERSION,
        "model_reviewer": {"provider": provider, "model": model, "run_name": run_name},
        "reviewed_at": utc_now(),
        "policy": "This is a non-binding machine recommendation. It never changes candidate status and never writes a graph edge.",
    }


def _local_policy_report(
    candidate: dict[str, Any], input_hash: str, reason: str, run_name: str
) -> dict[str, Any]:
    return {
        **_report_base(candidate, input_hash, "deterministic-policy", "local-rules", run_name),
        "review_status": "local_policy",
        "verdict": "recommend_reject",
        "model_verdict": "recommend_reject",
        "evidence_sufficiency": "insufficient" if "evidence" in reason else "partial",
        "relation_type_valid": reason != "low_value_mention",
        "identity_mapping_confidence": "unmapped",
        "temporal_scope": "unknown",
        "risk_flags": [reason],
        "validation_flags": [],
        "supporting_quote": "",
        "verdict_rationale": "Deterministic preflight excluded this candidate before a provider call.",
        "audit_eligible": False,
    }


def _failure_report(
    candidate: dict[str, Any],
    input_hash: str,
    provider: str,
    model: str,
    run_name: str,
    error: Exception,
    attempts: int,
) -> dict[str, Any]:
    return {
        **_report_base(candidate, input_hash, provider, model, run_name),
        "review_status": "failed",
        "verdict": "abstain",
        "model_verdict": "abstain",
        "evidence_sufficiency": "insufficient",
        "relation_type_valid": False,
        "identity_mapping_confidence": "unmapped",
        "temporal_scope": "unknown",
        "risk_flags": ["provider_or_schema_failure"],
        "validation_flags": [],
        "supporting_quote": "",
        "verdict_rationale": "No machine recommendation was accepted because the independent review call failed.",
        "audit_eligible": False,
        "failure_count": 1,
        "last_error": str(error)[:800],
        "attempts": attempts,
    }


def _review_one(
    candidate: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    input_payload: dict[str, Any],
    prompt: str,
    input_hash: str,
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    run_name: str,
) -> dict[str, Any]:
    try:
        response, usage, call_metadata = _call_reviewer_with_retry(base_url, api_key, model, prompt)
        validated = validate_review_response(response, candidate, documents)
        if input_payload.get("evidence_truncated") and validated["verdict"] == "recommend_approve":
            # A quote may be direct while an omitted portion reverses its
            # temporal or conditional scope. Keep these reports visible, but
            # require a human rather than treating them as audit-eligible.
            validated["verdict"] = "abstain"
            validated["audit_eligible"] = False
            validated["validation_flags"].append("evidence_excerpted_for_review")
        return {
            **_report_base(candidate, input_hash, provider, model, run_name),
            "review_status": "completed",
            **validated,
            "input_evidence_truncated": bool(input_payload.get("evidence_truncated")),
            "attempts": call_metadata["attempts"],
            "retry_count": call_metadata["retries"],
            "usage": usage,
        }
    except ProviderCallFailure as error:
        return _failure_report(candidate, input_hash, provider, model, run_name, error, error.attempts)
    except Exception as error:
        return _failure_report(candidate, input_hash, provider, model, run_name, error, 1)


def _latest_completed_keys(reports: Iterable[dict[str, Any]]) -> set[str]:
    return {
        str(report.get("report_key"))
        for report in reports
        if report.get("review_status") in {"completed", "local_policy"} and report.get("report_key")
    }


def _add_usage(total: Counter[str], usage: Any) -> None:
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, int):
            total[key] += value


def _validate_run_name(value: str) -> str:
    name = value.strip() or "secondary-review"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name):
        raise ValueError("run_name may contain only letters, numbers, dot, underscore, and hyphen.")
    return name


def review(
    limit: int | None = None,
    candidate_ids: set[str] | None = None,
    force: bool = False,
    workers: int = 1,
    run_name: str = "secondary-review",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create resumable machine-review reports without mutating review candidates."""
    run_name = _validate_run_name(run_name)
    workers = max(1, min(int(workers), 8))
    provider, base_url, api_key, model = review_provider_settings()
    candidates_path = RUNTIME_ROOT / "review" / "narrative_relation_candidates.jsonl"
    reports_path = RUNTIME_ROOT / "review" / REPORT_FILENAME
    candidates = [
        candidate
        for candidate in _read_jsonl(candidates_path)
        if candidate.get("review_status") == "pending_review"
        and (candidate_ids is None or str(candidate.get("candidate_id")) in candidate_ids)
    ]
    documents = {document["document_id"]: document for document in load_runtime_jsonl("documents.jsonl")}
    reports = _read_jsonl(reports_path)
    existing_keys = _latest_completed_keys(reports)

    planned: list[tuple[dict[str, Any], dict[str, Any], str, str | None]] = []
    skipped = 0
    for candidate in candidates:
        input_payload, input_hash = _build_review_input(candidate, documents)
        preflight_reason = _preflight_rejection_reason(candidate, input_payload, documents)
        effective_provider = "deterministic-policy" if preflight_reason else provider
        effective_model = "local-rules" if preflight_reason else model
        key = _report_key(str(candidate.get("candidate_id") or ""), input_hash, effective_provider, effective_model)
        if not force and key in existing_keys:
            skipped += 1
            continue
        prompt = json.dumps(input_payload, ensure_ascii=False)
        planned.append((candidate, input_payload, input_hash, preflight_reason))

    if limit is not None:
        planned = planned[: max(0, limit)]

    configuration_ready = provider == "openai-compatible" and bool(base_url and api_key and model)
    if not dry_run and any(reason is None for _, _, _, reason in planned) and not configuration_ready:
        if provider != "openai-compatible":
            report = {
                "stage": "C",
                "job": "review_relation_candidates",
                "generated_at": utc_now(),
                "provider": provider,
                "pending_candidates": len(candidates),
                "planned": len(planned),
                "status": "skipped_provider_disabled",
                "policy": "No secondary review report is generated until its independent provider is explicitly configured.",
            }
            write_json(RUNTIME_ROOT / "reports" / REPORT_SUMMARY_FILENAME, report)
            return report
        raise RuntimeError(
            "OpenAI-compatible independent relation review requires RELATION_REVIEW_BASE_URL, "
            "RELATION_REVIEW_API_KEY, and RELATION_REVIEW_MODEL."
        )

    local_work = [item for item in planned if item[3] is not None]
    provider_work = [item for item in planned if item[3] is None]
    if dry_run:
        return {
            "stage": "C",
            "job": "review_relation_candidates",
            "generated_at": utc_now(),
            "provider": provider,
            "model": model,
            "pending_candidates": len(candidates),
            "already_current": skipped,
            "planned": len(planned),
            "deterministic_policy_only": len(local_work),
            "provider_calls_required": len(provider_work),
            "workers": workers,
            "policy": "Dry run only. No provider call, candidate mutation, graph write, or review report write occurred.",
        }

    processed = 0
    failed = 0
    retry_count = 0
    usage_total: Counter[str] = Counter()
    verdict_counts: Counter[str] = Counter()
    failure_rows: list[dict[str, Any]] = []

    def checkpoint(result: dict[str, Any]) -> None:
        nonlocal processed, failed, retry_count
        reports.append(result)
        write_jsonl(reports_path, reports)
        processed += 1
        verdict_counts[str(result.get("verdict") or "unknown")] += 1
        retry_count += int(result.get("retry_count") or 0)
        _add_usage(usage_total, result.get("usage"))
        if result.get("review_status") == "failed":
            failed += 1
            failure_rows.append(
                {
                    "candidate_id": result.get("candidate_id"),
                    "source_type": result.get("source_type"),
                    "error": result.get("last_error", "Independent review failed."),
                }
            )

    for candidate, _, input_hash, reason in local_work:
        checkpoint(_local_policy_report(candidate, input_hash, str(reason), run_name))

    def call_work(item: tuple[dict[str, Any], dict[str, Any], str, str | None]) -> dict[str, Any]:
        candidate, input_payload, input_hash, _ = item
        return _review_one(
            candidate,
            documents,
            input_payload,
            json.dumps(input_payload, ensure_ascii=False),
            input_hash,
            provider,
            base_url,
            api_key,
            model,
            run_name,
        )

    if workers == 1:
        for item in provider_work:
            checkpoint(call_work(item))
    elif provider_work:
        # Bounded concurrency is intentionally modest. The compatible endpoint
        # still receives ordinary requests, not a provider-specific Batch API.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="relation-review") as executor:
            for result in executor.map(call_work, provider_work):
                checkpoint(result)

    report = {
        "stage": "C",
        "job": "review_relation_candidates",
        "generated_at": utc_now(),
        "provider": provider,
        "model": model,
        "run_name": run_name,
        "pending_candidates": len(candidates),
        "already_current": skipped,
        "attempted": len(planned),
        "processed": processed,
        "failed": failed,
        "failed_candidates": failure_rows,
        "retry_count": retry_count,
        "verdict_counts": dict(sorted(verdict_counts.items())),
        "usage_tokens": dict(sorted(usage_total.items())),
        "output": str(reports_path),
        "policy": "Machine reports remain advisory. They do not change candidate status, map graph nodes, or write graph edges.",
    }
    write_json(RUNTIME_ROOT / "reports" / REPORT_SUMMARY_FILENAME, report)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an independent evidence review over pending relation candidates.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum uncached candidates to process in this invocation.")
    parser.add_argument(
        "--candidate-id", action="append", default=[], help="Review one candidate ID; repeat the flag for multiple IDs."
    )
    parser.add_argument("--force", action="store_true", help="Re-review even when a current report for this provider/model exists.")
    parser.add_argument("--workers", type=int, default=1, help="Bounded concurrent compatible-provider calls (1-8).")
    parser.add_argument("--run-name", default="secondary-review", help="Audit label for this invocation.")
    parser.add_argument("--dry-run", action="store_true", help="Show the planned work without calling a provider or writing reports.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    print(
        json.dumps(
            review(
                limit=args.limit,
                candidate_ids=set(args.candidate_id) or None,
                force=args.force,
                workers=args.workers,
                run_name=args.run_name,
                dry_run=args.dry_run,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
