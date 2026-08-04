"""Optional AI-assisted narrative relation extraction.

The result is a review queue only. This module never writes to graph/edges.jsonl.
It uses an OpenAI-compatible endpoint only when explicitly configured for private
development validation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, MutableMapping

import httpx

from .common import RUNTIME_ROOT, load_runtime_jsonl, stable_id, utc_now, write_json, write_jsonl


SYSTEM_PROMPT = """你是叙事知识标注助手。只根据给出的证据提取明确表达的关系，不推测。
返回 JSON 对象，形如 {\"relations\": [{\"subject\": \"...\", \"relation_type\": \"...\", \"object\": \"...\", \"confidence\": 0.0-1.0, \"evidence_document_ids\": [\"doc_...\"], \"rationale\": \"...\"}]}。
relation_type 必须来自允许列表。若没有明确关系，返回空数组。不要输出任何额外文字。"""

EXTRACTION_POLICY = """
你只提取可长期进入叙事知识图谱的事实，不提取游戏机制、公告、商城或运营信息。

每条关系必须额外返回 evidence_quote：从证据原文逐字摘取的一小段直接引文。引文必须足以支持该关系，不能改写、拼接、概括或截断；去除空白和引号后至少 8 个字符。

严格排除以下内容及其衍生关系：抽卡或角色/武器共鸣、概率提升、补给、供应站、限时上架、商城价格、签到、兑换、奖励、通行证/凭证、活动等级、版本、获取方式、数值和关卡解锁条件。角色出现在卡池、奖励或商店中，不代表角色参与活动，也不代表其拥有该物品。

关系类型的边界：
- HAS_PREFERENCE 的客体必须是角色明确喜欢、厌恶或认同的事物、活动、食物或价值观；客体不能是分析员或任何角色。对某角色的欣赏、信任、依恋、好感或亲密互动使用 HAS_RELATIONSHIP_CONTEXT。
- HAS_PREFERENCE 的引文必须直接包含明确的喜好、意愿或厌恶表达。仅仅从角色正在做某事、身处安静环境或提到某个地点，不可推断偏好。
- OWNS_ITEM 仅用于原文明确表明角色个人持有、佩戴、携带、收藏或自用的物品；不可因为角色坐在、使用或操作某物而推断所有权，也不可根据发放、可购买、可获取或概率提升推断。
- PARTICIPATES_IN_EVENT 仅用于角色实际参与的剧情事件、行动、调查、探索、战斗或任务；不可用于卡池、签到、挑战关卡、商店或奖励活动。
- MENTIONS 只在名称被角色或叙述直接提及时使用，不要把它当作所在地、所属关系或偏好的替代。
- 对 MENTIONS、VISITS_LOCATION 和 OWNS_ITEM，客体名称必须完整地直接出现在 evidence_quote 中；不要用“该区域”“那张”“她的房间”等代词补全成其他实体。
- 不要输出 KNOWS。角色在对话中互相称呼、同场出现或互相认识，通常不构成值得审核的叙事关系。

证据不足或存在歧义时，宁可返回空数组。"""

EXTRACTION_POLICY += """
补充边界：PARTICIPATES_IN_EVENT 只接受已发生的事实，反问、假设、猜测、计划或“会不会/是否/如果”式表述不能作为参与证据。HAS_RELATIONSHIP_CONTEXT 的客体必须是分析员或明确命名的角色/实体，不能是“郎君”“亲爱的”“老师”“你”等称谓或代词；若称谓在页面上下文中明确指向分析员，请直接使用“分析员”，否则不输出关系。"""

MECHANICS_TERMS = (
    "抽卡",
    "共鸣",
    "概率提升",
    "获取概率",
    "补给",
    "供应站",
    "限时上架",
    "上架销售",
    "商城",
    "售价",
    "价格",
    "签到",
    "兑换",
    "奖励",
    "凭证",
    "通行证",
    "活动等级",
    "版本",
    "获取方式",
    "解锁条件",
    "挑战关卡",
    "试用角色",
)
NARRATIVE_EVENT_TERMS = ("行动", "任务", "探索", "调查", "执行", "战斗", "对抗", "营救", "前往", "抵达", "清理")
ANALYST_NAMES = {"分析员", "分析员大人"}
PREFERENCE_TERMS = (
    "喜欢",
    "喜爱",
    "偏好",
    "偏爱",
    "钟爱",
    "热爱",
    "最爱",
    "爱吃",
    "想要",
    "想吃",
    "想去",
    "希望",
    "愿意",
    "宁愿",
    "好吃",
    "讨厌",
    "不喜欢",
    "厌恶",
)
OWNERSHIP_TERMS = ("我的", "她的", "他的", "手上", "手中", "手里", "储物包", "背包", "随身", "携带", "佩戴", "持有", "拥有", "收藏", "自用", "拿着", "握着", "取出", "掏出")
DIRECT_QUOTE_RELATIONS = {"MENTIONS", "VISITS_LOCATION", "OWNS_ITEM"}
INCOMPLETE_QUOTE_ENDINGS = ("的", "和", "与", "在", "将", "、", "，", ",", "：", ":")
HYPOTHETICAL_EVENT_MARKERS = ("？", "?", "会不会", "是否", "如果", "假如", "难道", "不会是", "指的是")
GENERIC_RELATIONSHIP_TARGETS = {"郎君", "亲爱的", "爱人", "前辈", "队长", "老师", "你", "您", "他", "她", "他们", "她们", "我", "我们", "大家", "所有人", "其他人"}
RELATION_ENVIRONMENT_KEYS = {
    "RELATION_CANDIDATE_PROVIDER",
    "RELATION_CANDIDATE_DISABLE_THINKING",
    "DASHSCOPE_BASE_URL",
    "DASHSCOPE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_MODEL",
    "RELATION_CANDIDATE_MAX_ATTEMPTS",
    "RELATION_CANDIDATE_RETRY_BACKOFF_SECONDS",
    "RELATION_CANDIDATE_TIMEOUT_SECONDS",
    "RELATION_CANDIDATE_LONG_PROMPT_TIMEOUT_SECONDS",
    "RELATION_CANDIDATE_LONG_PROMPT_CHARS",
    # The independent second-review pipeline deliberately uses a separate
    # credential set. Keeping it here means the same safe local .env loader
    # can load the values without ever reading or exposing unrelated secrets.
    "RELATION_REVIEW_PROVIDER",
    "RELATION_REVIEW_BASE_URL",
    "RELATION_REVIEW_API_KEY",
    "RELATION_REVIEW_MODEL",
    "RELATION_REVIEW_REQUEST_ENABLE_THINKING",
    "RELATION_REVIEW_JSON_MODE",
    "RELATION_REVIEW_MAX_ATTEMPTS",
    "RELATION_REVIEW_RETRY_BACKOFF_SECONDS",
    "RELATION_REVIEW_TIMEOUT_SECONDS",
    "RELATION_REVIEW_LONG_PROMPT_TIMEOUT_SECONDS",
    "RELATION_REVIEW_LONG_PROMPT_CHARS",
    "RELATION_REVIEW_MAX_EVIDENCE_CHARS",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def load_relation_environment(
    env_path: Path | None = None, environment: MutableMapping[str, str] | None = None
) -> None:
    """Load only private relation-extraction settings from an untracked local .env file."""
    env_path = env_path or Path(__file__).resolve().parents[1] / ".env"
    environment = environment if environment is not None else os.environ
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in RELATION_ENVIRONMENT_KEYS or environment.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        environment[key] = value


def relation_provider_settings() -> tuple[str, str, str, str]:
    """Resolve official DashScope settings first, then generic compatible settings."""
    load_relation_environment()
    provider = os.getenv("RELATION_CANDIDATE_PROVIDER", "disabled")
    base_url = os.getenv("DASHSCOPE_BASE_URL") or os.getenv("OPENAI_COMPATIBLE_BASE_URL", "")
    api_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_COMPATIBLE_API_KEY", "")
    model = os.getenv("OPENAI_COMPATIBLE_MODEL", "")
    return provider, base_url, api_key, model


def _compact_text(value: Any) -> str:
    return re.sub(r"[\s\"'“”‘’]", "", str(value or ""))


def _known_character_names(documents: dict[str, dict[str, Any]]) -> set[str]:
    names = set(ANALYST_NAMES)
    for document in documents.values():
        metadata = document.get("metadata") or {}
        related_names = metadata.get("related_character_names") or []
        if not isinstance(related_names, list):
            related_names = []
        for value in (metadata.get("character_name"), *related_names):
            normalized = _compact_text(value)
            if normalized:
                names.add(normalized)
    return names


def build_relation_prompt(job: dict[str, Any], documents: dict[str, dict[str, Any]]) -> str:
    evidence = [documents[identifier] for identifier in job["evidence_document_ids"] if identifier in documents]
    if not evidence:
        raise ValueError("Relation extraction job has no available evidence documents.")
    return json.dumps(
        {
            "source_type": job["source_type"],
            "allowed_relation_types": job["allowed_relation_types"],
            "character_context": job["character_context"],
            "evidence": [
                {
                    "document_id": item["document_id"],
                    "title": item["title"],
                    "source_type": item["source_type"],
                    "text": item["text"],
                }
                for item in evidence
            ],
        },
        ensure_ascii=False,
    )


def validate_relation_candidate(
    relation: dict[str, Any],
    job: dict[str, Any],
    documents: dict[str, dict[str, Any]],
    known_character_names: set[str],
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a locally verifiable candidate or the reason it is not eligible for review."""
    if not isinstance(relation, dict):
        return None, "malformed_relation"
    relation_type = relation.get("relation_type")
    if relation_type not in job["allowed_relation_types"]:
        return None, "unsupported_relation_type"

    subject = str(relation.get("subject") or "").strip()
    object_ = str(relation.get("object") or "").strip()
    rationale = str(relation.get("rationale") or "").strip()
    evidence_quote = str(relation.get("evidence_quote") or "").strip()
    evidence_ids = [
        identifier
        for identifier in relation.get("evidence_document_ids", [])
        if identifier in job["evidence_document_ids"] and identifier in documents
    ]
    if not subject or not object_ or not rationale:
        return None, "missing_required_fields"
    if not evidence_ids:
        return None, "missing_valid_evidence_ids"
    compact_quote = _compact_text(evidence_quote)
    if len(compact_quote) < 8:
        return None, "evidence_quote_too_short"
    if evidence_quote.rstrip().endswith(INCOMPLETE_QUOTE_ENDINGS):
        return None, "evidence_quote_incomplete"
    if not any(compact_quote in _compact_text(documents[identifier].get("text")) for identifier in evidence_ids):
        return None, "evidence_quote_not_found"

    semantic_text = " ".join((object_, rationale, evidence_quote))
    if any(term in semantic_text for term in MECHANICS_TERMS):
        return None, "mechanics_or_operations_content"
    if relation_type == "KNOWS":
        return None, "low_value_relation_type"
    if relation_type == "HAS_PREFERENCE" and _compact_text(object_) in known_character_names:
        return None, "preference_target_is_character"
    if relation_type == "HAS_PREFERENCE" and not any(term in evidence_quote for term in PREFERENCE_TERMS):
        return None, "preference_not_explicit"
    if relation_type == "OWNS_ITEM" and not any(term in evidence_quote for term in OWNERSHIP_TERMS):
        return None, "ownership_not_explicit"
    if relation_type in DIRECT_QUOTE_RELATIONS and _compact_text(object_) not in compact_quote:
        return None, "object_not_in_evidence_quote"
    if relation_type == "PARTICIPATES_IN_EVENT" and any(marker in evidence_quote for marker in HYPOTHETICAL_EVENT_MARKERS):
        return None, "event_not_asserted_as_fact"
    if relation_type == "HAS_RELATIONSHIP_CONTEXT" and _compact_text(object_) in GENERIC_RELATIONSHIP_TARGETS:
        return None, "generic_relationship_target"
    if job["source_type"] == "event_lore" and relation_type == "PARTICIPATES_IN_EVENT":
        if not any(term in semantic_text for term in NARRATIVE_EVENT_TERMS):
            return None, "event_without_narrative_action"

    return (
        {
            "subject": subject,
            "relation_type": relation_type,
            "object": object_,
            "confidence": relation.get("confidence"),
            "rationale": rationale,
            "evidence_quote": evidence_quote,
            "evidence_document_ids": evidence_ids,
            "review_priority": "low" if relation_type == "MENTIONS" else "normal",
        },
        None,
    )


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in value
        ).strip()
    return ""


