"""Auditable Qwen Batch review, calibration, admission, and rollback.

The service keeps paid provider operations explicit.  Read-only estimation never
writes an artifact or contacts DashScope; creating/synchronising a run is the
only provider-facing path.  Machine decisions use a separate provenance path
from the existing human review endpoints.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from pipelines.review_relation_candidates import (
    REVIEW_POLICY_VERSION,
    REVIEW_SYSTEM_PROMPT,
    _build_review_input,
    _preflight_rejection_reason,
    validate_review_response,
)

from .config import Settings
from .graph_metadata import narrative_scope
from .repository import (
    _ACTOR_NODE_TYPES,
    _REVIEW_WRITE_LOCK,
    _normalized_review_entity,
    _object_endpoint_node_types,
    _read_jsonl,
    _review_node_id,
    _write_jsonl,
    RuntimeRepository,
)


AUTOMATION_POLICY_VERSION = "qwen-batch-evidence-review-v1"
ENTITY_POLICY_VERSION = "qwen-batch-entity-review-v1"
QWEN_MODEL = "qwen3.8-max"
PRICE_TIMEZONE = timezone(timedelta(hours=8), "Asia/Hong_Kong")
_AUTOMATION_SYNC_LOCK = RLock()
HIGH_IMPACT_RELATIONS = {"ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT"}
RUN_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled", "canceled"}
PROVIDER_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled", "canceled"}
RETRY_CHUNK_SIZE = 250
PRICE_SNAPSHOT = {
    "currency": "CNY",
    "model": QWEN_MODEL,
    "checked_on": "2026-08-11",
    "source": "https://www.qianwenai.com/models/qwen3.8-max#pricing",
    "batch_input_cny_per_million": 6.0,
    "batch_output_cny_per_million": 18.0,
    "realtime_input_cny_per_million": 12.0,
    "realtime_output_cny_per_million": 36.0,
}
CALIBRATION_QUOTAS = {
    "relation_approve_high": 30,
    "relation_approve_other": 50,
    "relation_reject": 30,
    "entity_approve": 30,
    "entity_reject": 10,
}
CALIBRATION_THRESHOLDS = {
    "relation_approve_high": 1.0,
    "relation_approve_other": 0.98,
    "relation_reject": 0.95,
    "entity_approve": 0.98,
    "entity_reject": 0.95,
}
CRITICAL_ERROR_CATEGORIES = {
    "identity_confusion",
    "wrong_node_type",
    "context_contamination",
    "fabricated_quote",
}
RELATION_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "relation_evidence_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "candidate_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["recommend_approve", "recommend_reject", "abstain"]},
                "evidence_sufficiency": {"type": "string", "enum": ["direct", "partial", "insufficient"]},
                "relation_type_valid": {"type": "boolean"},
                "identity_mapping_confidence": {"type": "string", "enum": ["exact_literal", "ambiguous", "unmapped"]},
                "temporal_scope": {"type": "string", "enum": ["stable", "situational", "costume_specific", "unknown"]},
                "risk_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "supporting_quote": {"type": "string"},
                "verdict_rationale": {"type": "string"},
            },
            "required": [
                "candidate_id", "verdict", "evidence_sufficiency", "relation_type_valid",
                "identity_mapping_confidence", "temporal_scope", "risk_flags", "supporting_quote",
                "verdict_rationale",
            ],
        },
    },
}

ENTITY_SYSTEM_PROMPT = """你是《尘白禁区》知识图谱的实体证据审核员。你只判断输入中的地点或事件候选是否是原文明确命名、可复用的图谱实体。
不得依赖游戏常识、标题暗示或未提供的上下文。代词、章节名、邮件名、页面名、普通动作（例如训练、聊天、吃饭）、运营活动和概括短语不得创建实体。
recommend_approve 必须满足：名称在 supporting_quote 中逐字出现；候选类型准确；名称指向明确的地点或已经发生且可命名的剧情事件；引文可在给出的原文中逐字找到。
证据不足或身份不明确时选择 abstain。仅输出一个 JSON 对象，字段固定为：
{
  "entity_candidate_id": "输入中的 ID",
  "verdict": "recommend_approve | recommend_reject | abstain",
  "node_type_valid": true,
  "exact_name_in_quote": true,
  "reusable_named_entity": true,
  "risk_flags": [],
  "supporting_quote": "逐字引文或空字符串",
  "verdict_rationale": "不超过160字的证据说明"
}"""

ENTITY_JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "entity_evidence_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "entity_candidate_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["recommend_approve", "recommend_reject", "abstain"]},
                "node_type_valid": {"type": "boolean"},
                "exact_name_in_quote": {"type": "boolean"},
                "reusable_named_entity": {"type": "boolean"},
                "risk_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "supporting_quote": {"type": "string"},
                "verdict_rationale": {"type": "string"},
            },
            "required": [
                "entity_candidate_id", "verdict", "node_type_valid", "exact_name_in_quote",
                "reusable_named_entity", "risk_flags", "supporting_quote", "verdict_rationale",
            ],
        },
    },
}


class RelationBatchResponse(BaseModel):
    """Strict local contract used even when thinking mode cannot use JSON Schema."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str = Field(min_length=1, max_length=240)
    verdict: Literal["recommend_approve", "recommend_reject", "abstain"]
    evidence_sufficiency: Literal["direct", "partial", "insufficient"]
    relation_type_valid: bool
    identity_mapping_confidence: Literal["exact_literal", "ambiguous", "unmapped"]
    temporal_scope: Literal["stable", "situational", "costume_specific", "unknown"]
    risk_flags: list[str] = Field(max_length=12)
    supporting_quote: str = Field(max_length=12_000)
    verdict_rationale: str = Field(max_length=2_000)


class EntityBatchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    entity_candidate_id: str = Field(min_length=1, max_length=240)
    verdict: Literal["recommend_approve", "recommend_reject", "abstain"]
    node_type_valid: bool
    exact_name_in_quote: bool
    reusable_named_entity: bool
    risk_flags: list[str] = Field(max_length=12)
    supporting_quote: str = Field(max_length=12_000)
    verdict_rationale: str = Field(max_length=2_000)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object at {path}.")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stable_hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _automation_report_key(candidate_id: str, input_hash: str, pass_index: int) -> str:
    return "\x1f".join(
        (candidate_id, input_hash, QWEN_MODEL, str(pass_index), AUTOMATION_POLICY_VERSION)
    )


