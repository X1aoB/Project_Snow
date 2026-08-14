"""High-coverage DeepSeek completion for unresolved review candidates.

This pipeline is deliberately separate from the calibrated Qwen Batch run. It
selects only candidates that are still ``needs_human_review``, asks DeepSeek for
a forced approve/reject decision, and converts every provider or mapping failure
into an audited rejection instead of growing another manual queue.

Provider credentials are resolved from the application's credential vault and
are never written to manifests, request snapshots, reports, or decision events.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipelines.review_relation_candidates import _build_review_input

from .agent_store import AgentStore
from .config import Settings
from .graph_metadata import narrative_scope
from .provider_registry import ProviderRegistry
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


DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_PROVIDER_ID = "deepseek"
COMPLETION_POLICY_VERSION = "deepseek-v4-pro-high-coverage-v1"
MINIMUM_FINAL_COVERAGE = 0.995
FINAL_CANDIDATE_STATUSES = {"approved", "rejected", "superseded"}
RUN_TERMINAL_STATUSES = {"ready_to_admit", "admitted", "rolled_back", "failed"}
ALLOWED_CREATED_NODE_TYPES = {
    "character", "sender", "enemy", "event", "location", "item", "weapon",
    "weapon_attachment", "furniture", "costume", "armor", "logistics_squad",
}
HIGH_IMPACT_RELATIONS = {"ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT"}
_RUN_LOCK = threading.RLock()


RELATION_SYSTEM_PROMPT = """你是《尘白禁区》知识图谱的最终补审员。任务目标是消除人工待审队列，但不能把明显错误事实写入图谱。

必须对当前候选强制二选一：approve 或 reject，绝不输出 abstain。只判断输入中的这一条候选，不能用输入外知识补造事实。

判定原则已经放宽：只要给出的原文语境能够合理、明确地支持主体与客体之间的候选关系，即可 approve；不要求一句引文同时逐字包含所有端点，也不因叙事是临时场景而自动拒绝。若关系只是提及、主客体认错、候选关系与原文相反、或原文没有合理支持，则 reject。低置信度时也必须选择更可能的一项。

批准时还要完成端点映射：
1. supplied_node_options 中存在同一实体时用 use_existing，并原样返回 node_id、node_type、name。
2. 没有合适节点但它是明确命名实体时用 create。主体只能创建 character/sender/enemy；客体类型必须符合 allowed_object_node_types。
3. 若端点无法可靠映射或创建，则 reject。
4. supporting_quote 必须是 evidence 中原文的逐字连续片段。

仅输出一个 JSON 对象，字段固定为：
{"candidate_id":"...","decision":"approve|reject","confidence":0.0,"supporting_quote":"...","subject_endpoint":{"action":"use_existing|create|none","node_id":"","node_type":"character|sender|enemy|event|location|item|weapon|weapon_attachment|furniture|costume|armor|logistics_squad","name":"..."},"object_endpoint":{"action":"use_existing|create|none","node_id":"","node_type":"character|sender|enemy|event|location|item|weapon|weapon_attachment|furniture|costume|armor|logistics_squad","name":"..."},"reason":"不超过120字"}
"""


ENTITY_SYSTEM_PROMPT = """你是《尘白禁区》知识图谱的最终实体补审员。必须对当前地点或事件候选强制二选一：approve 或 reject，绝不输出 abstain。

判定条件已放宽：原文能够把名称合理当作可复用地点或可识别剧情事件即可 approve；普通动作、整句剧情概述、页面栏目名、代词或没有明确所指的泛称应 reject。低置信度时也必须选择更可能的一项。supporting_quote 必须是 evidence 中原文的逐字连续片段；canonical_name 应保留原文实体名称，node_type 只能是输入建议的 event 或 location。