def _parse_relation_payload(content: str, finish_reason: Any, reasoning_content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        reasoning_hint = reasoning_content.replace("\n", " ")[:240]
        raise ValueError(
            "Provider returned empty assistant content; expected a JSON object. "
            f"finish_reason={finish_reason!r}, reasoning_preview={reasoning_hint!r}"
        )
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        preview = cleaned.replace("\n", " ")[:240]
        raise ValueError(
            "Provider returned non-JSON assistant content; expected a JSON object. "
            f"finish_reason={finish_reason!r}, content_preview={preview!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("Provider returned JSON that is not an object.")
    return parsed


class ProviderCallFailure(RuntimeError):
    def __init__(self, message: str, attempts: int, retriable: bool) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retriable = retriable


def _retry_settings() -> tuple[int, float]:
    try:
        max_attempts = int(os.getenv("RELATION_CANDIDATE_MAX_ATTEMPTS", "3"))
    except ValueError:
        max_attempts = 3
    try:
        backoff_seconds = float(os.getenv("RELATION_CANDIDATE_RETRY_BACKOFF_SECONDS", "2"))
    except ValueError:
        backoff_seconds = 2.0
    return max(1, min(max_attempts, 5)), max(0.0, min(backoff_seconds, 30.0))


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _provider_timeout_seconds(prompt: str) -> float:
    short_timeout = _bounded_float_env("RELATION_CANDIDATE_TIMEOUT_SECONDS", 90.0, 30.0, 600.0)
    long_timeout = _bounded_float_env("RELATION_CANDIDATE_LONG_PROMPT_TIMEOUT_SECONDS", 180.0, short_timeout, 600.0)
    try:
        long_prompt_chars = int(os.getenv("RELATION_CANDIDATE_LONG_PROMPT_CHARS", "6000"))
    except ValueError:
        long_prompt_chars = 6000
    return long_timeout if len(prompt) >= max(1000, long_prompt_chars) else short_timeout


def _is_retriable_provider_error(error: Exception) -> bool:
    if isinstance(error, httpx.RequestError):
        return True
    message = str(error).lower()
    if (
        "timed out" in message
        or "eof occurred" in message
        or "connection reset" in message
        or "empty assistant content" in message
        or "non-json assistant content" in message
    ):
        return True
    status_match = re.search(r"provider http (\d{3})", message)
    return bool(status_match and int(status_match.group(1)) in {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})


def _call_provider_with_retry(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    provider_call: Callable[[str, str, str, str], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], dict[str, int]]:
    max_attempts, base_backoff = _retry_settings()
    provider_call = provider_call or _call_provider
    for attempt in range(1, max_attempts + 1):
        try:
            response = provider_call(base_url, api_key, model, prompt)
            return response, {"attempts": attempt, "retries": attempt - 1}
        except Exception as error:
            retriable = _is_retriable_provider_error(error)
            if not retriable or attempt == max_attempts:
                raise ProviderCallFailure(
                    f"Provider call failed after {attempt} attempt(s): {error}", attempt, retriable
                ) from error
            sleep(base_backoff * (2 ** (attempt - 1)))
    raise AssertionError("Retry loop exited unexpectedly.")


def _call_provider(
    base_url: str, api_key: str, model: str, prompt: str, include_usage: bool = False
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
    load_relation_environment()
    url = base_url.rstrip("/") + "/chat/completions"
    request_body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT + EXTRACTION_POLICY}, {"role": "user", "content": prompt}],
    }
    if os.getenv("RELATION_CANDIDATE_DISABLE_THINKING", "true").lower() in {"1", "true", "yes"}:
        request_body["enable_thinking"] = False
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
    except ValueError as exc:
        preview = response.text.replace("\n", " ")[:500]
        raise ValueError(f"Provider returned a non-JSON HTTP response; response_preview={preview!r}") from exc
    choice = payload["choices"][0]
    message = choice.get("message") or {}
    content = _message_text(message.get("content"))
    reasoning_content = _message_text(message.get("reasoning_content"))
    parsed = _parse_relation_payload(content, choice.get("finish_reason"), reasoning_content)
    return (parsed, payload.get("usage") or {}) if include_usage else parsed