def _bounded_strings(value: Any, limit: int = 12, length: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = str(item or "").strip().replace("\n", " ")[:length]
        if text and text not in output:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _extract_json_object(content: Any) -> dict[str, Any]:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Provider response did not contain a JSON object.") from None
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Provider response JSON must be an object.")
    return value


def _contains_literal(text: Any, literal: Any) -> bool:
    return _normalized_review_entity(literal) in _normalized_review_entity(text)


class DashScopeBatchClient:
    """Small OpenAI-compatible Batch client; credentials never enter artifacts."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 120.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.client = client

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.api_key}"
        if self.client is not None:
            response = self.client.request(method, self.base_url + path, headers=headers, **kwargs)
        else:
            response = httpx.request(method, self.base_url + path, headers=headers, timeout=self.timeout, **kwargs)
        if response.is_error:
            preview = response.text.replace("\n", " ")[:800]
            raise RuntimeError(f"DashScope Batch HTTP {response.status_code}: {preview}")
        return response

    def upload(self, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            response = self._request(
                "POST", "/files", data={"purpose": "batch"},
                files={"file": (path.name, handle, "application/jsonl")},
            )
        return response.json()

    def create(self, input_file_id: str, endpoint: str, metadata: dict[str, str]) -> dict[str, Any]:
        return self._request(
            "POST", "/batches", json={
                "input_file_id": input_file_id,
                "endpoint": endpoint,
                "completion_window": "24h",
                "metadata": metadata,
            },
        ).json()

    def retrieve(self, batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/batches/{batch_id}").json()

    def cancel(self, batch_id: str) -> dict[str, Any]:
        return self._request("POST", f"/batches/{batch_id}/cancel").json()

    def content(self, file_id: str) -> bytes:
        return self._request("GET", f"/files/{file_id}/content").content


class ReviewAutomationService:
    def __init__(
        self,
        settings: Settings,
        repository: RuntimeRepository,
        *,
        batch_client: DashScopeBatchClient | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.root = settings.runtime_root / "review" / "automation"
        self.runs_root = self.root / "runs"
        self.entity_reports_path = settings.runtime_root / "review" / "entity_model_review_reports.jsonl"
        self.relation_reports_path = settings.runtime_root / "review" / "relation_model_review_reports.jsonl"
        self.calibration_samples_path = self.root / "calibration_samples.jsonl"
        self.decision_events_path = self.root / "decision_events.jsonl"
        self._batch_client = batch_client

    def _provider_settings(self) -> dict[str, Any]:
        provider = os.getenv("EVIDENCE_REVIEW_PROVIDER", os.getenv("RELATION_REVIEW_PROVIDER", "disabled")).strip()
        base_url = os.getenv(
            "EVIDENCE_REVIEW_BASE_URL",
            os.getenv("RELATION_REVIEW_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        ).strip()
        api_key = os.getenv("EVIDENCE_REVIEW_API_KEY", os.getenv("RELATION_REVIEW_API_KEY", "")).strip()
        model = os.getenv("EVIDENCE_REVIEW_MODEL", QWEN_MODEL).strip() or QWEN_MODEL
        try:
            budget = float(os.getenv("EVIDENCE_REVIEW_MAX_BUDGET_CNY", "300"))
        except (TypeError, ValueError):
            budget = 300.0
        if not math.isfinite(budget):
            budget = 300.0
        return {
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "budget_cny": max(1.0, min(budget, 10_000.0)),
            "configured": provider == "dashscope-batch" and bool(base_url and api_key) and model == QWEN_MODEL,
        }

    def _client(self) -> DashScopeBatchClient:
        if self._batch_client is not None:
            return self._batch_client
        provider = self._provider_settings()
        if not provider["configured"]:
            raise RuntimeError(
                "Qwen Batch requires EVIDENCE_REVIEW_PROVIDER=dashscope-batch, "
                "EVIDENCE_REVIEW_API_KEY, and EVIDENCE_REVIEW_MODEL=qwen3.8-max."
            )
        self._batch_client = DashScopeBatchClient(provider["base_url"], provider["api_key"])
        return self._batch_client

    def _documents(self) -> dict[str, dict[str, Any]]:
        return self.repository.documents_by_id()

    def _successful_report_indexes(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Return only exact, successful reports produced by this Qwen policy."""

        def index(path: Path) -> dict[str, dict[str, Any]]:
            output: dict[str, dict[str, Any]] = {}
            for report in _read_jsonl(path):
                reviewer = report.get("model_reviewer") or {}
                candidate_id = str(report.get("candidate_id") or report.get("entity_candidate_id") or "")
                input_hash = str(report.get("input_hash") or "")
                pass_index = int(report.get("pass_index") or 0)
                expected_key = _automation_report_key(candidate_id, input_hash, pass_index)
                if (
                    report.get("review_status") == "completed"
                    and reviewer.get("provider") == "dashscope-batch"
                    and reviewer.get("model") == QWEN_MODEL
                    and candidate_id
                    and input_hash
                    and pass_index in {1, 2}
                    and report.get("report_key") == expected_key
                ):
                    output[expected_key] = report
            return output

        return index(self.relation_reports_path), index(self.entity_reports_path)

    def _pending_relations(self) -> list[dict[str, Any]]:
        return [row for row in _read_jsonl(self.repository.review_candidates_path) if row.get("review_status") == "pending_review"]

    def _pending_entities(self) -> list[dict[str, Any]]:
        return [row for row in _read_jsonl(self.repository.entity_candidates_path) if row.get("review_status") == "pending_review"]

    def _entity_payload(self, candidate: dict[str, Any], documents: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str]:
        entity_name = str(candidate.get("entity_name") or "")
        remaining = 6_000
        evidence: list[dict[str, Any]] = []
        for document_id in candidate.get("evidence_document_ids", []):
            document = documents.get(str(document_id))
            if document is None or remaining <= 0:
                continue
            text = str(document.get("text") or "")
            normalized_name = _normalized_review_entity(entity_name)
            normalized_text = _normalized_review_entity(text)
            position = normalized_text.find(normalized_name)
            if position < 0:
                continue
            # Normalisation changes indices slightly; using the first raw hit is
            # preferable when available and a bounded prefix remains safe.
            raw_position = text.find(entity_name)
            raw_position = raw_position if raw_position >= 0 else min(position, len(text))
            start = max(0, raw_position - 1_500)
            excerpt = text[start : start + min(3_000, remaining)]
            remaining -= len(excerpt)
            evidence.append({
                "document_id": document_id,
                "page_id": document.get("page_id"),
                "source_type": document.get("source_type"),
                "title": document.get("title"),
                "text": excerpt,
            })
        payload = {
            "review_policy_version": ENTITY_POLICY_VERSION,
            "candidate": {
                "entity_candidate_id": candidate.get("entity_candidate_id"),
                "entity_name": entity_name,
                "proposed_node_type": candidate.get("proposed_node_type"),
                "source_types": candidate.get("source_types", []),
                "evidence_examples": candidate.get("evidence_examples", []),
            },
            "evidence": evidence,
        }
        return payload, _stable_hash(payload)

    @staticmethod
    def _rank(seed: str, identifier: str) -> str:
        return sha256(f"{seed}\x1f{identifier}".encode("utf-8")).hexdigest()

    def _selection(self, mode: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        relations = self._pending_relations()
        entities = self._pending_entities()
        if mode == "production":
            return relations, entities
        relation_limit, entity_limit = (60, 40) if mode == "test" else (300, 100)
        relations.sort(key=lambda row: self._rank(f"{mode}-relations-v1", str(row.get("candidate_id"))))
        entities.sort(key=lambda row: self._rank(f"{mode}-entities-v1", str(row.get("entity_candidate_id"))))
        return relations[:relation_limit], entities[:entity_limit]

    def estimate(self, mode: str = "production") -> dict[str, Any]:
        if mode not in {"test", "calibration", "production"}:
            raise ValueError("Automation mode must be test, calibration, or production.")
        relations, entities = self._selection(mode)
        documents = self._documents()
        relation_report_index, entity_report_index = self._successful_report_indexes()
        provider_relations: list[tuple[dict[str, Any], dict[str, Any], str, dict[str, Any] | None]] = []
        provider_entities: list[tuple[dict[str, Any], dict[str, Any], str, dict[str, Any] | None]] = []
        deterministic = Counter()
        relation_input_fingerprints: list[tuple[str, str, str | None]] = []
        entity_input_fingerprints: list[tuple[str, str]] = []
        gross_input_characters = 0
        first_input_characters = 0
        reused_reports = 0
        for candidate in relations:
            payload, input_hash = _build_review_input(candidate, documents)
            reason = _preflight_rejection_reason(candidate, payload, documents)
            relation_input_fingerprints.append(
                (str(candidate.get("candidate_id") or ""), input_hash, reason)
            )
            if reason:
                deterministic[reason] += 1
                continue
            candidate_id = str(candidate.get("candidate_id") or "")
            first = relation_report_index.get(_automation_report_key(candidate_id, input_hash, 1))
            provider_relations.append((candidate, payload, input_hash, first))
            characters = len(REVIEW_SYSTEM_PROMPT) + len(json.dumps(payload, ensure_ascii=False))
            gross_input_characters += characters
            if first:
                reused_reports += 1
            else:
                first_input_characters += characters
        for candidate in entities:
            payload, input_hash = self._entity_payload(candidate, documents)
            candidate_id = str(candidate.get("entity_candidate_id") or "")
            entity_input_fingerprints.append((candidate_id, input_hash))
            first = entity_report_index.get(_automation_report_key(candidate_id, input_hash, 1))
            provider_entities.append((candidate, payload, input_hash, first))
            characters = len(ENTITY_SYSTEM_PROMPT) + len(json.dumps(payload, ensure_ascii=False))
            gross_input_characters += characters
            if first:
                reused_reports += 1
            else:
                first_input_characters += characters
        gross_first_calls = len(provider_relations) + len(provider_entities)
        first_calls = sum(first is None for *_, first in provider_relations) + sum(
            first is None for *_, first in provider_entities
        )
        # The current 65 persisted provider reports average 2.46 prompt
        # characters per billed token for this exact relation prompt.  Use a
        # slightly more conservative 2.30 divisor and retain a further 10%
        # run-level headroom below; a generic CJK 1:1 estimate substantially
        # overstates this project's measured Qwen-compatible payloads.
        gross_first_input_tokens = math.ceil(gross_input_characters / 2.30)
        first_input_tokens = math.ceil(first_input_characters / 2.30)
        first_output_tokens = first_calls * 300
        known_relation_second_calls = 0
        unknown_relation_candidates = 0
        unknown_high_impact_candidates = 0
        for candidate, _, input_hash, first in provider_relations:
            candidate_id = str(candidate.get("candidate_id") or "")
            second = relation_report_index.get(_automation_report_key(candidate_id, input_hash, 2))
            high = str(candidate.get("relation_type") or "").upper() in HIGH_IMPACT_RELATIONS
            if second:
                reused_reports += 1
            if first is None:
                if second is None:
                    unknown_relation_candidates += 1
                    if high:
                        unknown_high_impact_candidates += 1
            elif (high or first.get("verdict") != "recommend_approve") and second is None:
                known_relation_second_calls += 1
        second_relation_calls = known_relation_second_calls + max(
            unknown_high_impact_candidates,
            math.ceil(unknown_relation_candidates * 0.65),
        )
        second_entity_calls = 0
        for candidate, _, input_hash, _ in provider_entities:
            candidate_id = str(candidate.get("entity_candidate_id") or "")
            second = entity_report_index.get(_automation_report_key(candidate_id, input_hash, 2))
            if second:
                reused_reports += 1
            else:
                second_entity_calls += 1
        second_calls = second_relation_calls + second_entity_calls
        average_input = gross_first_input_tokens / max(gross_first_calls, 1)
        second_input_tokens = math.ceil(second_calls * average_input)
        second_output_tokens = second_calls * 1_200
        input_tokens = first_input_tokens + second_input_tokens
        output_tokens = first_output_tokens + second_output_tokens
        first_cny = (
            first_input_tokens * PRICE_SNAPSHOT["batch_input_cny_per_million"]
            + first_output_tokens * PRICE_SNAPSHOT["batch_output_cny_per_million"]
        ) / 1_000_000
        second_cny = (
            second_input_tokens * PRICE_SNAPSHOT["batch_input_cny_per_million"]
            + second_output_tokens * PRICE_SNAPSHOT["batch_output_cny_per_million"]
        ) / 1_000_000
        batch_cny = (
            input_tokens * PRICE_SNAPSHOT["batch_input_cny_per_million"]
            + output_tokens * PRICE_SNAPSHOT["batch_output_cny_per_million"]
        ) / 1_000_000
        # Allow schema-repair/retry headroom in the displayed and enforced projection.
        projected_cny = round(batch_cny * 1.10, 2)
        now = datetime.now(UTC)
        local_now = now.astimezone(PRICE_TIMEZONE)
        price_day = local_now.date().isoformat()
        hash_payload = {
            "mode": mode,
            "policy_version": AUTOMATION_POLICY_VERSION,
            "model": QWEN_MODEL,
            "price_day": price_day,
            "prices": PRICE_SNAPSHOT,
            "input_fingerprint": _stable_hash({
                "relations": sorted(relation_input_fingerprints),
                "entities": sorted(entity_input_fingerprints),
            }),
            "counts": {
                "relations": len(relations),
                "entities": len(entities),
                "deterministic": sum(deterministic.values()),
                "first_calls": first_calls,
                "second_calls": second_calls,
                "reused_reports": reused_reports,
            },
            "estimated_tokens": {"input": input_tokens, "output": output_tokens},
        }
        estimate_hash = _stable_hash(hash_payload)
        provider = self._provider_settings()
        return {
            **hash_payload,
            "estimate_hash": estimate_hash,
            "generated_at": now.isoformat(),
            "expires_at": (
                local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            ).astimezone(UTC).isoformat(),
            "provider_configured": provider["configured"],
            "budget_cny": provider["budget_cny"],
            "projected_batch_cny": projected_cny,
            "projected_realtime_cny": round(projected_cny * 2, 2),
            "cost_breakdown": {
                "first_pass_cny": round(first_cny * 1.10, 2),
                "second_pass_cny": round(second_cny * 1.10, 2),
            },
            "deterministic_by_reason": dict(sorted(deterministic.items())),
            "policy": "Read-only estimate; no provider call or runtime write occurred.",
        }

    def _run_path(self, run_id: str) -> Path:
        if not re.fullmatch(r"review_run_[A-Za-z0-9_-]{8,80}", run_id):
            raise ValueError("Invalid automation run ID.")
        return self.runs_root / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._run_path(run_id) / "manifest.json"

    def _load_manifest(self, run_id: str) -> dict[str, Any]:
        manifest = _read_json(self._manifest_path(run_id))
        if not manifest:
            raise KeyError(run_id)
        return manifest

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.runs_root.exists():
            return []
        manifests = [_read_json(path) for path in self.runs_root.glob("*/manifest.json")]
        return sorted((item for item in manifests if item), key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def get_run(self, run_id: str) -> dict[str, Any]:
        manifest = {**self._load_manifest(run_id), "report_summary": self._report_summary(run_id)}
        if manifest.get("mode") == "calibration":
            manifest = {**manifest, "calibration": self.calibration_status(run_id)}
        return manifest

    def _report_summary(self, run_id: str) -> dict[str, Any]:
        reports = _read_jsonl(self._run_path(run_id) / "reports.jsonl")
        by_pass: dict[str, dict[str, dict[str, int]]] = {}
        for report in reports:
            pass_name = f"pass_{int(report.get('pass_index') or 0)}"
            kind = "relation" if report.get("candidate_id") else "entity"
            verdict = str(report.get("verdict") or "invalid")
            verdicts = by_pass.setdefault(pass_name, {}).setdefault(kind, {})
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        return {
            "total": len(reports),
            "audit_eligible": sum(bool(report.get("audit_eligible")) for report in reports),
            "reused": sum(bool(report.get("reused_from_report_id")) for report in reports),
            "by_pass": by_pass,
        }

    def _prepare_reusable_reports(self, manifest: dict[str, Any]) -> None:
        """Attach exact prior Qwen results to a new run without another paid call."""

        if manifest.get("mode") == "test":
            return
        relation_report_index, entity_report_index = self._successful_report_indexes()
        documents = self._documents()
        relation_candidates = {
            str(row.get("candidate_id")): row for row in self._pending_relations()
        }
        entity_candidates = {
            str(row.get("entity_candidate_id")): row for row in self._pending_entities()
        }
        reusable: list[dict[str, Any]] = []

        def attach(source: dict[str, Any]) -> None:
            source_run_id = str(source.get("automation_run_id") or "")
            source_report_id = str(source.get("report_id") or "")
            copied = {
                **source,
                "report_id": "automation_report_" + sha256(
                    f"{manifest['run_id']}\x1f{source_report_id}\x1freuse".encode("utf-8")
                ).hexdigest()[:20],
                "automation_run_id": manifest["run_id"],
                "reused_from_report_id": source_report_id,
                "reused_from_run_id": source_run_id or None,
                "reused_at": _utc_now(),
            }
            reusable.append(copied)

        for candidate_id in manifest.get("relation_candidate_ids", []):
            candidate = relation_candidates.get(str(candidate_id))
            if candidate is None:
                continue
            payload, input_hash = _build_review_input(candidate, documents)
            if _preflight_rejection_reason(candidate, payload, documents):
                continue
            for pass_index in (1, 2):
                source = relation_report_index.get(
                    _automation_report_key(str(candidate_id), input_hash, pass_index)
                )
                if source:
                    attach(source)
        for candidate_id in manifest.get("entity_candidate_ids", []):
            candidate = entity_candidates.get(str(candidate_id))
            if candidate is None:
                continue
            _, input_hash = self._entity_payload(candidate, documents)
            for pass_index in (1, 2):
                source = entity_report_index.get(
                    _automation_report_key(str(candidate_id), input_hash, pass_index)
                )
                if source:
                    attach(source)
        if reusable:
            self._append_reports(
                self._run_path(str(manifest["run_id"])) / "reports.jsonl", reusable
            )
        manifest["reused_report_count"] = len(reusable)
        manifest["reused_from_run_ids"] = sorted(
            {
                str(report.get("reused_from_run_id"))
                for report in reusable
                if report.get("reused_from_run_id")
            }
        )

    def _request_body(self, kind: str, payload: dict[str, Any], *, thinking: bool, test: bool) -> dict[str, Any]:
        if test:
            return {
                "model": "batch-test-model",
                "messages": [{"role": "user", "content": "Validate this JSONL request."}],
            }
        system = REVIEW_SYSTEM_PROMPT if kind == "relation" else ENTITY_SYSTEM_PROMPT
        if thinking:
            system += "\n这是独立的第二轮复核。不要参考任何上一轮结论。请在思考后仅在 content 中输出符合首轮字段定义的 JSON 对象，不要使用 Markdown。"
        body: dict[str, Any] = {
            "model": QWEN_MODEL,
            "temperature": 0,
            "enable_thinking": thinking,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        if not thinking:
            body["response_format"] = RELATION_JSON_SCHEMA if kind == "relation" else ENTITY_JSON_SCHEMA
        return body

    def _build_phase_rows(
        self,
        manifest: dict[str, Any],
        pass_index: int,
        *,
        retry_custom_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        run_id = str(manifest["run_id"])
        mode = str(manifest["mode"])
        documents = self._documents()
        relation_index = {str(row.get("candidate_id")): row for row in self._pending_relations()}
        entity_index = {str(row.get("entity_candidate_id")): row for row in self._pending_entities()}
        relation_reports, entity_reports = self._run_report_maps(run_id)
        rows: list[dict[str, Any]] = []
        request_index: list[dict[str, Any]] = []

        def add(kind: str, candidate_id: str, payload: dict[str, Any], input_hash: str) -> None:
            custom_id = candidate_id
            if retry_custom_ids is not None and custom_id not in retry_custom_ids:
                return
            endpoint = "/v1/chat/ds-test" if mode == "test" else "/v1/chat/completions"
            rows.append({
                "custom_id": custom_id,
                "method": "POST",
                "url": endpoint,
                "body": self._request_body(kind, payload, thinking=pass_index == 2, test=mode == "test"),
            })
            request_index.append({
                "custom_id": custom_id,
                "kind": kind,
                "candidate_id": candidate_id,
                "pass_index": pass_index,
                "input_hash": input_hash,
                "evidence_truncated": bool(payload.get("evidence_truncated")),
            })

        for candidate_id in manifest.get("relation_candidate_ids", []):
            candidate = relation_index.get(str(candidate_id))
            if candidate is None:
                continue
            payload, input_hash = _build_review_input(candidate, documents)
            if _preflight_rejection_reason(candidate, payload, documents):
                continue
            if retry_custom_ids is None and (str(candidate_id), pass_index) in relation_reports:
                continue
            if pass_index == 2:
                first = relation_reports.get((str(candidate_id), 1))
                high = str(candidate.get("relation_type") or "").upper() in HIGH_IMPACT_RELATIONS
                if not first or (first.get("verdict") == "recommend_approve" and not high):
                    continue
            add("relation", str(candidate_id), payload, input_hash)

        for candidate_id in manifest.get("entity_candidate_ids", []):
            candidate = entity_index.get(str(candidate_id))
            if candidate is None:
                continue
            if retry_custom_ids is None and (str(candidate_id), pass_index) in entity_reports:
                continue
            if pass_index == 2 and (str(candidate_id), 1) not in entity_reports:
                continue
            payload, input_hash = self._entity_payload(candidate, documents)
            add("entity", str(candidate_id), payload, input_hash)
        return rows, request_index

    @staticmethod
    def _write_jsonl_file(path: Path, rows: Iterable[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def _submit_phase(
        self,
        manifest: dict[str, Any],
        pass_index: int,
        *,
        retry_custom_ids: set[str] | None = None,
        retry_count: int = 0,
        retry_chunk_index: int | None = None,
        preserve_chunk_state: bool = False,
    ) -> dict[str, Any]:
        rows, request_index = self._build_phase_rows(manifest, pass_index, retry_custom_ids=retry_custom_ids)
        if not rows:
            manifest["active_phase"] = None
            return self._advance_after_pass(manifest, pass_index)
        if retry_custom_ids is not None and retry_chunk_index is None and len(rows) > RETRY_CHUNK_SIZE:
            chunks = [
                request_index[index : index + RETRY_CHUNK_SIZE]
                for index in range(0, len(request_index), RETRY_CHUNK_SIZE)
            ]
            manifest["retry_chunk_state"] = {
                "pass_index": pass_index,
                "retry_count": retry_count,
                "next_chunk_index": 2,
                "remaining_custom_id_chunks": [
                    [str(item["custom_id"]) for item in chunk]
                    for chunk in chunks[1:]
                ],
                "failed_custom_ids": [],
                "chunk_size": RETRY_CHUNK_SIZE,
                "created_at": _utc_now(),
            }
            rows = rows[:RETRY_CHUNK_SIZE]
            request_index = request_index[:RETRY_CHUNK_SIZE]
            retry_chunk_index = 1
        elif not preserve_chunk_state and retry_chunk_index is None:
            manifest.pop("retry_chunk_state", None)
        run_path = self._run_path(str(manifest["run_id"]))
        suffix = f"-retry{retry_count}" if retry_count else ""
        chunk_suffix = f"-part{retry_chunk_index:03d}" if retry_chunk_index is not None else ""
        phase_name = f"pass-{pass_index}{suffix}{chunk_suffix}"
        input_path = run_path / f"{phase_name}-input.jsonl"
        self._write_jsonl_file(input_path, rows)
        upload = self._client().upload(input_path)
        file_id = str(upload.get("id") or "")
        if not file_id:
            raise RuntimeError("DashScope file upload did not return a file ID.")
        endpoint = "/v1/chat/ds-test" if manifest["mode"] == "test" else "/v1/chat/completions"
        batch = self._client().create(file_id, endpoint, {
            "ds_name": str(manifest["run_id"])[:100],
            "ds_description": f"Project Snow {manifest['mode']} pass {pass_index}"[:200],
        })
        batch_id = str(batch.get("id") or "")
        if not batch_id:
            raise RuntimeError("DashScope batch creation did not return a batch ID.")
        phase = {
            "name": phase_name,
            "pass_index": pass_index,
            "retry_count": retry_count,
            "retry_chunk_index": retry_chunk_index,
            "request_count": len(rows),
            "request_index": request_index,
            "input_file": str(input_path),
            "input_file_sha256": sha256(input_path.read_bytes()).hexdigest(),
            "provider_input_file_id": file_id,
            "provider_batch_id": batch_id,
            "provider_status": str(batch.get("status") or "validating"),
            "submitted_at": _utc_now(),
        }
        if preserve_chunk_state and retry_chunk_index is not None:
            state = manifest.get("retry_chunk_state") or {}
            remaining = state.get("remaining_custom_id_chunks") or []
            submitted_custom_ids = [str(item["custom_id"]) for item in request_index]
            if remaining and [str(item) for item in remaining[0]] == submitted_custom_ids:
                remaining.pop(0)
            state["remaining_custom_id_chunks"] = remaining
            state["next_chunk_index"] = retry_chunk_index + 1
            state["updated_at"] = _utc_now()
        manifest.setdefault("phases", []).append(phase)
        manifest["active_phase"] = phase_name
        manifest["status"] = "submitted"
        manifest["updated_at"] = _utc_now()
        _write_json(self._manifest_path(str(manifest["run_id"])), manifest)
        return manifest

    def restart_active_phase_in_chunks(self, run_id: str) -> dict[str, Any]:
        """Cancel one stalled provider phase and resume its unfinished work in bounded chunks."""

        with _AUTOMATION_SYNC_LOCK:
            manifest = self._load_manifest(run_id)
            if manifest.get("status") not in {"submitted", "running", "finalizing", "cancelling"}:
                raise ValueError("Only an active automation phase can be restarted.")
            phase = self._phase(manifest)
            batch = self._client().retrieve(str(phase["provider_batch_id"]))
            provider_status = str(batch.get("status") or "unknown")
            if provider_status in PROVIDER_TERMINAL_STATUSES:
                return self._sync_run_locked(run_id)
            phase["restart_in_chunks"] = True
            phase["restart_requested_at"] = _utc_now()
            phase["provider_status"] = provider_status
            manifest["updated_at"] = _utc_now()
            _write_json(self._manifest_path(run_id), manifest)
            cancelled = self._client().cancel(str(phase["provider_batch_id"]))
            phase["provider_status"] = str(cancelled.get("status") or "cancelling")
            phase["restart_cancel_response_at"] = _utc_now()
            manifest["status"] = "running"
            manifest["updated_at"] = _utc_now()
            _write_json(self._manifest_path(run_id), manifest)
            return self.get_run(run_id)

    def force_restart_active_phase_in_chunks(self, run_id: str) -> dict[str, Any]:
        """Stop waiting for a stuck cancellation and resubmit its request index in chunks.

        This is an operator recovery path for providers that remain in
        ``cancelling`` indefinitely. Reports are keyed idempotently, so a late
        provider completion cannot create duplicate local decisions.
        """

        with _AUTOMATION_SYNC_LOCK:
            manifest = self._load_manifest(run_id)
            if manifest.get("status") not in {"submitted", "running", "finalizing", "cancelling"}:
                raise ValueError("Only an active automation phase can be force-restarted.")
            phase = self._phase(manifest)
            if not phase.get("restart_in_chunks"):
                raise ValueError("The active phase must be cancelled before a force restart.")
            batch = self._client().retrieve(str(phase["provider_batch_id"]))
            provider_status = str(batch.get("status") or "unknown")
            if provider_status in PROVIDER_TERMINAL_STATUSES:
                return self._sync_run_locked(run_id)
            retry_custom_ids = {
                str(item.get("custom_id") or "")
                for item in phase.get("request_index", [])
                if item.get("custom_id")
            }
            if not retry_custom_ids:
                raise ValueError("The active phase has no recoverable request index.")
            phase["provider_status"] = provider_status
            phase["force_restart_at"] = _utc_now()
            phase["abandoned_after_cancel_timeout"] = True
            manifest["active_phase"] = None
            manifest["updated_at"] = _utc_now()
            _write_json(self._manifest_path(run_id), manifest)
            return self._submit_phase(
                manifest,
                int(phase["pass_index"]),
                retry_custom_ids=retry_custom_ids,
                retry_count=int(phase.get("retry_count") or 0),
            )

    def resume_pending_retry_chunks(self, run_id: str) -> dict[str, Any]:
        """Resume a run left terminal by an older worker while chunks remain."""

        with _AUTOMATION_SYNC_LOCK:
            manifest = self._load_manifest(run_id)
            state = manifest.get("retry_chunk_state")
            if not isinstance(state, dict) or not (
                state.get("remaining_custom_id_chunks") or state.get("failed_custom_ids")
            ):
                raise ValueError("The run has no pending retry chunks to resume.")
            chunk_phases = [
                phase for phase in manifest.get("phases", [])
                if phase.get("retry_chunk_index") is not None
            ]
            if not chunk_phases:
                raise ValueError("No retry chunk phase is recorded in the manifest.")
            # Older workers did not persist results_imported_at. Their output
            # and usage are already present, so mark them imported to avoid
            # double-counting when the new worker resumes the state machine.
            for phase in chunk_phases:
                if phase.get("provider_status") == "completed" and phase.get("completed_at") and not phase.get("results_imported_at"):
                    phase["results_imported_at"] = phase.get("completed_at")
                    phase["recovered_import_marker"] = True
            latest = max(
                chunk_phases,
                key=lambda phase: int(phase.get("retry_chunk_index") or 0),
            )
            manifest["status"] = "running"
            manifest["active_phase"] = latest.get("name")
            manifest["resumed_after_stale_terminal_status_at"] = _utc_now()
            _write_json(self._manifest_path(run_id), manifest)
            return self._sync_run_locked(run_id)

    def create_run(self, mode: str, estimate_hash: str, calibration_run_id: str | None = None) -> dict[str, Any]:
        estimate = self.estimate(mode)
        if estimate_hash != estimate["estimate_hash"]:
            raise ValueError("Estimate hash is stale or does not match the current review queue and pricing day.")
        provider = self._provider_settings()
        if estimate["projected_batch_cny"] > provider["budget_cny"]:
            raise ValueError(
                f"Projected cost CNY {estimate['projected_batch_cny']:.2f} exceeds the configured "
                f"CNY {provider['budget_cny']:.2f} budget ceiling."
            )
        if mode != "test" and not provider["configured"]:
            raise RuntimeError("Qwen Batch provider is not fully configured.")
        if mode == "production":
            if not calibration_run_id:
                raise ValueError("A labelled calibration run is required before a production run.")
            try:
                calibration_manifest = self._load_manifest(calibration_run_id)
            except KeyError:
                raise ValueError("The requested calibration run does not exist.") from None
            if calibration_manifest.get("mode") != "calibration" or calibration_manifest.get("status") != "awaiting_calibration":
                raise ValueError("Production requires a completed calibration-mode run.")
            if (
                calibration_manifest.get("model") != QWEN_MODEL
                or calibration_manifest.get("policy_version") != AUTOMATION_POLICY_VERSION
            ):
                raise ValueError("Production requires calibration from the current Qwen model and automation policy.")
            calibration = self.calibration_status(calibration_run_id)
            if not calibration["sample_count"] or calibration["labelled_count"] != calibration["sample_count"]:
                raise ValueError("All available calibration samples must be labelled before production submission.")
        relations, entities = self._selection(mode)
        now = datetime.now(UTC)
        run_id = "review_run_" + now.strftime("%Y%m%dT%H%M%S%fZ") + "_" + estimate_hash[:10]
        manifest = {
            "run_id": run_id,
            "mode": mode,
            "status": "creating",
            "policy_version": AUTOMATION_POLICY_VERSION,
            "relation_policy_version": REVIEW_POLICY_VERSION,
            "entity_policy_version": ENTITY_POLICY_VERSION,
            "model": "batch-test-model" if mode == "test" else QWEN_MODEL,
            "provider": "dashscope-batch",
            "estimate": {key: value for key, value in estimate.items() if key != "provider_configured"},
            "calibration_run_id": calibration_run_id,
            "relation_candidate_ids": [str(row.get("candidate_id")) for row in relations if row.get("candidate_id")],
            "entity_candidate_ids": [str(row.get("entity_candidate_id")) for row in entities if row.get("entity_candidate_id")],
            "phases": [],
            "active_phase": None,
            "actual_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "actual_cost_cny": 0.0,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        run_path = self._run_path(run_id)
        run_path.mkdir(parents=True, exist_ok=False)
        _write_json(self._manifest_path(run_id), manifest)
        try:
            self._prepare_reusable_reports(manifest)
            _write_json(self._manifest_path(run_id), manifest)
            return self._submit_phase(manifest, 1)
        except Exception:
            manifest["status"] = "failed"
            manifest["last_error"] = "Initial Batch submission failed. Credentials were not persisted."
            manifest["updated_at"] = _utc_now()
            _write_json(self._manifest_path(run_id), manifest)
            raise

    def _phase(self, manifest: dict[str, Any]) -> dict[str, Any]:
        active = str(manifest.get("active_phase") or "")
        phase = next((row for row in manifest.get("phases", []) if row.get("name") == active), None)
        if phase is None:
            raise ValueError("Run has no active Batch phase.")
        return phase

    @staticmethod
    def _jsonl_bytes(data: bytes) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for number, line in enumerate(data.decode("utf-8-sig").splitlines(), start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Batch result line {number} is not an object.")
            rows.append(value)
        return rows

    def _validate_entity_response(
        self,
        response: dict[str, Any],
        candidate: dict[str, Any],
        documents: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        candidate_id = str(candidate.get("entity_candidate_id") or "")
        if str(response.get("entity_candidate_id") or "") != candidate_id:
            raise ValueError("Provider entity_candidate_id does not match the request.")
        verdict = str(response.get("verdict") or "")
        if verdict not in {"recommend_approve", "recommend_reject", "abstain"}:
            raise ValueError("Provider entity verdict is outside the controlled vocabulary.")
        boolean_fields = ("node_type_valid", "exact_name_in_quote", "reusable_named_entity")
        if any(not isinstance(response.get(field), bool) for field in boolean_fields):
            raise ValueError("Provider entity response has invalid boolean fields.")
        quote = str(response.get("supporting_quote") or "").strip()
        risk_flags = _bounded_strings(response.get("risk_flags"))
        evidence = [
            documents[str(identifier)]
            for identifier in candidate.get("evidence_document_ids", [])
            if str(identifier) in documents
        ]
        validation_flags: list[str] = []
        if verdict == "recommend_approve":
            if not response["node_type_valid"]:
                validation_flags.append("node_type_not_valid")
            if not response["exact_name_in_quote"] or not _contains_literal(quote, candidate.get("entity_name")):
                validation_flags.append("entity_name_not_literal_in_quote")
            if not response["reusable_named_entity"]:
                validation_flags.append("not_reusable_named_entity")
            if not quote or not any(_contains_literal(document.get("text"), quote) for document in evidence):
                validation_flags.append("supporting_quote_not_found")
            if risk_flags:
                validation_flags.append("provider_risk_flags_present")
            if validation_flags:
                verdict = "abstain"
        return {
            "verdict": verdict,
            "model_verdict": str(response.get("verdict") or ""),
            "node_type_valid": response["node_type_valid"],
            "exact_name_in_quote": response["exact_name_in_quote"],
            "reusable_named_entity": response["reusable_named_entity"],
            "risk_flags": risk_flags,
            "validation_flags": validation_flags,
            "supporting_quote": quote,
            "verdict_rationale": str(response.get("verdict_rationale") or "").strip()[:1_000],
            "audit_eligible": verdict == "recommend_approve" and not validation_flags,
        }

    def _append_reports(self, path: Path, reports: list[dict[str, Any]]) -> None:
        existing = _read_jsonl(path)
        by_key = {str(row.get("report_key") or row.get("report_id")): row for row in existing}
        for report in reports:
            by_key[str(report.get("report_key") or report.get("report_id"))] = report
        _write_jsonl(path, by_key.values())

    def _import_results(
        self,
        manifest: dict[str, Any],
        phase: dict[str, Any],
        output_rows: list[dict[str, Any]],
    ) -> tuple[set[str], dict[str, int]]:
        expected = {str(row["custom_id"]): row for row in phase.get("request_index", [])}
        seen: set[str] = set()
        failed: set[str] = set()
        relation_candidates = {str(row.get("candidate_id")): row for row in self._pending_relations()}
        entity_candidates = {str(row.get("entity_candidate_id")): row for row in self._pending_entities()}
        documents = self._documents()
        relation_reports: list[dict[str, Any]] = []
        entity_reports: list[dict[str, Any]] = []
        usage_total: Counter[str] = Counter()
        for row in output_rows:
            custom_id = str(row.get("custom_id") or "")
            if custom_id in seen or custom_id not in expected:
                if custom_id:
                    failed.add(custom_id)
                continue
            seen.add(custom_id)
            index = expected[custom_id]
            response = row.get("response") or {}
            if int(response.get("status_code") or 0) != 200:
                failed.add(custom_id)
                continue
            body = response.get("body") or {}
            usage = body.get("usage") or {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(key), int):
                    usage_total[key] += usage[key]
            try:
                message = body["choices"][0]["message"]
                raw_content = message.get("content")
                parsed = _extract_json_object(raw_content)
                try:
                    direct_value = json.loads(str(raw_content or "").strip())
                    exact_json_format = isinstance(direct_value, dict) and direct_value == parsed
                except json.JSONDecodeError:
                    exact_json_format = False
                candidate_id = str(index["candidate_id"])
                pass_index = int(index["pass_index"])
                base = {
                    "report_id": "automation_report_" + sha256(
                        f"{manifest['run_id']}\x1f{custom_id}\x1f{pass_index}\x1f{index['input_hash']}".encode("utf-8")
                    ).hexdigest()[:20],
                    "report_key": _automation_report_key(candidate_id, index["input_hash"], pass_index),
                    "automation_run_id": manifest["run_id"],
                    "pass_index": pass_index,
                    "input_hash": index["input_hash"],
                    "model_reviewer": {"provider": "dashscope-batch", "model": QWEN_MODEL},
                    "reviewed_at": _utc_now(),
                    "review_status": "completed",
                    "usage": usage,
                }
                if index["kind"] == "relation":
                    parsed = RelationBatchResponse.model_validate(parsed).model_dump()
                    candidate = relation_candidates[candidate_id]
                    validated = validate_review_response(parsed, candidate, documents)
                    if index.get("evidence_truncated") and validated.get("verdict") == "recommend_approve":
                        validated["verdict"] = "abstain"
                        validated["audit_eligible"] = False
                        validated.setdefault("validation_flags", []).append("evidence_excerpted_for_review")
                    relation_reports.append({
                        **base,
                        "candidate_id": candidate_id,
                        "review_group_id": candidate.get("review_group_id"),
                        "relation_type": candidate.get("relation_type"),
                        "source_type": candidate.get("source_type"),
                        "input_evidence_truncated": bool(index.get("evidence_truncated")),
                        **validated,
                    })
                else:
                    parsed = EntityBatchResponse.model_validate(parsed).model_dump()
                    candidate = entity_candidates[candidate_id]
                    validated = self._validate_entity_response(parsed, candidate, documents)
                    entity_reports.append({
                        **base,
                        "entity_candidate_id": candidate_id,
                        "proposed_node_type": candidate.get("proposed_node_type"),
                        **validated,
                    })
                if not exact_json_format:
                    target = relation_reports[-1] if index["kind"] == "relation" else entity_reports[-1]
                    target["verdict"] = "abstain"
                    target["audit_eligible"] = False
                    target.setdefault("validation_flags", []).append("provider_output_not_exact_json")
            except Exception:
                failed.add(custom_id)
        failed.update(set(expected) - seen)
        if relation_reports:
            self._append_reports(self.relation_reports_path, relation_reports)
        if entity_reports:
            self._append_reports(self.entity_reports_path, entity_reports)
        run_reports = self._run_path(str(manifest["run_id"])) / "reports.jsonl"
        self._append_reports(run_reports, relation_reports + entity_reports)
        return failed, dict(usage_total)

    def _run_report_maps(
        self, run_id: str
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
        relation: dict[tuple[str, int], dict[str, Any]] = {}
        entity: dict[tuple[str, int], dict[str, Any]] = {}
        for report in _read_jsonl(self._run_path(run_id) / "reports.jsonl"):
            pass_index = int(report.get("pass_index") or 0)
            if report.get("candidate_id"):
                relation[(str(report["candidate_id"]), pass_index)] = report
            elif report.get("entity_candidate_id"):
                entity[(str(report["entity_candidate_id"]), pass_index)] = report
        return relation, entity

    def _advance_after_pass(self, manifest: dict[str, Any], pass_index: int) -> dict[str, Any]:
        if manifest["mode"] == "test":
            manifest["status"] = "completed"
            manifest["active_phase"] = None
        elif pass_index == 1:
            projected_remaining = float(
                (manifest.get("estimate", {}).get("cost_breakdown") or {}).get("second_pass_cny") or 0
            )
            budget = float(manifest.get("estimate", {}).get("budget_cny") or 300)
            if float(manifest.get("actual_cost_cny") or 0) + projected_remaining > budget:
                manifest["status"] = "failed"
                manifest["active_phase"] = None
                manifest["last_error"] = (
                    "Second-pass submission was stopped because actual first-pass cost plus the "
                    "remaining projection exceeds the configured budget ceiling."
                )
                manifest["updated_at"] = _utc_now()
                _write_json(self._manifest_path(str(manifest["run_id"])), manifest)
                return manifest
            return self._submit_phase(manifest, 2)
        else:
            manifest["active_phase"] = None
            manifest["status"] = "awaiting_calibration" if manifest["mode"] == "calibration" else "ready_to_admit"
            if manifest["mode"] == "calibration":
                self._create_calibration_samples(manifest)
        manifest["updated_at"] = _utc_now()
        _write_json(self._manifest_path(str(manifest["run_id"])), manifest)
        return manifest

    def sync_run(self, run_id: str) -> dict[str, Any]:
        with _AUTOMATION_SYNC_LOCK:
            return self._sync_run_locked(run_id)

    def _sync_run_locked(self, run_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(run_id)
        # A stale process may have written a terminal status while a newer
        # process was still holding retry chunks. Re-open such a manifest so
        # the chunk state machine can finish before admission.
        pending_chunks = manifest.get("retry_chunk_state")
        has_pending_chunks = bool(
            isinstance(pending_chunks, dict)
            and (
                pending_chunks.get("remaining_custom_id_chunks")
                or pending_chunks.get("failed_custom_ids")
            )
        )
        if manifest.get("status") in RUN_TERMINAL_STATUSES | {"awaiting_calibration", "ready_to_admit", "admitted", "rolled_back"} and not has_pending_chunks:
            return self.get_run(run_id)
        if has_pending_chunks and manifest.get("status") in {"ready_to_admit", "admitted"}:
            manifest["status"] = "running"
            manifest["active_phase"] = manifest.get("active_phase") or next(
                (
                    phase.get("name")
                    for phase in reversed(manifest.get("phases", []))
                    if phase.get("retry_chunk_index") is not None
                ),
            )
            if not manifest.get("active_phase"):
                manifest["last_error"] = "Retry chunks remain but no resumable active phase was found."
                _write_json(self._manifest_path(run_id), manifest)
                return manifest
            _write_json(self._manifest_path(run_id), manifest)
        phase = self._phase(manifest)
        batch = self._client().retrieve(str(phase["provider_batch_id"]))
        provider_status = str(batch.get("status") or "unknown")
        phase["provider_status"] = provider_status
        phase["request_counts"] = batch.get("request_counts") or {}
        phase["last_synced_at"] = _utc_now()
        manifest["status"] = "running" if provider_status not in PROVIDER_TERMINAL_STATUSES else provider_status
        manifest["updated_at"] = _utc_now()
        output_file_id = str(batch.get("output_file_id") or "")
        error_file_id = str(batch.get("error_file_id") or "")
        terminal_failure = provider_status in {"failed", "expired", "cancelled", "canceled"}
        if terminal_failure and not output_file_id:
            if phase.get("restart_in_chunks"):
                failed = {
                    str(item.get("custom_id") or "")
                    for item in phase.get("request_index", [])
                    if item.get("custom_id")
                }
                phase["provider_terminal_status"] = provider_status
                phase["completed_at"] = _utc_now()
                phase["failed_custom_ids"] = sorted(failed)
                phase["restart_without_output"] = True
                _write_json(self._manifest_path(run_id), manifest)
                return self._submit_phase(
                    manifest,
                    int(phase["pass_index"]),
                    retry_custom_ids=failed,
                    retry_count=int(phase.get("retry_count") or 0),
                )
            manifest["last_error"] = f"DashScope Batch phase ended as {provider_status} without an output file."
            _write_json(self._manifest_path(run_id), manifest)
            return manifest
        if provider_status != "completed" and not (terminal_failure and output_file_id):
            _write_json(self._manifest_path(run_id), manifest)
            return manifest

        if phase.get("results_imported_at"):
            failed = {str(item) for item in phase.get("failed_custom_ids", []) if item}
        else:
            run_path = self._run_path(run_id)
            output_data = self._client().content(output_file_id) if output_file_id else b""
            output_path = run_path / f"{phase['name']}-output.jsonl"
            output_path.write_bytes(output_data)
            error_rows: list[dict[str, Any]] = []
            if error_file_id:
                error_data = self._client().content(error_file_id)
                error_path = run_path / f"{phase['name']}-errors.jsonl"
                error_path.write_bytes(error_data)
                error_rows = self._jsonl_bytes(error_data)
            if manifest["mode"] == "test":
                failed = {
                    str(row.get("custom_id") or "") for row in error_rows if row.get("custom_id")
                }
                usage: dict[str, int] = {}
            else:
                failed, usage = self._import_results(manifest, phase, self._jsonl_bytes(output_data))
                failed.update(str(row.get("custom_id") or "") for row in error_rows if row.get("custom_id"))
            phase["completed_at"] = _utc_now()
            phase["results_imported_at"] = _utc_now()
            if terminal_failure:
                phase["provider_terminal_status"] = provider_status
                phase["recovered_terminal_output"] = True
            phase["failed_custom_ids"] = sorted(failed)
            phase["usage"] = usage
            actual = manifest.setdefault("actual_usage", {})
            for key, value in usage.items():
                actual[key] = int(actual.get(key) or 0) + int(value)
            manifest["actual_cost_cny"] = round(
                (
                    int(actual.get("prompt_tokens") or 0) * PRICE_SNAPSHOT["batch_input_cny_per_million"]
                    + int(actual.get("completion_tokens") or 0) * PRICE_SNAPSHOT["batch_output_cny_per_million"]
                ) / 1_000_000,
                4,
            )
            # Persist imported results before attempting another provider submission.
            # A transient upload/create error can then be resumed without importing
            # the same usage or reports a second time.
            _write_json(self._manifest_path(run_id), manifest)
        if manifest["actual_cost_cny"] > float(manifest["estimate"]["budget_cny"]):
            manifest["status"] = "failed"
            manifest["last_error"] = "Actual usage exceeded the configured budget ceiling; no further phase was submitted."
            manifest["active_phase"] = None
            _write_json(self._manifest_path(run_id), manifest)
            return manifest
        retry_chunk_index = phase.get("retry_chunk_index")
        chunk_state = manifest.get("retry_chunk_state")
        if retry_chunk_index is not None and isinstance(chunk_state, dict):
            accumulated_failures = {
                str(item) for item in chunk_state.get("failed_custom_ids", []) if item
            }
            accumulated_failures.update(failed)
            chunk_state["failed_custom_ids"] = sorted(accumulated_failures)
            chunk_state["updated_at"] = _utc_now()
            remaining_chunks = chunk_state.get("remaining_custom_id_chunks") or []
            _write_json(self._manifest_path(run_id), manifest)
            if remaining_chunks:
                next_custom_ids = {str(item) for item in remaining_chunks[0] if item}
                return self._submit_phase(
                    manifest,
                    int(phase["pass_index"]),
                    retry_custom_ids=next_custom_ids,
                    retry_count=int(phase.get("retry_count") or 0),
                    retry_chunk_index=int(chunk_state.get("next_chunk_index") or 1),
                    preserve_chunk_state=True,
                )
            manifest.pop("retry_chunk_state", None)
            failed = accumulated_failures
            _write_json(self._manifest_path(run_id), manifest)
        elif phase.get("restart_in_chunks") and failed:
            # A manually restarted non-chunk phase keeps its retry number. The
            # cancellation is an operational recovery, not a consumed model retry.
            _write_json(self._manifest_path(run_id), manifest)
            return self._submit_phase(
                manifest,
                int(phase["pass_index"]),
                retry_custom_ids=failed,
                retry_count=int(phase.get("retry_count") or 0),
            )
        if failed and int(phase.get("retry_count") or 0) < 2:
            _write_json(self._manifest_path(run_id), manifest)
            return self._submit_phase(
                manifest,
                int(phase["pass_index"]),
                retry_custom_ids=failed,
                retry_count=int(phase.get("retry_count") or 0) + 1,
            )
        manifest["active_phase"] = None
        _write_json(self._manifest_path(run_id), manifest)
        return self._advance_after_pass(manifest, int(phase["pass_index"]))

    @staticmethod
    def _relation_prediction(candidate: dict[str, Any], reports: dict[tuple[str, int], dict[str, Any]]) -> str:
        candidate_id = str(candidate.get("candidate_id") or "")
        first, second = reports.get((candidate_id, 1)), reports.get((candidate_id, 2))
        high = str(candidate.get("relation_type") or "").upper() in HIGH_IMPACT_RELATIONS
        if high:
            if first and second and first.get("audit_eligible") and second.get("audit_eligible"):
                return "approve"
        else:
            eligible = [report for report in (first, second) if report and report.get("audit_eligible")]
            conflict = any(report and report.get("verdict") == "recommend_reject" for report in (first, second))
            if eligible and not conflict:
                return "approve"
        if first and second and all(report.get("verdict") == "recommend_reject" for report in (first, second)):
            return "reject"
        return "needs_human_review"

    @staticmethod
    def _entity_prediction(candidate: dict[str, Any], reports: dict[tuple[str, int], dict[str, Any]]) -> str:
        candidate_id = str(candidate.get("entity_candidate_id") or "")
        first, second = reports.get((candidate_id, 1)), reports.get((candidate_id, 2))
        if first and second and first.get("audit_eligible") and second.get("audit_eligible"):
            return "approve"
        if first and second and all(report.get("verdict") == "recommend_reject" for report in (first, second)):
            return "reject"
        return "needs_human_review"

    def _create_calibration_samples(self, manifest: dict[str, Any]) -> None:
        run_id = str(manifest["run_id"])
        relation_reports, entity_reports = self._run_report_maps(run_id)
        relation_index = {str(row.get("candidate_id")): row for row in self._pending_relations()}
        entity_index = {str(row.get("entity_candidate_id")): row for row in self._pending_entities()}
        pools: dict[str, list[dict[str, Any]]] = {name: [] for name in CALIBRATION_QUOTAS}
        for candidate_id in manifest.get("relation_candidate_ids", []):
            candidate = relation_index.get(str(candidate_id))
            if candidate is None:
                continue
            prediction = self._relation_prediction(candidate, relation_reports)
            high = str(candidate.get("relation_type") or "").upper() in HIGH_IMPACT_RELATIONS
            category = (
                "relation_approve_high" if prediction == "approve" and high
                else "relation_approve_other" if prediction == "approve"
                else "relation_reject" if prediction == "reject"
                else None
            )
            if category:
                pools[category].append({
                    "kind": "relation",
                    "candidate_id": str(candidate_id),
                    "prediction": prediction,
                    "candidate_snapshot": {
                        key: candidate.get(key)
                        for key in (
                            "subject", "relation_type", "object", "source_type", "evidence_quote",
                            "evidence_document_ids", "evidence_page_ids",
                        )
                    },
                })
        for candidate_id in manifest.get("entity_candidate_ids", []):
            candidate = entity_index.get(str(candidate_id))
            if candidate is None:
                continue
            prediction = self._entity_prediction(candidate, entity_reports)
            category = "entity_approve" if prediction == "approve" else "entity_reject" if prediction == "reject" else None
            if category:
                pools[category].append({
                    "kind": "entity",
                    "candidate_id": str(candidate_id),
                    "prediction": prediction,
                    "candidate_snapshot": {
                        key: candidate.get(key)
                        for key in (
                            "entity_name", "normalized_name", "proposed_node_type", "source_types",
                            "evidence_document_ids", "evidence_page_ids", "evidence_examples",
                        )
                    },
                })
        existing = [row for row in _read_jsonl(self.calibration_samples_path) if row.get("run_id") != run_id]
        created: list[dict[str, Any]] = []
        for category, quota in CALIBRATION_QUOTAS.items():
            pool = sorted(
                pools[category],
                key=lambda row: self._rank("qwen-calibration-fixed-seed-v1:" + category, row["candidate_id"]),
            )
            for item in pool[:quota]:
                sample_id = "calibration_" + sha256(
                    f"{run_id}\x1f{category}\x1f{item['candidate_id']}".encode("utf-8")
                ).hexdigest()[:20]
                created.append({
                    "sample_id": sample_id,
                    "run_id": run_id,
                    "category": category,
                    **item,
                    "correct": None,
                    "critical_error": False,
                    "error_category": "none",
                    "created_at": _utc_now(),
                })
        _write_jsonl(self.calibration_samples_path, existing + created)

    def calibration_status(self, run_id: str) -> dict[str, Any]:
        samples = [row for row in _read_jsonl(self.calibration_samples_path) if row.get("run_id") == run_id]
        relation_index = {
            str(row.get("candidate_id")): row for row in _read_jsonl(self.repository.review_candidates_path)
        }
        entity_index = {
            str(row.get("entity_candidate_id")): row for row in _read_jsonl(self.repository.entity_candidates_path)
        }
        relation_reports, entity_reports = self._run_report_maps(run_id)
        visible_samples: list[dict[str, Any]] = []
        for sample in samples:
            candidate_id = str(sample.get("candidate_id") or "")
            if sample.get("kind") == "relation":
                candidate = relation_index.get(candidate_id, {})
                snapshot = sample.get("candidate_snapshot") or candidate
                display = {
                    "subject": snapshot.get("subject"),
                    "relation_type": snapshot.get("relation_type"),
                    "object": snapshot.get("object"),
                    "source_type": snapshot.get("source_type"),
                    "evidence_quote": snapshot.get("evidence_quote"),
                }
                reports = [relation_reports.get((candidate_id, index)) for index in (1, 2)]
            else:
                candidate = entity_index.get(candidate_id, {})
                snapshot = sample.get("candidate_snapshot") or candidate
                display = {
                    "entity_name": snapshot.get("entity_name"),
                    "proposed_node_type": snapshot.get("proposed_node_type"),
                    "source_types": snapshot.get("source_types"),
                    "evidence_examples": snapshot.get("evidence_examples"),
                }
                reports = [entity_reports.get((candidate_id, index)) for index in (1, 2)]
            visible_samples.append({
                **sample,
                "display": display,
                "reports": [
                    {
                        "pass_index": report.get("pass_index"),
                        "verdict": report.get("verdict"),
                        "supporting_quote": report.get("supporting_quote"),
                        "verdict_rationale": report.get("verdict_rationale"),
                        "risk_flags": report.get("risk_flags", []),
                        "validation_flags": report.get("validation_flags", []),
                    }
                    for report in reports
                    if report
                ],
            })
        categories: dict[str, dict[str, Any]] = {}
        for category, quota in CALIBRATION_QUOTAS.items():
            rows = [row for row in samples if row.get("category") == category]
            labelled = [row for row in rows if isinstance(row.get("correct"), bool)]
            correct = sum(bool(row.get("correct")) for row in labelled)
            accuracy = correct / len(labelled) if labelled else 0.0
            critical = sum(bool(row.get("critical_error")) for row in labelled)
            passed = (
                len(rows) >= quota
                and len(labelled) >= quota
                and accuracy >= CALIBRATION_THRESHOLDS[category]
                and critical == 0
            )
            categories[category] = {
                "required": quota,
                "available": len(rows),
                "labelled": len(labelled),
                "correct": correct,
                "accuracy": round(accuracy, 4),
                "critical_errors": critical,
                "threshold": CALIBRATION_THRESHOLDS[category],
                "passed": passed,
            }
        return {
            "run_id": run_id,
            "sample_count": len(samples),
            "labelled_count": sum(isinstance(row.get("correct"), bool) for row in samples),
            "categories": categories,
            "all_passed": all(row["passed"] for row in categories.values()),
            "samples": visible_samples,
        }

    def label_calibration(self, sample_id: str, label: dict[str, Any]) -> dict[str, Any]:
        with _REVIEW_WRITE_LOCK:
            samples = _read_jsonl(self.calibration_samples_path)
            sample = next((row for row in samples if row.get("sample_id") == sample_id), None)
            if sample is None:
                raise KeyError(sample_id)
            error_category = str(label.get("error_category") or "none")
            critical = bool(label.get("critical_error")) or error_category in CRITICAL_ERROR_CATEGORIES
            if bool(label.get("correct")) and (critical or error_category != "none"):
                raise ValueError("A correct sample cannot carry an error category or critical-error flag.")
            sample.update({
                "correct": bool(label.get("correct")),
                "critical_error": critical,
                "error_category": error_category,
                "reviewer_id": str(label.get("reviewer_id") or "")[:120],
                "note": str(label.get("note") or "")[:2_000],
                "labelled_at": _utc_now(),
            })
            _write_jsonl(self.calibration_samples_path, samples)
            return sample

    def _calibration_gates(self, run_id: str | None) -> dict[str, bool]:
        if not run_id:
            return {category: False for category in CALIBRATION_QUOTAS}
        status = self.calibration_status(run_id)
        return {category: bool(row["passed"]) for category, row in status["categories"].items()}

    def grant_calibration_override(
        self,
        run_id: str,
        *,
        authorized_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record an explicit user waiver for a fully labelled calibration run.

        The waiver never changes model reports. It only opens admission gates,
        switches admission to strict two-pass consensus, and remains visible in
        the run manifest for later audit and rollback.
        """

        with _AUTOMATION_SYNC_LOCK:
            manifest = self._load_manifest(run_id)
            if manifest.get("mode") != "production" or manifest.get("status") not in {"ready_to_admit", "rolled_back"}:
                raise ValueError("Calibration override requires a completed or rolled-back production run.")
            if manifest.get("retry_chunk_state") or manifest.get("active_phase"):
                raise ValueError("Calibration override is blocked while retry work remains.")
            calibration_run_id = str(manifest.get("calibration_run_id") or "")
            calibration = self.calibration_status(calibration_run_id)
            samples = calibration.get("samples") or []
            if not samples or calibration.get("labelled_count") != calibration.get("sample_count"):
                raise ValueError("Calibration override requires every available sample to be labelled.")
            if any(not bool(sample.get("correct")) or bool(sample.get("critical_error")) for sample in samples):
                raise ValueError("Calibration override requires all labelled samples to be correct with zero critical errors.")
            now = _utc_now()
            previous_admission = {
                key: manifest.get(key)
                for key in ("admitted_at", "admission_counts", "calibration_gates", "admission_attempt_id")
                if manifest.get(key) is not None
            }
            if previous_admission:
                manifest.setdefault("admission_history", []).append({
                    **previous_admission,
                    "rolled_back_at": manifest.get("rolled_back_at"),
                })
            manifest["calibration_override"] = {
                "enabled": True,
                "policy": "strict_two_pass_consensus_user_override_v1",
                "enabled_categories": sorted(CALIBRATION_QUOTAS),
                "authorized_by": str(authorized_by)[:120],
                "reason": str(reason)[:2_000],
                "authorized_at": now,
                "calibration_run_id": calibration_run_id,
                "sample_count": int(calibration.get("sample_count") or 0),
                "labelled_count": int(calibration.get("labelled_count") or 0),
                "all_labelled_samples_correct": True,
                "critical_errors": sum(
                    int(category.get("critical_errors") or 0)
                    for category in (calibration.get("categories") or {}).values()
                ),
                "category_snapshot": calibration.get("categories") or {},
            }
            manifest["status"] = "ready_to_admit"
            manifest["active_phase"] = None
            manifest["updated_at"] = now
            _write_json(self._manifest_path(run_id), manifest)
            return self.get_run(run_id)

    @staticmethod
    def _strict_consensus_relation_prediction(
        candidate: dict[str, Any],
        reports: dict[tuple[str, int], dict[str, Any]],
    ) -> str:
        candidate_id = str(candidate.get("candidate_id") or "")
        first, second = reports.get((candidate_id, 1)), reports.get((candidate_id, 2))
        if (
            first and second
            and first.get("verdict") == "recommend_approve"
            and second.get("verdict") == "recommend_approve"
            and first.get("audit_eligible")
            and second.get("audit_eligible")
        ):
            return "approve"
        if first and second and all(report.get("verdict") == "recommend_reject" for report in (first, second)):
            return "reject"
        return "needs_human_review"

    @staticmethod
    def _node_index(nodes: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        index: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            name = _normalized_review_entity(node.get("name"))
            if name:
                index.setdefault(name, []).append(node)
        return index

    def admit_run(self, run_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(run_id)
        if manifest.get("mode") != "production" or manifest.get("status") != "ready_to_admit":
            raise ValueError("Only a completed production run can be admitted.")
        chunk_state = manifest.get("retry_chunk_state")
        if chunk_state and (
            chunk_state.get("remaining_custom_id_chunks")
            or chunk_state.get("failed_custom_ids")
        ):
            raise ValueError("Automation run still has unconsumed retry chunks; admission is blocked.")
        gates = self._calibration_gates(manifest.get("calibration_run_id"))
        calibration_override = manifest.get("calibration_override") or {}
        strict_consensus_override = (
            calibration_override.get("enabled") is True
            and calibration_override.get("policy") == "strict_two_pass_consensus_user_override_v1"
        )
        if strict_consensus_override:
            enabled_categories = set(calibration_override.get("enabled_categories") or [])
            gates = {
                category: bool(gates.get(category)) or category in enabled_categories
                for category in CALIBRATION_QUOTAS
            }
        relation_reports, entity_reports = self._run_report_maps(run_id)
        documents = self._documents()
        relation_ids = set(str(value) for value in manifest.get("relation_candidate_ids", []))
        entity_ids = set(str(value) for value in manifest.get("entity_candidate_ids", []))
        with _REVIEW_WRITE_LOCK:
            relation_candidates = _read_jsonl(self.repository.review_candidates_path)
            entity_candidates = _read_jsonl(self.repository.entity_candidates_path)
            approved_nodes = _read_jsonl(self.repository.approved_entity_nodes_path)
            approved_edges = _read_jsonl(self.repository.reviewed_edges_path)
            events = _read_jsonl(self.decision_events_path)
            now = _utc_now()
            admission_attempt_id = "admission_" + sha256(
                f"{run_id}\x1f{now}\x1f{calibration_override.get('authorized_at') or ''}".encode("utf-8")
            ).hexdigest()[:20]
            counters: Counter[str] = Counter()

            # Entity decisions are applied first so relation mappings can use
            # newly admitted locations/events in the same transaction window.
            graph_nodes = list(self.repository.graph_nodes().values())
            node_index = self._node_index(graph_nodes)
            for candidate in entity_candidates:
                candidate_id = str(candidate.get("entity_candidate_id") or "")
                if candidate_id not in entity_ids or candidate.get("review_status") != "pending_review":
                    continue
                before = dict(candidate)
                prediction = self._entity_prediction(candidate, entity_reports)
                if prediction == "approve" and gates["entity_approve"]:
                    node_type = str(candidate.get("proposed_node_type") or "")
                    entity_name = str(candidate.get("entity_name") or "").strip()
                    node_id = _review_node_id(node_type, entity_name)
                    existing = node_index.get(_normalized_review_entity(entity_name), [])
                    if node_type in {"location", "event"} and not existing and node_id == candidate.get("proposed_node_id"):
                        model_report_ids = [
                            str(report.get("report_id"))
                            for key, report in entity_reports.items()
                            if key[0] == candidate_id and report.get("report_id")
                        ]
                        node = {
                            "node_id": node_id,
                            "node_type": node_type,
                            "name": entity_name,
                            "attributes": {
                                "source": "qwen_batch_entity_review",
                                "entity_candidate_id": candidate_id,
                                "evidence_page_ids": list(candidate.get("evidence_page_ids") or []),
                                "source_types": list(candidate.get("source_types") or []),
                                "relation_candidate_ids": list(candidate.get("relation_candidate_ids") or []),
                                "automation_run_id": run_id,
                                "model_report_ids": model_report_ids,
                            },
                            "confidence": "model_approved_audited",
                            "model": QWEN_MODEL,
                            "review_status": "verified",
                            "automation_run_id": run_id,
                            "review_policy_version": AUTOMATION_POLICY_VERSION,
                            "model_report_ids": model_report_ids,
                            "created_at": now,
                        }
                        approved_nodes.append(node)
                        graph_nodes.append(node)
                        node_index.setdefault(_normalized_review_entity(entity_name), []).append(node)
                        candidate.update({
                            "review_status": "approved", "approved_node_id": node_id,
                            "automation_run_id": run_id,
                            "decision_source": (
                                "model_policy_user_calibration_override"
                                if strict_consensus_override else "model_policy"
                            ),
                            "reviewed_at": now,
                        })
                        counters["entities_approved"] += 1
                    else:
                        candidate.update({"review_status": "needs_human_review", "automation_run_id": run_id, "decision_source": "mapping_guard", "reviewed_at": now})
                        counters["entities_needing_human"] += 1
                elif prediction == "reject" and gates["entity_reject"]:
                    candidate.update({
                        "review_status": "rejected",
                        "automation_run_id": run_id,
                        "decision_source": (
                            "model_policy_user_calibration_override"
                            if strict_consensus_override else "model_policy"
                        ),
                        "reviewed_at": now,
                    })
                    counters["entities_rejected"] += 1
                else:
                    candidate.update({
                        "review_status": "needs_human_review",
                        "automation_run_id": run_id,
                        "decision_source": (
                            "model_policy_user_calibration_override"
                            if strict_consensus_override else "model_policy"
                        ),
                        "reviewed_at": now,
                    })
                    counters["entities_needing_human"] += 1
                events.append({"run_id": run_id, "admission_attempt_id": admission_attempt_id, "kind": "entity", "candidate_id": candidate_id, "before": before, "after_status": candidate["review_status"], "created_at": now})

            node_index = self._node_index(graph_nodes)
            eligible: list[tuple[dict[str, Any], str, str, str, list[str], list[str]]] = []
            relation_outcomes: dict[str, str] = {}
            relation_before: dict[str, dict[str, Any]] = {}
            for candidate in relation_candidates:
                candidate_id = str(candidate.get("candidate_id") or "")
                if candidate_id not in relation_ids or candidate.get("review_status") != "pending_review":
                    continue
                relation_before[candidate_id] = dict(candidate)
                payload, _ = _build_review_input(candidate, documents)
                deterministic_reason = _preflight_rejection_reason(candidate, payload, documents)
                if deterministic_reason:
                    relation_outcomes[candidate_id] = "rejected"
                    candidate["automation_reason"] = deterministic_reason
                    continue
                prediction = (
                    self._strict_consensus_relation_prediction(candidate, relation_reports)
                    if strict_consensus_override
                    else self._relation_prediction(candidate, relation_reports)
                )
                high = str(candidate.get("relation_type") or "").upper() in HIGH_IMPACT_RELATIONS
                approve_gate = gates["relation_approve_high" if high else "relation_approve_other"]
                if prediction == "reject" and gates["relation_reject"]:
                    relation_outcomes[candidate_id] = "rejected"
                    continue
                if prediction != "approve" or not approve_gate:
                    relation_outcomes[candidate_id] = "needs_human_review"
                    continue
                relation_type = str(candidate.get("relation_type") or "").upper()
                subjects = [node for node in node_index.get(_normalized_review_entity(candidate.get("subject")), []) if node.get("node_type") in _ACTOR_NODE_TYPES]
                targets = [node for node in node_index.get(_normalized_review_entity(candidate.get("object")), []) if node.get("node_type") in _object_endpoint_node_types(relation_type)]
                reports = [report for key, report in relation_reports.items() if key[0] == candidate_id]
                if len(subjects) != 1 or len(targets) != 1 or any(report.get("input_evidence_truncated") or report.get("risk_flags") or report.get("validation_flags") for report in reports):
                    relation_outcomes[candidate_id] = "needs_human_review"
                    continue
                source_types = list(dict.fromkeys(
                    str(documents[document_id].get("source_type") or candidate.get("source_type") or "")
                    for document_id in candidate.get("evidence_document_ids", [])
                    if document_id in documents
                ))
                page_ids = list(dict.fromkeys(
                    str(documents[document_id].get("page_id") or "")
                    for document_id in candidate.get("evidence_document_ids", [])
                    if document_id in documents and documents[document_id].get("page_id")
                ))
                if not page_ids:
                    relation_outcomes[candidate_id] = "needs_human_review"
                    continue
                scope = narrative_scope(relation_type, source_types)
                eligible.append((candidate, str(subjects[0]["node_id"]), str(targets[0]["node_id"]), scope, source_types, page_ids))

            existing_keys = {
                (str(edge.get("from_id")), str(edge.get("relation_type")), str(edge.get("to_id")), str(edge.get("narrative_scope") or "unknown"))
                for edge in list(self.repository.graph_edges()) + approved_edges
            }
            grouped: dict[tuple[str, str, str, str], list[tuple[dict[str, Any], list[str], list[str]]]] = {}
            for candidate, from_id, to_id, scope, source_types, page_ids in eligible:
                key = (from_id, str(candidate.get("relation_type") or "").upper(), to_id, scope)
                grouped.setdefault(key, []).append((candidate, source_types, page_ids))
            for key, rows in grouped.items():
                rows.sort(key=lambda item: str(item[0].get("candidate_id") or ""))
                candidate_ids = [str(item[0]["candidate_id"]) for item in rows]
                if key in existing_keys:
                    for candidate_id in candidate_ids:
                        relation_outcomes[candidate_id] = "superseded"
                    continue
                primary = rows[0][0]
                all_source_types = list(dict.fromkeys(value for _, source_types, _ in rows for value in source_types if value))
                all_page_ids = list(dict.fromkeys(value for _, _, page_ids in rows for value in page_ids if value))
                edge_id = "edge_model_" + sha256((run_id + "\x1f" + "\x1f".join(key)).encode("utf-8")).hexdigest()[:16]
                approved_edges.append({
                    "edge_id": edge_id,
                    "from_id": key[0],
                    "relation_type": key[1],
                    "to_id": key[2],
                    "narrative_scope": key[3],
                    "evidence_page_ids": all_page_ids,
                    "source_types": all_source_types,
                    "source_manifests": ["qwen_batch_relation_review"],
                    "confidence": "model_approved_audited",
                    "model": QWEN_MODEL,
                    "review_status": "verified",
                    "candidate_id": str(primary.get("candidate_id")),
                    "candidate_ids": candidate_ids,
                    "model_report_ids": sorted(
                        {
                            str(report.get("report_id"))
                            for candidate_id in candidate_ids
                            for key, report in relation_reports.items()
                            if key[0] == candidate_id and report.get("report_id")
                        }
                    ),
                    "automation_run_id": run_id,
                    "review_policy_version": AUTOMATION_POLICY_VERSION,
                    "created_at": now,
                })
                relation_outcomes[candidate_ids[0]] = "approved"
                for candidate_id in candidate_ids[1:]:
                    relation_outcomes[candidate_id] = "superseded"
                existing_keys.add(key)

            for candidate in relation_candidates:
                candidate_id = str(candidate.get("candidate_id") or "")
                status_value = relation_outcomes.get(candidate_id)
                if not status_value:
                    continue
                candidate.update({
                    "review_status": status_value,
                    "automation_run_id": run_id,
                    "decision_source": (
                        "deterministic_policy" if candidate.get("automation_reason")
                        else "model_policy_user_calibration_override" if strict_consensus_override
                        else "model_policy"
                    ),
                    "reviewed_at": now,
                })
                counters[f"relations_{status_value}"] += 1
                events.append({"run_id": run_id, "admission_attempt_id": admission_attempt_id, "kind": "relation", "candidate_id": candidate_id, "before": relation_before[candidate_id], "after_status": status_value, "created_at": now})

            manifest["rollback_guard"] = {
                "nodes": {
                    str(node.get("node_id")): _stable_hash(node)
                    for node in approved_nodes
                    if node.get("automation_run_id") == run_id and node.get("node_id")
                },
                "edges": {
                    str(edge.get("edge_id")): _stable_hash(edge)
                    for edge in approved_edges
                    if edge.get("automation_run_id") == run_id and edge.get("edge_id")
                },
            }
            _write_jsonl(self.repository.entity_candidates_path, entity_candidates)
            _write_jsonl(self.repository.approved_entity_nodes_path, approved_nodes)
            _write_jsonl(self.repository.review_candidates_path, relation_candidates)
            _write_jsonl(self.repository.reviewed_edges_path, approved_edges)
            _write_jsonl(self.decision_events_path, events)
            self.repository.clear_caches()
            manifest["status"] = "admitted"
            manifest["admitted_at"] = now
            manifest["admission_attempt_id"] = admission_attempt_id
            manifest["admission_counts"] = dict(sorted(counters.items()))
            manifest["calibration_gates"] = gates
            manifest["admission_policy"] = (
                "strict_two_pass_consensus_user_override_v1"
                if strict_consensus_override
                else "calibrated_default_v1"
            )
            manifest["updated_at"] = now
            _write_json(self._manifest_path(run_id), manifest)
            return manifest

    def rollback_run(self, run_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(run_id)
        if manifest.get("status") != "admitted":
            raise ValueError("Only an admitted automation run can be rolled back.")
        with _REVIEW_WRITE_LOCK:
            admission_attempt_id = str(manifest.get("admission_attempt_id") or "")
            run_events = [
                row for row in _read_jsonl(self.decision_events_path)
                if row.get("run_id") == run_id
            ]
            if admission_attempt_id:
                events = [
                    row for row in run_events
                    if row.get("admission_attempt_id") == admission_attempt_id
                ]
            elif manifest.get("admitted_at"):
                # Compatibility for admissions created before attempt IDs were
                # introduced. Repeated admission uses one shared timestamp per
                # attempt, so select only the most recent transaction.
                events = [
                    row for row in run_events
                    if row.get("created_at") == manifest.get("admitted_at")
                ]
            else:
                events = run_events
            relation_candidates = _read_jsonl(self.repository.review_candidates_path)
            entity_candidates = _read_jsonl(self.repository.entity_candidates_path)
            relation_index = {str(row.get("candidate_id")): row for row in relation_candidates}
            entity_index = {str(row.get("entity_candidate_id")): row for row in entity_candidates}
            conflicts: list[str] = []
            for event in events:
                index = relation_index if event.get("kind") == "relation" else entity_index
                current = index.get(str(event.get("candidate_id")))
                if (
                    current is None
                    or current.get("automation_run_id") != run_id
                    or current.get("review_status") != event.get("after_status")
                    or current.get("reviewed_at") != event.get("created_at")
                ):
                    conflicts.append(str(event.get("candidate_id")))
            if conflicts:
                raise ValueError(
                    "Rollback refused because candidates were changed after automation: " + ", ".join(conflicts[:20])
                )
            approved_edges_all = _read_jsonl(self.repository.reviewed_edges_path)
            approved_nodes_all = _read_jsonl(self.repository.approved_entity_nodes_path)
            guard = manifest.get("rollback_guard") or {}
            current_edge_hashes = {
                str(row.get("edge_id")): _stable_hash(row)
                for row in approved_edges_all
                if row.get("automation_run_id") == run_id and row.get("edge_id")
            }
            current_node_hashes = {
                str(row.get("node_id")): _stable_hash(row)
                for row in approved_nodes_all
                if row.get("automation_run_id") == run_id and row.get("node_id")
            }
            artifact_conflicts = [
                f"node:{identifier}"
                for identifier, expected_hash in (guard.get("nodes") or {}).items()
                if current_node_hashes.get(identifier) != expected_hash
            ] + [
                f"edge:{identifier}"
                for identifier, expected_hash in (guard.get("edges") or {}).items()
                if current_edge_hashes.get(identifier) != expected_hash
            ]
            artifact_conflicts.extend(
                f"node:{identifier}"
                for identifier in sorted(set(current_node_hashes) - set(guard.get("nodes") or {}))
            )
            artifact_conflicts.extend(
                f"edge:{identifier}"
                for identifier in sorted(set(current_edge_hashes) - set(guard.get("edges") or {}))
            )
            if artifact_conflicts:
                raise ValueError(
                    "Rollback refused because graph artifacts were changed after automation: "
                    + ", ".join(artifact_conflicts[:20])
                )
            for event in events:
                before = dict(event.get("before") or {})
                if event.get("kind") == "relation":
                    relation_index[str(event["candidate_id"])] = before
                else:
                    entity_index[str(event["candidate_id"])] = before
            restored_relations = [relation_index[str(row.get("candidate_id"))] for row in relation_candidates]
            restored_entities = [entity_index[str(row.get("entity_candidate_id"))] for row in entity_candidates]
            approved_edges = [row for row in approved_edges_all if row.get("automation_run_id") != run_id]
            approved_nodes = [row for row in approved_nodes_all if row.get("automation_run_id") != run_id]
            _write_jsonl(self.repository.review_candidates_path, restored_relations)
            _write_jsonl(self.repository.entity_candidates_path, restored_entities)
            _write_jsonl(self.repository.reviewed_edges_path, approved_edges)
            _write_jsonl(self.repository.approved_entity_nodes_path, approved_nodes)
            self.repository.clear_caches()
            manifest["status"] = "rolled_back"
            manifest["rolled_back_at"] = _utc_now()
            manifest["updated_at"] = manifest["rolled_back_at"]
            _write_json(self._manifest_path(run_id), manifest)
            return manifest