仅输出一个 JSON 对象，字段固定为：
{"entity_candidate_id":"...","decision":"approve|reject","confidence":0.0,"canonical_name":"...","node_type":"event|location","supporting_quote":"...","reason":"不超过120字"}
"""


class EndpointDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["use_existing", "create", "none"]
    node_id: str = ""
    node_type: Literal[
        "character", "sender", "enemy", "event", "location", "item", "weapon",
        "weapon_attachment", "furniture", "costume", "armor", "logistics_squad",
    ] = "character"
    name: str = Field(default="", max_length=240)


class RelationCompletionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_id: str = Field(min_length=1, max_length=240)
    decision: Literal["approve", "reject"]
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_quote: str = Field(default="", max_length=12_000)
    subject_endpoint: EndpointDecision
    object_endpoint: EndpointDecision
    reason: str = Field(default="", max_length=2_000)


class EntityCompletionDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    entity_candidate_id: str = Field(min_length=1, max_length=240)
    decision: Literal["approve", "reject"]
    confidence: float = Field(ge=0.0, le=1.0)
    canonical_name: str = Field(default="", max_length=240)
    node_type: Literal["event", "location"]
    supporting_quote: str = Field(default="", max_length=12_000)
    reason: str = Field(default="", max_length=2_000)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


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


def _bounded_text(value: Any, maximum: int = 2_000) -> str:
    return str(value or "").strip().replace("\x00", "")[:maximum]


def _quote_in_payload(quote: str, payload: dict[str, Any]) -> bool:
    if not quote.strip():
        return False
    return any(quote in str(item.get("text") or "") for item in payload.get("evidence", []))


def _valid_new_node_name(value: Any) -> bool:
    name = _bounded_text(value, 240)
    normalized = _normalized_review_entity(name)
    invalid = {
        "", "我", "我们", "你", "你们", "他", "她", "它", "他们", "她们", "它们",
        "这里", "那里", "某处", "有人", "大家", "对方", "敌人", "角色", "地点", "事件",
    }
    return normalized not in invalid and 1 <= len(normalized) <= 80


def _valid_created_actor_name(value: Any) -> bool:
    """Reject organizations/factions that the current graph cannot type safely."""

    name = _bounded_text(value, 240)
    if not _valid_new_node_name(name):
        return False
    organization_markers = (
        "公司", "集团", "组织", "机构", "委员会", "董事会", "部队", "小队", "军团",
        "家族", "一族", "阵营", "联盟", "部门", "研究所", "实验室", "世界树",
    )
    if any(marker in name for marker in organization_markers):
        return False
    # Family/collective labels such as “安德烈奥蒂家” are not characters.
    if name.endswith(("家", "方", "众", "们")):
        return False
    return True


CompletionCallable = Callable[[str, str, dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]


class DeepSeekReviewCompletionService:
    """Run, admit, and roll back a forced-decision DeepSeek review."""

    def __init__(
        self,
        settings: Settings,
        repository: RuntimeRepository,
        *,
        registry: ProviderRegistry | None = None,
        completion: CompletionCallable | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.store = AgentStore(settings.runtime_root / "chat" / "agent.sqlite3")
        self.registry = registry or ProviderRegistry(self.store)
        self.completion = completion
        self.root = settings.runtime_root / "review" / "automation" / "deepseek-runs"
        self.decision_events_path = self.root / "decision-events.jsonl"
        self._threads: dict[str, threading.Thread] = {}

    def _run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"deepseek_review_run_[A-Za-z0-9_]+", str(run_id or "")):
            raise KeyError(run_id)
        return self.root / run_id

    def _manifest_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "manifest.json"

    def _load_manifest(self, run_id: str) -> dict[str, Any]:
        path = self._manifest_path(run_id)
        if not path.exists():
            raise KeyError(run_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid DeepSeek run manifest.")
        return value

    def _save_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = _utc_now()
        _write_json(self._manifest_path(str(manifest["run_id"])), manifest)

    def _current_selection(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        relations = [
            row for row in _read_jsonl(self.repository.review_candidates_path)
            if row.get("review_status") == "needs_human_review"
        ]
        entities = [
            row for row in _read_jsonl(self.repository.entity_candidates_path)
            if row.get("review_status") == "needs_human_review"
        ]
        relations.sort(key=lambda row: str(row.get("candidate_id") or ""))
        entities.sort(key=lambda row: str(row.get("entity_candidate_id") or ""))
        return relations, entities

    def _provider_selection(self) -> tuple[Any, str]:
        selection = self.registry.route(
            {"text"},
            {"provider_id": DEEPSEEK_PROVIDER_ID, "model_name": DEEPSEEK_MODEL},
            {"text"},
            profile="assistant_agent",
        )
        credential = self.registry.credential_for_selection(selection)
        if not credential:
            raise RuntimeError("DeepSeek credential is unavailable from the credential vault.")
        return selection, credential

    def estimate(self) -> dict[str, Any]:
        relations, entities = self._current_selection()
        total = len(relations) + len(entities)
        return {
            "model": DEEPSEEK_MODEL,
            "provider_id": DEEPSEEK_PROVIDER_ID,
            "policy_version": COMPLETION_POLICY_VERSION,
            "relation_count": len(relations),
            "entity_count": len(entities),
            "request_count": total,
            "minimum_final_coverage": MINIMUM_FINAL_COVERAGE,
            "maximum_allowed_unresolved": int(total * (1.0 - MINIMUM_FINAL_COVERAGE)),
            "price_gate_enabled": False,
            "selection_hash": _stable_hash({
                "relations": [(row.get("candidate_id"), _stable_hash(row)) for row in relations],
                "entities": [(row.get("entity_candidate_id"), _stable_hash(row)) for row in entities],
            }),
        }

    def create_run(self, expected_selection_hash: str | None = None) -> dict[str, Any]:
        estimate = self.estimate()
        if expected_selection_hash and expected_selection_hash != estimate["selection_hash"]:
            raise ValueError("Unresolved candidate selection changed; refresh the estimate.")
        relations, entities = self._current_selection()
        if not relations and not entities:
            raise ValueError("There are no needs_human_review candidates to complete.")
        now = _utc_now()
        run_id = "deepseek_review_run_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ_") + sha256(
            f"{now}\x1f{estimate['selection_hash']}".encode("utf-8")
        ).hexdigest()[:10]
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        selection_rows = [
            {"kind": "entity", "candidate_id": str(row["entity_candidate_id"]), "before": row, "before_hash": _stable_hash(row)}
            for row in entities
        ] + [
            {"kind": "relation", "candidate_id": str(row["candidate_id"]), "before": row, "before_hash": _stable_hash(row)}
            for row in relations
        ]
        _write_jsonl(run_dir / "selection.jsonl", selection_rows)
        qwen_runs = Counter(
            str(row.get("automation_run_id") or "") for row in relations + entities if row.get("automation_run_id")
        )
        manifest = {
            "run_id": run_id,
            "status": "created",
            "active_phase": None,
            "created_at": now,
            "updated_at": now,
            "provider_id": DEEPSEEK_PROVIDER_ID,
            "model": DEEPSEEK_MODEL,
            "policy_version": COMPLETION_POLICY_VERSION,
            "selection_hash": estimate["selection_hash"],
            "relation_count": len(relations),
            "entity_count": len(entities),
            "request_count": len(selection_rows),
            "source_qwen_run_id": qwen_runs.most_common(1)[0][0] if qwen_runs else None,
            "minimum_final_coverage": MINIMUM_FINAL_COVERAGE,
            "progress": {"completed": 0, "provider_failed_default_reject": 0, "total": len(selection_rows)},
            "actual_usage": {},
            "price_gate_enabled": False,
        }
        self._save_manifest(manifest)
        return self.get_run(run_id)

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        runs: list[dict[str, Any]] = []
        for path in self.root.glob("deepseek_review_run_*/manifest.json"):
            try:
                runs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(runs, key=lambda row: str(row.get("created_at") or ""), reverse=True)

    def get_run(self, run_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(run_id)
        reports = _read_jsonl(self._run_dir(run_id) / "reports.jsonl")
        decisions = Counter(str(row.get("decision") or "unknown") for row in reports)
        manifest["report_summary"] = {
            "report_count": len(reports),
            "approve": decisions["approve"],
            "reject": decisions["reject"],
            "provider_failed_default_reject": sum(bool(row.get("provider_failed_default_reject")) for row in reports),
        }
        return manifest

    def _entity_payload(self, candidate: dict[str, Any], documents: dict[str, dict[str, Any]]) -> dict[str, Any]:
        name = str(candidate.get("entity_name") or "")
        evidence: list[dict[str, Any]] = []
        remaining = 8_000
        for document_id in candidate.get("evidence_document_ids", []):
            document = documents.get(str(document_id))
            if document is None or remaining <= 0:
                continue
            text = str(document.get("text") or "")
            position = text.find(name)
            if position < 0:
                continue
            start = max(0, position - 1_200)
            excerpt = text[start : start + min(2_800, remaining)]
            remaining -= len(excerpt)
            evidence.append({
                "document_id": document_id,
                "page_id": document.get("page_id"),
                "title": document.get("title"),
                "source_type": document.get("source_type"),
                "text": excerpt,
            })
        return {
            "policy_version": COMPLETION_POLICY_VERSION,
            "candidate": {
                "entity_candidate_id": candidate.get("entity_candidate_id"),
                "entity_name": name,
                "proposed_node_type": candidate.get("proposed_node_type"),
                "source_types": candidate.get("source_types", []),
                "evidence_examples": candidate.get("evidence_examples", [])[:12],
            },
            "evidence": evidence,
        }

    @staticmethod
    def _node_options(name: Any, nodes: Iterable[dict[str, Any]], allowed_types: set[str], limit: int = 12) -> list[dict[str, Any]]:
        query = _normalized_review_entity(name)
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for node in nodes:
            if str(node.get("node_type") or "") not in allowed_types or not node.get("node_id"):
                continue
            node_name = _normalized_review_entity(node.get("name"))
            if not query or not node_name:
                continue
            if query == node_name:
                score = 10.0
            elif query.startswith(node_name) or query.endswith(node_name) or node_name.startswith(query) or node_name.endswith(query):
                score = 5.0 + min(len(query), len(node_name)) / max(len(query), len(node_name))
            else:
                score = difflib.SequenceMatcher(None, query, node_name).ratio()
            if score >= 0.34:
                ranked.append((score, str(node.get("node_id")), node))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [
            {"node_id": node.get("node_id"), "node_type": node.get("node_type"), "name": node.get("name")}
            for _, _, node in ranked[:limit]
        ]

    def _relation_payload(
        self,
        candidate: dict[str, Any],
        documents: dict[str, dict[str, Any]],
        nodes: list[dict[str, Any]],
        prospective_entities: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        base, _ = _build_review_input(candidate, documents)
        relation_type = str(candidate.get("relation_type") or "").upper()
        subject_options = self._node_options(candidate.get("subject"), nodes, set(_ACTOR_NODE_TYPES))
        object_options = self._node_options(candidate.get("object"), nodes, _object_endpoint_node_types(relation_type))
        prospective = prospective_entities.get(_normalized_review_entity(candidate.get("object")))
        if prospective and prospective.get("decision") == "approve":
            candidate_node = {
                "node_id": prospective.get("proposed_node_id"),
                "node_type": prospective.get("node_type"),
                "name": prospective.get("canonical_name"),
                "prospective_deepseek_entity": True,
            }
            if candidate_node["node_type"] in _object_endpoint_node_types(relation_type):
                object_options.insert(0, candidate_node)
        return {
            **base,
            "policy_version": COMPLETION_POLICY_VERSION,
            "allowed_subject_node_types": sorted(_ACTOR_NODE_TYPES),
            "allowed_object_node_types": sorted(_object_endpoint_node_types(relation_type)),
            "supplied_node_options": {"subject": subject_options, "object": object_options[:13]},
        }

    def _provider_call(
        self,
        selection: Any,
        credential: str,
        system_prompt: str,
        payload: dict[str, Any],
        correction: str = "",
        *,
        thinking_enabled: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.completion is not None:
            return self.completion("entity" if "entity_candidate_id" in payload.get("candidate", {}) else "relation", system_prompt, payload)
        user_content = json.dumps(payload, ensure_ascii=False)
        if correction:
            user_content += "\n\n上一次输出无效。" + correction + "。请只返回修正后的 JSON。"
        response = httpx.post(
            selection.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
            json={
                "model": selection.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                # DeepSeek thinking tokens share this output budget.  A 1,200
                # token ceiling can finish the reasoning trace before the JSON
                # answer is emitted for long narrative evidence.
                "max_tokens": 3_000,
                "response_format": {"type": "json_object"},
                **self.registry.thinking_request_fields(
                    selection.provider_kind, "on" if thinking_enabled else "off"
                ),
            },
            timeout=180,
            follow_redirects=True,
        )
        response.raise_for_status()
        body = response.json()
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content"))
        return _extract_json_object(content), dict(body.get("usage") or {})

    def _review_one(
        self,
        kind: Literal["entity", "relation"],
        candidate: dict[str, Any],
        payload: dict[str, Any],
        selection: Any,
        credential: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        identifier = str(candidate.get("entity_candidate_id") or candidate.get("candidate_id") or "")
        system_prompt = ENTITY_SYSTEM_PROMPT if kind == "entity" else RELATION_SYSTEM_PROMPT
        errors: list[str] = []
        usage_total = Counter()
        correction = ""
        for attempt in range(1, 6):
            try:
                relation_type = str((payload.get("candidate") or {}).get("relation_type") or "").upper()
                # Keep full reasoning for identity-sensitive actor relations.
                # Event/location/item relations are substantially more literal;
                # structured non-thinking output is faster and less likely to
                # spend the entire output budget before emitting JSON. Repair
                # attempts always disable thinking to prioritize valid JSON.
                thinking_enabled = kind == "entity" or (attempt == 1 and relation_type in HIGH_IMPACT_RELATIONS)
                raw, usage = self._provider_call(
                    selection,
                    credential,
                    system_prompt,
                    payload,
                    correction,
                    thinking_enabled=thinking_enabled,
                )
                for key, value in usage.items():
                    if isinstance(value, (int, float)):
                        usage_total[str(key)] += value
                if kind == "entity":
                    parsed = EntityCompletionDecision.model_validate(raw)
                    if parsed.entity_candidate_id != identifier:
                        raise ValueError("entity_candidate_id does not match the request")
                    decision = parsed.model_dump()
                    if parsed.node_type != str(candidate.get("proposed_node_type") or ""):
                        decision["decision"] = "reject"
                        decision["local_override_reason"] = "node_type_mismatch"
                    if parsed.decision == "approve" and not _quote_in_payload(parsed.supporting_quote, payload):
                        fallback = str((candidate.get("evidence_examples") or [{}])[0].get("evidence_quote") or "")
                        if _quote_in_payload(fallback, payload):
                            decision["supporting_quote"] = fallback
                            decision["quote_repaired_from_candidate"] = True
                        else:
                            decision["decision"] = "reject"
                            decision["local_override_reason"] = "supporting_quote_not_found"
                    if decision["decision"] == "approve" and not _valid_new_node_name(decision.get("canonical_name")):
                        decision["decision"] = "reject"
                        decision["local_override_reason"] = "invalid_canonical_name"
                else:
                    parsed = RelationCompletionDecision.model_validate(raw)
                    if parsed.candidate_id != identifier:
                        raise ValueError("candidate_id does not match the request")
                    decision = parsed.model_dump()
                    if parsed.decision == "approve" and not _quote_in_payload(parsed.supporting_quote, payload):
                        fallback = str(candidate.get("evidence_quote") or "")
                        if _quote_in_payload(fallback, payload):
                            decision["supporting_quote"] = fallback
                            decision["quote_repaired_from_candidate"] = True
                        else:
                            decision["decision"] = "reject"
                            decision["local_override_reason"] = "supporting_quote_not_found"
                report = {
                    **decision,
                    "kind": kind,
                    "report_id": "deepseek_report_" + sha256(
                        f"{identifier}\x1f{_stable_hash(payload)}\x1f{COMPLETION_POLICY_VERSION}".encode("utf-8")
                    ).hexdigest()[:20],
                    "input_hash": _stable_hash(payload),
                    "model": DEEPSEEK_MODEL,
                    "provider_id": DEEPSEEK_PROVIDER_ID,
                    "policy_version": COMPLETION_POLICY_VERSION,
                    "attempt_count": attempt,
                    "reviewed_at": _utc_now(),
                    "errors_before_success": errors,
                    "usage": dict(usage_total),
                }
                return report, {"kind": kind, "candidate_id": identifier, "input_hash": report["input_hash"], "payload": payload}
            except (httpx.HTTPError, ValueError, KeyError, IndexError, ValidationError, json.JSONDecodeError) as exc:
                message = f"{type(exc).__name__}: {_bounded_text(exc, 500)}"
                errors.append(message)
                correction = message
                if attempt < 5:
                    retry_after = 0.0
                    if isinstance(exc, httpx.HTTPStatusError):
                        try:
                            retry_after = float(exc.response.headers.get("Retry-After") or 0)
                        except ValueError:
                            retry_after = 0.0
                    time.sleep(min(max(retry_after, 0.5 * (2 ** (attempt - 1))), 8.0))
        if kind == "entity":
            decision = {
                "entity_candidate_id": identifier,
                "decision": "reject",
                "confidence": 0.0,
                "canonical_name": str(candidate.get("entity_name") or ""),
                "node_type": str(candidate.get("proposed_node_type") or "event"),
                "supporting_quote": "",
                "reason": "Provider unresolved after retries; defaulted to reject.",
            }
        else:
            decision = {
                "candidate_id": identifier,
                "decision": "reject",
                "confidence": 0.0,
                "supporting_quote": "",
                "subject_endpoint": {"action": "none", "node_id": "", "node_type": "character", "name": ""},
                "object_endpoint": {"action": "none", "node_id": "", "node_type": "character", "name": ""},
                "reason": "Provider unresolved after retries; defaulted to reject.",
            }
        report = {
            **decision,
            "kind": kind,
            "report_id": "deepseek_report_" + sha256(f"{identifier}\x1fdefault-reject".encode("utf-8")).hexdigest()[:20],
            "input_hash": _stable_hash(payload),
            "model": DEEPSEEK_MODEL,
            "provider_id": DEEPSEEK_PROVIDER_ID,
            "policy_version": COMPLETION_POLICY_VERSION,
            "attempt_count": 5,
            "reviewed_at": _utc_now(),
            "errors_before_success": errors,
            "provider_failed_default_reject": True,
            "usage": dict(usage_total),
        }
        return report, {"kind": kind, "candidate_id": identifier, "input_hash": report["input_hash"], "payload": payload}

    @staticmethod
    def _merge_artifacts(path: Path, rows: list[dict[str, Any]], key_name: str = "candidate_id") -> None:
        index = {(str(row.get("kind") or ""), str(row.get(key_name) or row.get("entity_candidate_id") or "")): row for row in _read_jsonl(path)}
        for row in rows:
            identifier = str(row.get(key_name) or row.get("entity_candidate_id") or "")
            index[(str(row.get("kind") or ""), identifier)] = row
        _write_jsonl(path, index.values())

    def _process_phase(
        self,
        manifest: dict[str, Any],
        kind: Literal["entity", "relation"],
        candidates: list[dict[str, Any]],
        payload_builder: Callable[[dict[str, Any]], dict[str, Any]],
        selection: Any,
        credential: str,
        concurrency: int,
    ) -> None:
        run_dir = self._run_dir(str(manifest["run_id"]))
        reports_path = run_dir / "reports.jsonl"
        requests_path = run_dir / "requests.jsonl"
        completed = {
            (str(row.get("kind") or ""), str(row.get("candidate_id") or row.get("entity_candidate_id") or ""))
            for row in _read_jsonl(reports_path)
            if not row.get("provider_failed_default_reject")
        }
        pending = [
            candidate for candidate in candidates
            if (kind, str(candidate.get("entity_candidate_id") or candidate.get("candidate_id") or "")) not in completed
        ]
        manifest["status"] = "running"
        manifest["active_phase"] = kind
        self._save_manifest(manifest)
        # Persist frequent checkpoints: at 20 concurrent reasoning requests a
        # 160-item chunk can look stalled for several minutes and loses too
        # much completed work if the desktop service restarts.
        chunk_size = max(25, min(128, concurrency * 2))
        for offset in range(0, len(pending), chunk_size):
            chunk = pending[offset : offset + chunk_size]
            outputs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=f"deepseek-{kind}") as executor:
                futures = {
                    executor.submit(
                        self._review_one, kind, candidate, payload_builder(candidate), selection, credential
                    ): candidate
                    for candidate in chunk
                }
                for future in as_completed(futures):
                    outputs.append(future.result())
            reports = [item[0] for item in outputs]
            requests = [item[1] for item in outputs]
            self._merge_artifacts(reports_path, reports)
            self._merge_artifacts(requests_path, requests)
            all_reports = _read_jsonl(reports_path)
            usage = Counter()
            for report in all_reports:
                for key, value in dict(report.get("usage") or {}).items():
                    if isinstance(value, (int, float)):
                        usage[key] += value
            manifest["progress"] = {
                "completed": len(all_reports),
                "provider_failed_default_reject": sum(bool(row.get("provider_failed_default_reject")) for row in all_reports),
                "total": int(manifest["request_count"]),
            }
            manifest["actual_usage"] = dict(usage)
            self._save_manifest(manifest)

    def run(self, run_id: str, concurrency: int = 12) -> dict[str, Any]:
        concurrency = max(1, min(int(concurrency), 128))
        with _RUN_LOCK:
            manifest = self._load_manifest(run_id)
            if manifest.get("status") in {"admitted", "rolled_back"}:
                raise ValueError("An admitted or rolled-back run cannot be executed again.")
            manifest["status"] = "running"
            manifest["last_error"] = None
            self._save_manifest(manifest)
        try:
            selection, credential = self._provider_selection()
            selection_rows = _read_jsonl(self._run_dir(run_id) / "selection.jsonl")
            entities = [dict(row["before"]) for row in selection_rows if row.get("kind") == "entity"]
            relations = [dict(row["before"]) for row in selection_rows if row.get("kind") == "relation"]
            documents = self.repository.documents_by_id()
            self._process_phase(
                manifest, "entity", entities,
                lambda candidate: self._entity_payload(candidate, documents),
                selection, credential, concurrency,
            )
            entity_reports = {
                str(row.get("entity_candidate_id") or ""): row
                for row in _read_jsonl(self._run_dir(run_id) / "reports.jsonl")
                if row.get("kind") == "entity"
            }
            entity_by_name: dict[str, dict[str, Any]] = {}
            entity_candidates = {str(row.get("entity_candidate_id") or ""): row for row in entities}
            for identifier, report in entity_reports.items():
                candidate = entity_candidates.get(identifier) or {}
                report["proposed_node_id"] = candidate.get("proposed_node_id")
                entity_by_name[_normalized_review_entity(candidate.get("entity_name"))] = report
            nodes = list(self.repository.graph_nodes().values())
            self._process_phase(
                manifest, "relation", relations,
                lambda candidate: self._relation_payload(candidate, documents, nodes, entity_by_name),
                selection, credential, concurrency,
            )
            reports = _read_jsonl(self._run_dir(run_id) / "reports.jsonl")
            total = int(manifest["request_count"])
            coverage = len(reports) / total if total else 1.0
            if coverage < MINIMUM_FINAL_COVERAGE:
                raise RuntimeError(f"Final model-decision coverage {coverage:.4%} is below {MINIMUM_FINAL_COVERAGE:.2%}.")
            manifest["status"] = "ready_to_admit"
            manifest["active_phase"] = None
            manifest["model_decision_coverage"] = coverage
            manifest["completed_at"] = _utc_now()
            self._save_manifest(manifest)
            return self.get_run(run_id)
        except Exception as exc:
            manifest = self._load_manifest(run_id)
            manifest["status"] = "failed"
            manifest["active_phase"] = None
            manifest["last_error"] = f"{type(exc).__name__}: {_bounded_text(exc, 2_000)}"
            self._save_manifest(manifest)
            raise

    def start(self, run_id: str, concurrency: int = 12) -> dict[str, Any]:
        with _RUN_LOCK:
            current = self._threads.get(run_id)
            if current and current.is_alive():
                return self.get_run(run_id)

            def target() -> None:
                try:
                    self.run(run_id, concurrency)
                except Exception:
                    return

            thread = threading.Thread(target=target, name=f"deepseek-review-{run_id[-10:]}", daemon=True)
            self._threads[run_id] = thread
            thread.start()
        return self.get_run(run_id)

    @staticmethod
    def _node_index(nodes: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            name = _normalized_review_entity(node.get("name"))
            if name:
                output.setdefault(name, []).append(node)
        return output

    @staticmethod
    def _alias_node(name: Any, nodes: list[dict[str, Any]], allowed_types: set[str]) -> dict[str, Any] | None:
        query = _normalized_review_entity(name)
        exact = [node for node in nodes if node.get("node_type") in allowed_types and _normalized_review_entity(node.get("name")) == query]
        if len(exact) == 1:
            return exact[0]
        contained: list[tuple[int, dict[str, Any]]] = []
        for node in nodes:
            if node.get("node_type") not in allowed_types:
                continue
            node_name = _normalized_review_entity(node.get("name"))
            if node_name and (query.startswith(node_name) or query.endswith(node_name)) and len(query) - len(node_name) <= 5:
                contained.append((len(node_name), node))
        if contained:
            maximum = max(length for length, _ in contained)
            winners = [node for length, node in contained if length == maximum]
            if len(winners) == 1:
                return winners[0]
        return None

    def _resolve_endpoint(
        self,
        endpoint: dict[str, Any],
        fallback_name: Any,
        allowed_types: set[str],
        nodes: list[dict[str, Any]],
        allowed_option_ids: set[str],
        run_id: str,
        source_qwen_run_id: str | None,
        created_nodes: list[dict[str, Any]],
        now: str,
    ) -> str | None:
        node_id = str(endpoint.get("node_id") or "")
        node_by_id = {str(node.get("node_id")): node for node in nodes if node.get("node_id")}
        if endpoint.get("action") == "use_existing" and node_id in node_by_id and node_id in allowed_option_ids:
            if str(node_by_id[node_id].get("node_type") or "") in allowed_types:
                return node_id
        proposed_name = endpoint.get("name") or fallback_name
        alias = self._alias_node(proposed_name, nodes, allowed_types) or self._alias_node(fallback_name, nodes, allowed_types)
        if alias is not None:
            return str(alias["node_id"])
        node_type = str(endpoint.get("node_type") or "")
        if endpoint.get("action") != "create" or node_type not in allowed_types or node_type not in ALLOWED_CREATED_NODE_TYPES:
            return None
        name = _bounded_text(proposed_name, 240)
        if not _valid_new_node_name(name) or (node_type in _ACTOR_NODE_TYPES and not _valid_created_actor_name(name)):
            return None
        new_node_id = _review_node_id(node_type, name)
        if new_node_id in node_by_id:
            return new_node_id
        node = {
            "node_id": new_node_id,
            "node_type": node_type,
            "name": name,
            "attributes": {
                "source": "deepseek_v4_pro_high_coverage_relation_review",
                "automation_run_id": run_id,
                "source_qwen_run_id": source_qwen_run_id,
            },
            "confidence": "model_approved_high_coverage",
            "model": DEEPSEEK_MODEL,
            "review_status": "verified",
            "automation_run_id": run_id,
            "deepseek_completion_run_id": run_id,
            "review_policy_version": COMPLETION_POLICY_VERSION,
            "created_at": now,
        }
        nodes.append(node)
        created_nodes.append(node)
        return new_node_id

    def admit(self, run_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(run_id)
        if manifest.get("status") != "ready_to_admit":
            raise ValueError("Only a ready_to_admit DeepSeek run can be admitted.")
        run_dir = self._run_dir(run_id)
        selections = _read_jsonl(run_dir / "selection.jsonl")
        reports = _read_jsonl(run_dir / "reports.jsonl")
        requests = _read_jsonl(run_dir / "requests.jsonl")
        report_index = {(str(row.get("kind")), str(row.get("entity_candidate_id") or row.get("candidate_id") or "")): row for row in reports}
        request_index = {(str(row.get("kind")), str(row.get("candidate_id") or "")): row for row in requests}
        if len(report_index) < int(manifest["request_count"]) * MINIMUM_FINAL_COVERAGE:
            raise ValueError("DeepSeek reports do not meet the required final-decision coverage.")
        with _REVIEW_WRITE_LOCK:
            relation_candidates = _read_jsonl(self.repository.review_candidates_path)
            entity_candidates = _read_jsonl(self.repository.entity_candidates_path)
            relation_index = {str(row.get("candidate_id") or ""): row for row in relation_candidates}
            entity_index = {str(row.get("entity_candidate_id") or ""): row for row in entity_candidates}
            changed: list[str] = []
            for selection in selections:
                index = entity_index if selection.get("kind") == "entity" else relation_index
                current = index.get(str(selection.get("candidate_id") or ""))
                if current is None or _stable_hash(current) != selection.get("before_hash") or current.get("review_status") != "needs_human_review":
                    changed.append(str(selection.get("candidate_id") or ""))
            if changed:
                raise ValueError("Admission refused because selected candidates changed after run creation: " + ", ".join(changed[:20]))

            now = _utc_now()
            source_qwen_run_id = manifest.get("source_qwen_run_id")
            approved_nodes = _read_jsonl(self.repository.approved_entity_nodes_path)
            approved_edges = _read_jsonl(self.repository.reviewed_edges_path)
            all_nodes = list(self.repository.graph_nodes().values())
            created_nodes: list[dict[str, Any]] = []
            created_edges: list[dict[str, Any]] = []
            events = _read_jsonl(self.decision_events_path)
            counters = Counter()

            for selection in selections:
                if selection.get("kind") != "entity":
                    continue
                candidate_id = str(selection["candidate_id"])
                candidate = entity_index[candidate_id]
                report = report_index.get(("entity", candidate_id)) or {"decision": "reject", "reason": "missing_report"}
                final_status = "rejected"
                if report.get("decision") == "approve":
                    node_type = str(candidate.get("proposed_node_type") or "")
                    name = str(report.get("canonical_name") or candidate.get("entity_name") or "").strip()
                    existing = self._alias_node(name, all_nodes, {node_type})
                    if existing is not None:
                        node_id = str(existing["node_id"])
                    elif node_type in {"event", "location"} and _valid_new_node_name(name):
                        node_id = _review_node_id(node_type, name)
                        node = {
                            "node_id": node_id,
                            "node_type": node_type,
                            "name": name,
                            "attributes": {
                                "source": "deepseek_v4_pro_high_coverage_entity_review",
                                "entity_candidate_id": candidate_id,
                                "evidence_page_ids": list(candidate.get("evidence_page_ids") or []),
                                "source_types": list(candidate.get("source_types") or []),
                                "relation_candidate_ids": list(candidate.get("relation_candidate_ids") or []),
                                "automation_run_id": run_id,
                                "source_qwen_run_id": source_qwen_run_id,
                                "model_report_id": report.get("report_id"),
                            },
                            "confidence": "model_approved_high_coverage",
                            "model": DEEPSEEK_MODEL,
                            "review_status": "verified",
                            "automation_run_id": run_id,
                            "deepseek_completion_run_id": run_id,
                            "review_policy_version": COMPLETION_POLICY_VERSION,
                            "model_report_id": report.get("report_id"),
                            "created_at": now,
                        }
                        approved_nodes.append(node)
                        all_nodes.append(node)
                        created_nodes.append(node)
                    else:
                        node_id = ""
                    if node_id:
                        final_status = "approved"
                        candidate["approved_node_id"] = node_id
                    else:
                        candidate["deepseek_local_override_reason"] = "entity_mapping_failed_after_model_approve"
                candidate.update({
                    "review_status": final_status,
                    "decision_source": "deepseek_v4_pro_high_coverage",
                    "deepseek_completion_run_id": run_id,
                    "deepseek_model_report_id": report.get("report_id"),
                    "reviewed_at": now,
                })
                counters[f"entities_{final_status}"] += 1
                events.append({
                    "run_id": run_id, "kind": "entity", "candidate_id": candidate_id,
                    "before": selection["before"], "after_status": final_status, "created_at": now,
                })

            eligible: list[dict[str, Any]] = []
            for selection in selections:
                if selection.get("kind") != "relation":
                    continue
                candidate_id = str(selection["candidate_id"])
                candidate = relation_index[candidate_id]
                report = report_index.get(("relation", candidate_id)) or {"decision": "reject", "reason": "missing_report"}
                final_status = "rejected"
                if report.get("decision") == "approve":
                    relation_type = str(candidate.get("relation_type") or "").upper()
                    request_payload = dict((request_index.get(("relation", candidate_id)) or {}).get("payload") or {})
                    options = dict(request_payload.get("supplied_node_options") or {})
                    subject_option_ids = {str(row.get("node_id") or "") for row in options.get("subject", [])}
                    object_option_ids = {str(row.get("node_id") or "") for row in options.get("object", [])}
                    created_before = len(created_nodes)
                    nodes_before = len(all_nodes)
                    from_id = self._resolve_endpoint(
                        dict(report.get("subject_endpoint") or {}), candidate.get("subject"), set(_ACTOR_NODE_TYPES),
                        all_nodes, subject_option_ids, run_id, source_qwen_run_id, created_nodes, now,
                    )
                    to_id = self._resolve_endpoint(
                        dict(report.get("object_endpoint") or {}), candidate.get("object"), _object_endpoint_node_types(relation_type),
                        all_nodes, object_option_ids, run_id, source_qwen_run_id, created_nodes, now,
                    )
                    documents = self.repository.documents_by_id()
                    page_ids = list(dict.fromkeys(
                        str(documents[document_id].get("page_id") or "")
                        for document_id in candidate.get("evidence_document_ids", [])
                        if document_id in documents and documents[document_id].get("page_id")
                    ))
                    source_types = list(dict.fromkeys(
                        str(documents[document_id].get("source_type") or candidate.get("source_type") or "")
                        for document_id in candidate.get("evidence_document_ids", [])
                        if document_id in documents
                    ))
                    if from_id and to_id and page_ids and relation_type in {
                        "ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT", "HAS_PREFERENCE",
                        "PARTICIPATES_IN_EVENT", "VISITS_LOCATION", "OWNS_ITEM",
                    }:
                        eligible.append({
                            "candidate": candidate, "candidate_id": candidate_id, "selection": selection,
                            "report": report, "from_id": from_id, "to_id": to_id,
                            "relation_type": relation_type, "page_ids": page_ids, "source_types": source_types,
                            "scope": narrative_scope(relation_type, source_types),
                        })
                        continue
                    # Do not persist orphan endpoints created while resolving a
                    # relation that ultimately cannot be admitted.
                    del created_nodes[created_before:]
                    del all_nodes[nodes_before:]
                    candidate["deepseek_local_override_reason"] = "relation_mapping_failed_after_model_approve"
                candidate.update({
                    "review_status": final_status,
                    "decision_source": "deepseek_v4_pro_high_coverage",
                    "deepseek_completion_run_id": run_id,
                    "deepseek_model_report_id": report.get("report_id"),
                    "reviewed_at": now,
                })
                counters["relations_rejected"] += 1
                events.append({
                    "run_id": run_id, "kind": "relation", "candidate_id": candidate_id,
                    "before": selection["before"], "after_status": final_status, "created_at": now,
                })

            existing_keys = {
                (str(edge.get("from_id")), str(edge.get("relation_type")), str(edge.get("to_id")), str(edge.get("narrative_scope") or "unknown"))
                for edge in list(self.repository.graph_edges()) + approved_edges
            }
            grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
            for row in eligible:
                key = (row["from_id"], row["relation_type"], row["to_id"], row["scope"])
                grouped.setdefault(key, []).append(row)
            for key, rows in sorted(grouped.items()):
                rows.sort(key=lambda row: row["candidate_id"])
                candidate_ids = [row["candidate_id"] for row in rows]
                if key not in existing_keys:
                    primary = rows[0]
                    edge = {
                        "edge_id": "edge_deepseek_" + sha256((run_id + "\x1f" + "\x1f".join(key)).encode("utf-8")).hexdigest()[:16],
                        "from_id": key[0], "relation_type": key[1], "to_id": key[2], "narrative_scope": key[3],
                        "evidence_page_ids": list(dict.fromkeys(value for row in rows for value in row["page_ids"])),
                        "source_types": list(dict.fromkeys(value for row in rows for value in row["source_types"] if value)),
                        "source_manifests": ["deepseek_v4_pro_high_coverage_relation_review"],
                        "confidence": "model_approved_high_coverage",
                        "model": DEEPSEEK_MODEL,
                        "review_status": "verified",
                        "candidate_id": primary["candidate_id"],
                        "candidate_ids": candidate_ids,
                        "model_report_ids": [row["report"].get("report_id") for row in rows],
                        "automation_run_id": run_id,
                        "deepseek_completion_run_id": run_id,
                        "source_qwen_run_id": source_qwen_run_id,
                        "review_policy_version": COMPLETION_POLICY_VERSION,
                        "created_at": now,
                    }
                    approved_edges.append(edge)
                    created_edges.append(edge)
                    existing_keys.add(key)
                    primary_status = "approved"
                else:
                    primary_status = "superseded"
                for index, row in enumerate(rows):
                    status = primary_status if index == 0 else "superseded"
                    candidate = row["candidate"]
                    candidate.update({
                        "review_status": status,
                        "decision_source": "deepseek_v4_pro_high_coverage",
                        "deepseek_completion_run_id": run_id,
                        "deepseek_model_report_id": row["report"].get("report_id"),
                        "reviewed_at": now,
                    })
                    counters[f"relations_{status}"] += 1
                    events.append({
                        "run_id": run_id, "kind": "relation", "candidate_id": row["candidate_id"],
                        "before": row["selection"]["before"], "after_status": status, "created_at": now,
                    })

            final_count = sum(
                1 for selection in selections
                if (entity_index if selection.get("kind") == "entity" else relation_index)[str(selection["candidate_id"])].get("review_status") in FINAL_CANDIDATE_STATUSES
            )
            coverage = final_count / len(selections) if selections else 1.0
            if coverage < MINIMUM_FINAL_COVERAGE:
                raise RuntimeError(f"Admission coverage {coverage:.4%} is below the required {MINIMUM_FINAL_COVERAGE:.2%}.")

            _write_jsonl(self.repository.entity_candidates_path, entity_candidates)
            _write_jsonl(self.repository.review_candidates_path, relation_candidates)
            _write_jsonl(self.repository.approved_entity_nodes_path, approved_nodes + [node for node in created_nodes if node not in approved_nodes])
            _write_jsonl(self.repository.reviewed_edges_path, approved_edges)
            _write_jsonl(self.decision_events_path, events)
            self.repository.clear_caches()
            manifest["rollback_guard"] = {
                "nodes": {str(node["node_id"]): _stable_hash(node) for node in created_nodes},
                "edges": {str(edge["edge_id"]): _stable_hash(edge) for edge in created_edges},
            }
            manifest["status"] = "admitted"
            manifest["admitted_at"] = now
            manifest["admission_counts"] = dict(sorted(counters.items()))
            manifest["final_decision_coverage"] = coverage
            self._save_manifest(manifest)
            return self.get_run(run_id)

    def rollback(self, run_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(run_id)
        if manifest.get("status") != "admitted":
            raise ValueError("Only an admitted DeepSeek completion run can be rolled back.")
        with _REVIEW_WRITE_LOCK:
            events = [row for row in _read_jsonl(self.decision_events_path) if row.get("run_id") == run_id]
            relations = _read_jsonl(self.repository.review_candidates_path)
            entities = _read_jsonl(self.repository.entity_candidates_path)
            relation_index = {str(row.get("candidate_id") or ""): row for row in relations}
            entity_index = {str(row.get("entity_candidate_id") or ""): row for row in entities}
            conflicts: list[str] = []
            for event in events:
                current = (entity_index if event.get("kind") == "entity" else relation_index).get(str(event.get("candidate_id") or ""))
                if (
                    current is None
                    or current.get("deepseek_completion_run_id") != run_id
                    or current.get("review_status") != event.get("after_status")
                    or current.get("reviewed_at") != event.get("created_at")
                ):
                    conflicts.append(str(event.get("candidate_id") or ""))
            approved_nodes = _read_jsonl(self.repository.approved_entity_nodes_path)
            approved_edges = _read_jsonl(self.repository.reviewed_edges_path)
            guard = dict(manifest.get("rollback_guard") or {})
            run_nodes = {str(row.get("node_id")): row for row in approved_nodes if row.get("deepseek_completion_run_id") == run_id}
            run_edges = {str(row.get("edge_id")): row for row in approved_edges if row.get("deepseek_completion_run_id") == run_id}
            artifact_conflicts = [
                f"node:{identifier}" for identifier, expected in dict(guard.get("nodes") or {}).items()
                if identifier not in run_nodes or _stable_hash(run_nodes[identifier]) != expected
            ] + [
                f"edge:{identifier}" for identifier, expected in dict(guard.get("edges") or {}).items()
                if identifier not in run_edges or _stable_hash(run_edges[identifier]) != expected
            ]
            if conflicts or artifact_conflicts:
                raise ValueError("Rollback refused because DeepSeek-admitted data changed: " + ", ".join((conflicts + artifact_conflicts)[:20]))
            for event in events:
                if event.get("kind") == "entity":
                    entity_index[str(event["candidate_id"])] = dict(event["before"])
                else:
                    relation_index[str(event["candidate_id"])] = dict(event["before"])
            _write_jsonl(self.repository.entity_candidates_path, [entity_index[str(row["entity_candidate_id"])] for row in entities])
            _write_jsonl(self.repository.review_candidates_path, [relation_index[str(row["candidate_id"])] for row in relations])
            _write_jsonl(self.repository.approved_entity_nodes_path, [row for row in approved_nodes if row.get("deepseek_completion_run_id") != run_id])
            _write_jsonl(self.repository.reviewed_edges_path, [row for row in approved_edges if row.get("deepseek_completion_run_id") != run_id])
            self.repository.clear_caches()
            manifest["status"] = "rolled_back"
            manifest["rolled_back_at"] = _utc_now()
            self._save_manifest(manifest)
            return self.get_run(run_id)