def extract(
    limit: int | None = None, job_ids: set[str] | None = None, include_failed: bool = True
) -> dict[str, Any]:
    provider, base_url, api_key, model = relation_provider_settings()
    jobs_path = RUNTIME_ROOT / "review" / "narrative_relation_jobs.jsonl"
    candidates_path = RUNTIME_ROOT / "review" / "narrative_relation_candidates.jsonl"
    jobs = _read_jsonl(jobs_path)
    existing = _read_jsonl(candidates_path)
    if provider != "openai-compatible":
        report = {
            "stage": "C",
            "job": "extract_relation_candidates",
            "generated_at": utc_now(),
            "provider": provider,
            "processed": 0,
            "status": "skipped_provider_disabled",
            "policy": "No candidate relation is generated or promoted without explicit provider configuration.",
        }
        write_json(RUNTIME_ROOT / "reports" / "extract_relation_candidates.json", report)
        return report
    if not all((base_url, api_key, model)):
        raise RuntimeError("DashScope/OpenAI-compatible relation extraction requires base URL, API key and model configuration.")
    documents = {document["document_id"]: document for document in load_runtime_jsonl("documents.jsonl")}
    known_character_names = _known_character_names(documents)
    existing_candidate_ids = {str(candidate.get("candidate_id")) for candidate in existing if candidate.get("candidate_id")}
    attempted = 0
    processed = 0
    failed = 0
    retry_count = 0
    failed_jobs: list[dict[str, Any]] = []
    filtered_relation_counts: dict[str, int] = {}
    for job in jobs:
        if job_ids is not None and job.get("job_id") not in job_ids:
            continue
        if not include_failed and job.get("status") != "queued":
            continue
        if job.get("status") in {"completed", "completed_no_relation", "superseded"}:
            continue
        attempted += 1
        job["last_attempt_at"] = utc_now()
        try:
            response, call_metadata = _call_provider_with_retry(base_url, api_key, model, build_relation_prompt(job, documents))
        except ProviderCallFailure as error:
            failed += 1
            retry_count += max(0, error.attempts - 1)
            job["status"] = "failed"
            job["failure_count"] = int(job.get("failure_count", 0)) + 1
            job["last_attempts"] = error.attempts
            job["last_error"] = str(error)
            job["last_failed_at"] = utc_now()
            failed_jobs.append({"job_id": job["job_id"], "source_type": job["source_type"], "error": str(error)})
            write_jsonl(candidates_path, existing)
            write_jsonl(jobs_path, jobs)
            if limit and attempted >= limit:
                break
            continue

        retry_count += call_metadata["retries"]
        for ordinal, relation in enumerate(response.get("relations", [])):
            candidate, rejection_reason = validate_relation_candidate(relation, job, documents, known_character_names)
            if candidate is None:
                reason = rejection_reason or "invalid_candidate"
                filtered_relation_counts[reason] = filtered_relation_counts.get(reason, 0) + 1
                continue
            candidate_id = stable_id(
                job["job_id"],
                candidate["subject"],
                candidate["relation_type"],
                candidate["object"],
                candidate["evidence_quote"],
                prefix="relation_candidate_",
            )
            if candidate_id not in existing_candidate_ids:
                existing.append(
                    {
                        "candidate_id": candidate_id,
                        "job_id": job["job_id"],
                        "page_id": job["page_id"],
                        "source_type": job["source_type"],
                        **candidate,
                        "review_status": "pending_review",
                        "extractor": {"provider": provider, "model": model},
                        "created_at": utc_now(),
                    }
                )
                existing_candidate_ids.add(candidate_id)
        job["status"] = "completed"
        job["completed_at"] = utc_now()
        job["last_attempts"] = call_metadata["attempts"]
        job["retry_count"] = call_metadata["retries"]
        job.pop("last_error", None)
        job.pop("last_failed_at", None)
        processed += 1
        write_jsonl(candidates_path, existing)
        write_jsonl(jobs_path, jobs)
        if limit and attempted >= limit:
            break
    report = {
        "stage": "C",
        "job": "extract_relation_candidates",
        "generated_at": utc_now(),
        "provider": provider,
        "attempted": attempted,
        "processed": processed,
        "failed": failed,
        "failed_jobs": failed_jobs,
        "retry_count": retry_count,
        "pending_candidates": len(existing),
        "filtered_relation_counts": dict(sorted(filtered_relation_counts.items())),
        "policy": "Candidates remain pending_review and cannot be used by retrieval.",
    }
    write_json(RUNTIME_ROOT / "reports" / "extract_relation_candidates.json", report)
    return report


def resolve_no_eligible_relation(job_id: str, reviewer: str, note: str) -> dict[str, Any]:
    """Record a human-reviewed no-relation outcome without fabricating a graph edge."""
    jobs_path = RUNTIME_ROOT / "review" / "narrative_relation_jobs.jsonl"
    resolutions_path = RUNTIME_ROOT / "review" / "relation_job_resolutions.jsonl"
    jobs = _read_jsonl(jobs_path)
    job = next((row for row in jobs if row.get("job_id") == job_id), None)
    if job is None:
        raise KeyError(f"Relation extraction job was not found: {job_id}")
    if job.get("status") not in {"queued", "failed"}:
        raise ValueError("Only queued or failed jobs can be resolved without a relation candidate.")
    resolved_at = utc_now()
    job.update(
        {
            "status": "completed_no_relation",
            "completed_at": resolved_at,
            "resolution": "human_verified_no_eligible_relation",
            "resolved_by": reviewer,
            "resolution_note": note,
        }
    )
    resolutions = _read_jsonl(resolutions_path)
    resolutions = [row for row in resolutions if row.get("job_id") != job_id]
    resolutions.append(
        {
            "job_id": job_id,
            "page_id": job.get("page_id"),
            "source_type": job.get("source_type"),
            "evidence_document_ids": job.get("evidence_document_ids", []),
            "resolution": "human_verified_no_eligible_relation",
            "reviewer": reviewer,
            "note": note,
            "resolved_at": resolved_at,
        }
    )
    write_jsonl(jobs_path, jobs)
    write_jsonl(resolutions_path, resolutions)
    report = {
        "stage": "C",
        "job": "resolve_relation_job_without_candidate",
        "job_id": job_id,
        "resolved_at": resolved_at,
        "policy": "Resolution records no eligible relation and never creates or promotes a graph edge.",
    }
    write_json(RUNTIME_ROOT / "reports" / "resolve_relation_job_without_candidate.json", report)
    return report


def revalidate_existing_candidates() -> dict[str, Any]:
    """Apply the current local policy to pending candidates while preserving an audit trail."""
    candidates_path = RUNTIME_ROOT / "review" / "narrative_relation_candidates.jsonl"
    jobs_path = RUNTIME_ROOT / "review" / "narrative_relation_jobs.jsonl"
    rejected_path = RUNTIME_ROOT / "review" / "policy_rejected_relation_candidates.jsonl"
    candidates = _read_jsonl(candidates_path)
    jobs = {job["job_id"]: job for job in _read_jsonl(jobs_path) if job.get("job_id")}
    documents = {document["document_id"]: document for document in load_runtime_jsonl("documents.jsonl")}
    known_character_names = _known_character_names(documents)
    policy_rejected = _read_jsonl(rejected_path)
    rejected_ids = {str(candidate.get("candidate_id")) for candidate in policy_rejected if candidate.get("candidate_id")}
    retained: list[dict[str, Any]] = []
    rejected = 0
    for candidate in candidates:
        if candidate.get("review_status") != "pending_review":
            retained.append(candidate)
            continue
        job = jobs.get(candidate.get("job_id"))
        validated, reason = (
            validate_relation_candidate(candidate, job, documents, known_character_names)
            if job
            else (None, "source_job_missing")
        )
        if validated is not None:
            retained.append({**candidate, **validated, "policy_revalidated_at": utc_now()})
            continue
        rejected += 1
        candidate_id = str(candidate.get("candidate_id", ""))
        if candidate_id and candidate_id not in rejected_ids:
            policy_rejected.append(
                {
                    **candidate,
                    "policy_status": "rejected",
                    "policy_rejection_reason": reason or "invalid_candidate",
                    "policy_rejected_at": utc_now(),
                }
            )
            rejected_ids.add(candidate_id)
    write_jsonl(candidates_path, retained)
    write_jsonl(rejected_path, policy_rejected)
    report = {
        "stage": "C",
        "job": "revalidate_relation_candidates",
        "generated_at": utc_now(),
        "retained": len(retained),
        "policy_rejected": rejected,
        "policy_rejected_output": str(rejected_path),
        "policy": "Rejected candidates remain in an audit file and never change graph artifacts.",
    }
    write_json(RUNTIME_ROOT / "reports" / "revalidate_relation_candidates.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--revalidate-existing", action="store_true")
    parser.add_argument("--job-id", action="append", default=[], help="Extract only this queued or failed job; repeat for several jobs.")
    parser.add_argument("--queued-only", action="store_true", help="Do not retry failed jobs in this batch.")
    parser.add_argument("--resolve-no-relation", metavar="JOB_ID", help="Record a reviewed no-relation outcome for one queued or failed job.")
    parser.add_argument("--reviewer", default="manual-review")
    parser.add_argument("--resolution-note", default="No graph-eligible relation is explicitly stated in the source evidence.")
    args = parser.parse_args()
    if args.resolve_no_relation:
        result = resolve_no_eligible_relation(args.resolve_no_relation, args.reviewer, args.resolution_note)
    elif args.revalidate_existing:
        result = revalidate_existing_candidates()
    else:
        result = extract(args.limit, set(args.job_id) or None, include_failed=not args.queued_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
