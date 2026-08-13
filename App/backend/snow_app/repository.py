"""Read-only application repository for corpus, persona, and graph artifacts."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from .config import Settings
from .graph_metadata import hydrate_human_approved_edge, narrative_scope


_REVIEW_WRITE_LOCK = threading.RLock()

REVIEW_TIERS = ("high", "normal", "low")
REVIEW_RISK_LEVELS = ("high", "medium", "low")
MACHINE_REVIEW_FILTERS = (
    "recommend_approve",
    "recommend_reject",
    "abstain_or_incomplete",
    "mixed",
    "unreviewed",
)

# These weights determine review order only. They are deliberately not an
# automatic truth score: every candidate still requires an explicit human
# decision and graph-node mapping.
_RELATION_REVIEW_WEIGHT = {
    "ALLY_OF": 50,
    "OPPOSES": 50,
    "HAS_RELATIONSHIP_CONTEXT": 48,
    "HAS_PREFERENCE": 42,
    "PARTICIPATES_IN_EVENT": 28,
    "VISITS_LOCATION": 24,
    "OWNS_ITEM": 24,
    "MENTIONS": 0,
}
_HIGH_VALUE_RELATIONS = {"ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT", "HAS_PREFERENCE"}
_CONTEXT_SENSITIVE_RELATIONS = {"ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT", "PARTICIPATES_IN_EVENT"}
_ACTOR_NODE_TYPES = {"character", "sender", "enemy"}
_ITEM_NODE_TYPES = {"item", "weapon", "weapon_attachment", "furniture", "costume", "armor", "logistics_squad"}
_LOCATION_NODE_TYPES = {"location"}
_RELATION_ENDPOINT_NODE_TYPES = _ACTOR_NODE_TYPES | _ITEM_NODE_TYPES | _LOCATION_NODE_TYPES | {"event"}
ENTITY_NODE_REVIEW_STATUSES = {"pending_review", "needs_human_review", "approved", "rejected"}
_SOURCE_BUCKETS = {
    "main_story": ("canonical_narrative", 4),
    "character_story": ("canonical_narrative", 4),
    "affinity_story": ("canonical_narrative", 4),
    "character_profile": ("character_context", 3),
    "character_profiles": ("character_context", 3),
    "character_voice": ("character_context", 3),
    "character_affection": ("character_context", 3),
    "birthday_content": ("character_context", 3),
    "character_costume": ("character_context", 3),
    "character_costumes": ("character_context", 3),
    "furniture_lore": ("character_context", 3),
    "world_lore": ("world_context", 3),
    "exploration_notes": ("world_context", 3),
    "briefings": ("world_context", 3),
    "special_mail": ("situational_context", 2),
    "random_event": ("situational_context", 2),
    "event_lore": ("situational_context", 2),
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """Atomically replace a local review artifact, tolerating short Windows locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary_path, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2**attempt))
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _normalized_review_entity(value: Any) -> str:
    """Normalize only presentation differences; do not infer aliases or identities."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\-—_·•,，。！？!?：:;；'\"“”‘’()（）\[\]【】]+", "", normalized)


def _review_group_id(candidate: dict[str, Any]) -> str:
    key = "\x1f".join(
        (
            _normalized_review_entity(candidate.get("subject")),
            str(candidate.get("relation_type") or "").strip().upper(),
            _normalized_review_entity(candidate.get("object")),
        )
    )
    return "relation_group_" + sha256(key.encode("utf-8")).hexdigest()[:16]


def _source_bucket(source_type: Any) -> tuple[str, int]:
    return _SOURCE_BUCKETS.get(str(source_type or ""), ("other_context", 1))


def _safe_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    return confidence if 0.0 <= confidence <= 1.0 else None


def _object_endpoint_node_types(relation_type: str) -> set[str]:
    """Return safe graph endpoint types for exact-name suggestions only."""
    if relation_type in {"ALLY_OF", "OPPOSES", "HAS_RELATIONSHIP_CONTEXT"}:
        return _ACTOR_NODE_TYPES
    if relation_type == "HAS_PREFERENCE":
        return _ITEM_NODE_TYPES
    if relation_type == "PARTICIPATES_IN_EVENT":
        return {"event"}
    if relation_type == "VISITS_LOCATION":
        return _LOCATION_NODE_TYPES
    if relation_type == "OWNS_ITEM":
        return _ITEM_NODE_TYPES
    if relation_type == "KNOWS":
        return _ACTOR_NODE_TYPES
    if relation_type == "MENTIONS":
        return _RELATION_ENDPOINT_NODE_TYPES
    # There are no stable location nodes in the current graph artifact. A
    # reviewer may still map one manually after verifying a future node type.
    return set()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _review_node_id(node_type: str, entity_name: Any) -> str:
    """Return the deterministic ID assigned only after human node approval."""
    normalized_name = _normalized_review_entity(entity_name)
    digest = sha256(f"{node_type}\x1f{normalized_name}".encode("utf-8")).hexdigest()[:16]
    return f"{node_type}:review_{digest}"


class RuntimeRepository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.documents_path = settings.runtime_root / "lakehouse" / "documents.jsonl"
        self.lexical_path = settings.runtime_root / "indexes" / "lexical.sqlite3"
        self.vectors_path = settings.runtime_root / "vectors" / "local_vectors.jsonl"
        self.personas_path = settings.runtime_root / "personas" / "persona_profiles.jsonl"
        self.graph_nodes_path = settings.runtime_root / "graph" / "nodes.jsonl"
        self.graph_edges_path = settings.runtime_root / "graph" / "edges.jsonl"
        self.review_jobs_path = settings.runtime_root / "review" / "narrative_relation_jobs.jsonl"
        self.review_candidates_path = settings.runtime_root / "review" / "narrative_relation_candidates.jsonl"
        self.machine_review_reports_path = settings.runtime_root / "review" / "relation_model_review_reports.jsonl"
        self.review_events_path = settings.runtime_root / "review" / "relation_review_events.jsonl"
        self.reviewed_edges_path = settings.runtime_root / "review" / "approved_narrative_edges.jsonl"
        self.entity_candidates_path = settings.runtime_root / "review" / "entity_node_candidates.jsonl"
        self.entity_review_events_path = settings.runtime_root / "review" / "entity_node_review_events.jsonl"
        self.approved_entity_nodes_path = settings.runtime_root / "review" / "approved_entity_nodes.jsonl"
        # The semantic index is optional at serving time.  Do not let a missing
        # local embedding model turn an otherwise usable chat request into a
        # long Hugging Face download/retry sequence.
        self._embedding_model: Any | None = None
        self._embedding_unavailable = False

    @lru_cache(maxsize=1)
    def documents(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.documents_path)

    @lru_cache(maxsize=1)
    def documents_by_id(self) -> dict[str, dict[str, Any]]:
        return {document["document_id"]: document for document in self.documents()}

    @lru_cache(maxsize=1)
    def personas(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.personas_path)

    @lru_cache(maxsize=1)
    def graph_nodes(self) -> dict[str, dict[str, Any]]:
        deterministic = _read_jsonl(self.graph_nodes_path)
        approved = [
            node for node in _read_jsonl(self.approved_entity_nodes_path) if node.get("review_status") == "verified"
        ]
        return {node["node_id"]: node for node in deterministic + approved if node.get("node_id")}

    @lru_cache(maxsize=1)
    def graph_edges(self) -> list[dict[str, Any]]:
        deterministic = _read_jsonl(self.graph_edges_path)
        candidates_by_id = {
            str(candidate.get("candidate_id")): candidate
            for candidate in _read_jsonl(self.review_candidates_path)
            if candidate.get("candidate_id")
        }
        documents_by_id = self.documents_by_id()
        approved = [
            hydrate_human_approved_edge(edge, candidates_by_id, documents_by_id)
            for edge in _read_jsonl(self.reviewed_edges_path)
            if edge.get("review_status") == "verified"
        ]
        return list({edge["edge_id"]: edge for edge in deterministic + approved if edge.get("edge_id")}.values())

    def clear_caches(self) -> None:
        self.documents.cache_clear()
        self.documents_by_id.cache_clear()
        self.personas.cache_clear()
        self.graph_nodes.cache_clear()
        self.graph_edges.cache_clear()
        self._vectors.cache_clear()
        self._embedding_model = None
        self._embedding_unavailable = False

    def status(self) -> dict[str, bool]:
        return {
            "lakehouse": self.documents_path.exists(),
            "lexical_index": self.lexical_path.exists(),
            "vector_index": self.vectors_path.exists(),
            "personas": self.personas_path.exists(),
            "graph": self.graph_nodes_path.exists() and self.graph_edges_path.exists(),
        }

    def list_characters(self) -> list[dict[str, Any]]:
        return sorted(
            [
                {
                    "character_id": profile["character_id"],
                    "character_name": profile["character_name"],
                    "review_status": profile["review_status"],
                    "source_counts": profile["source_counts"],
                }
                for profile in self.personas()
            ],
            key=lambda value: value["character_name"],
        )

    def get_persona(self, character_id: str) -> dict[str, Any] | None:
        return next((profile for profile in self.personas() if profile["character_id"] == character_id), None)

    def _relation_candidates_for_status(self, review_status: str) -> list[dict[str, Any]]:
        return [
            candidate
            for candidate in _read_jsonl(self.review_candidates_path)
            if candidate.get("review_status") == review_status
        ]

    def _relation_candidate_evidence(
        self, candidate: dict[str, Any], documents: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for document_id in candidate.get("evidence_document_ids", []):
            document = documents.get(document_id)
            if document is None:
                continue
            evidence.append(
                {
                    "document_id": document_id,
                    "page_id": document.get("page_id"),
                    "title": document.get("title"),
                    "source_type": document.get("source_type"),
                    "canonical_url": document.get("canonical_url"),
                    "text": document.get("text", ""),
                }
            )
        return evidence

    def _latest_machine_reviews(self, candidate_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
        """Return the newest independent-review report for each current candidate.

        Model reports are advisory artifacts.  Selecting the latest report here
        only affects what a human sees in the review workspace; it never changes
        the candidate's state or creates a graph edge.
        """
        latest: dict[str, tuple[tuple[str, int], dict[str, Any]]] = {}
        for ordinal, report in enumerate(_read_jsonl(self.machine_review_reports_path)):
            candidate_id = str(report.get("candidate_id") or "")
            if not candidate_id or (candidate_ids is not None and candidate_id not in candidate_ids):
                continue
            sort_key = (str(report.get("reviewed_at") or ""), ordinal)
            previous = latest.get(candidate_id)
            if previous is None or sort_key >= previous[0]:
                latest[candidate_id] = (sort_key, report)
        return {candidate_id: report for candidate_id, (_, report) in latest.items()}

    @staticmethod
    def _machine_review_group_summary(
        candidates: list[dict[str, Any]], machine_reviews: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        reports = [
            machine_reviews[candidate_id]
            for candidate in candidates
            if (candidate_id := str(candidate.get("candidate_id") or "")) in machine_reviews
        ]
        verdict_counts = Counter(str(report.get("verdict") or "unknown") for report in reports)
        status_counts = Counter(str(report.get("review_status") or "unknown") for report in reports)
        completed_count = sum(
            1 for report in reports if report.get("review_status") in {"completed", "local_policy"}
        )
        total = len(candidates)
        if not reports:
            group_verdict = "unreviewed"
        elif completed_count == total and verdict_counts == Counter({"recommend_approve": total}):
            group_verdict = "recommend_approve"
        elif completed_count == total and verdict_counts == Counter({"recommend_reject": total}):
            group_verdict = "recommend_reject"
        elif verdict_counts.get("abstain") or completed_count < total:
            group_verdict = "abstain_or_incomplete"
        else:
            group_verdict = "mixed"
        reviewer_models = sorted(
            {
                "/".join(
                    part
                    for part in (
                        str((report.get("model_reviewer") or {}).get("provider") or ""),
                        str((report.get("model_reviewer") or {}).get("model") or ""),
                    )
                    if part
                )
                or "unknown"
                for report in reports
            }
        )
        return {
            "candidate_count": total,
            "reported_candidate_count": len(reports),
            "completed_candidate_count": completed_count,
            "unreviewed_candidate_count": total - len(reports),
            "verdict_counts": dict(sorted(verdict_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
            "audit_eligible_candidate_count": sum(bool(report.get("audit_eligible")) for report in reports),
            "group_verdict": group_verdict,
            "reviewer_models": reviewer_models,
            "policy": "Machine verdicts are advisory only. A group label never approves candidates or writes graph edges.",
        }

    def _graph_node_name_index(self) -> dict[str, list[dict[str, Any]]]:
        index: dict[str, list[dict[str, Any]]] = {}
        for node_id, node in self.graph_nodes().items():
            if node.get("node_type") not in _RELATION_ENDPOINT_NODE_TYPES:
                continue
            normalized_name = _normalized_review_entity(node.get("name"))
            if not normalized_name:
                continue
            index.setdefault(normalized_name, []).append(
                {
                    "node_id": node_id,
                    "node_type": node.get("node_type"),
                    "name": node.get("name"),
                }
            )
        for matches in index.values():
            matches.sort(key=lambda match: (str(match.get("node_type") or ""), str(match["node_id"])))
        return index

    def _review_group_summary(
        self,
        candidates: list[dict[str, Any]],
        documents: dict[str, dict[str, Any]],
        node_name_index: dict[str, list[dict[str, Any]]],
        machine_reviews: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build a literal-triple review group without alias or identity inference."""
        ordered = sorted(candidates, key=lambda candidate: str(candidate.get("candidate_id") or ""))
        representative = ordered[0]
        relation_type = str(representative.get("relation_type") or "").strip().upper()
        source_types = sorted({str(candidate.get("source_type") or "unknown") for candidate in ordered})
        bucket_pairs = [_source_bucket(source_type) for source_type in source_types]
        source_buckets = sorted({bucket for bucket, _ in bucket_pairs})
        max_source_authority = max((authority for _, authority in bucket_pairs), default=1)

        evidence_document_ids = list(
            dict.fromkeys(
                document_id
                for candidate in ordered
                for document_id in candidate.get("evidence_document_ids", [])
                if isinstance(document_id, str) and document_id
            )
        )
        available_evidence_ids = [document_id for document_id in evidence_document_ids if document_id in documents]
        missing_evidence_ids = [document_id for document_id in evidence_document_ids if document_id not in documents]
        page_ids = set(str(candidate["page_id"]) for candidate in ordered if candidate.get("page_id"))
        page_ids.update(
            str(documents[document_id]["page_id"])
            for document_id in available_evidence_ids
            if documents[document_id].get("page_id")
        )

        confidence_values = [
            confidence for confidence in (_safe_confidence(candidate.get("confidence")) for candidate in ordered) if confidence is not None
        ]
        evidence_quotes: list[dict[str, Any]] = []
        seen_quotes: set[tuple[str, tuple[str, ...]]] = set()
        for candidate in ordered:
            quote = str(candidate.get("evidence_quote") or "").strip()
            quote_key = (quote, tuple(candidate.get("evidence_document_ids", [])))
            if not quote or quote_key in seen_quotes:
                continue
            seen_quotes.add(quote_key)
            evidence_quotes.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "quote": quote,
                    "source_type": candidate.get("source_type"),
                    "page_id": candidate.get("page_id"),
                    "evidence_document_ids": candidate.get("evidence_document_ids", []),
                }
            )

        subject_matches = [
            match
            for match in node_name_index.get(_normalized_review_entity(representative.get("subject")), [])
            if match.get("node_type") in _ACTOR_NODE_TYPES
        ]
        object_matches = [
            match
            for match in node_name_index.get(_normalized_review_entity(representative.get("object")), [])
            if match.get("node_type") in _object_endpoint_node_types(relation_type)
        ]
        if len(subject_matches) == 1 and len(object_matches) == 1:
            mapping_status = "exact_unique_match_available"
        elif not subject_matches or not object_matches:
            mapping_status = "manual_mapping_required"
        else:
            mapping_status = "ambiguous_exact_match"

        risk_flags: list[str] = []
        if not available_evidence_ids:
            risk_flags.append("no_available_evidence")
        elif missing_evidence_ids:
            risk_flags.append("some_evidence_documents_missing")
        if relation_type in _CONTEXT_SENSITIVE_RELATIONS:
            risk_flags.append("timeline_or_context_check_required")
        if max_source_authority <= 2:
            risk_flags.append("situational_source_scope_check")
        if len(page_ids) <= 1:
            risk_flags.append("single_page_support")
        if confidence_values and min(confidence_values) < 0.8:
            risk_flags.append("lower_model_confidence")
        if relation_type == "MENTIONS":
            risk_flags.append("low_value_mention")

        if "no_available_evidence" in risk_flags or "some_evidence_documents_missing" in risk_flags:
            risk_level = "high"
        elif "timeline_or_context_check_required" in risk_flags and max_source_authority <= 2:
            risk_level = "medium"
        elif "lower_model_confidence" in risk_flags:
            risk_level = "medium"
        else:
            risk_level = "low"

        if relation_type == "MENTIONS":
            priority_tier = "low"
        elif relation_type in _HIGH_VALUE_RELATIONS and max_source_authority >= 3:
            priority_tier = "high"
        else:
            priority_tier = "normal"

        priority_score = (
            _RELATION_REVIEW_WEIGHT.get(relation_type, 12)
            + max_source_authority * 10
            + min(len(page_ids), 5)
            + min(len(ordered), 3)
        )
        primary_bucket = max(bucket_pairs, key=lambda pair: (pair[1], pair[0]), default=("other_context", 1))[0]
        extractor_models = sorted(
            {
                "/".join(
                    part
                    for part in (
                        str((candidate.get("extractor") or {}).get("provider") or ""),
                        str((candidate.get("extractor") or {}).get("model") or ""),
                    )
                    if part
                )
                or "unknown"
                for candidate in ordered
            }
        )
        return {
            "review_group_id": _review_group_id(representative),
            "review_status": representative.get("review_status"),
            "subject": representative.get("subject"),
            "relation_type": relation_type,
            "object": representative.get("object"),
            "candidate_count": len(ordered),
            "candidate_ids": [candidate.get("candidate_id") for candidate in ordered],
            "evidence_document_count": len(available_evidence_ids),
            "evidence_page_count": len(page_ids),
            "evidence_page_ids": sorted(page_ids),
            "missing_evidence_document_ids": missing_evidence_ids,
            "source_types": source_types,
            "source_buckets": source_buckets,
            "source_authority": max_source_authority,
            "extractor_models": extractor_models,
            "confidence": {
                "minimum": round(min(confidence_values), 4) if confidence_values else None,
                "maximum": round(max(confidence_values), 4) if confidence_values else None,
                "mean": round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else None,
            },
            "priority_tier": priority_tier,
            "priority_score": priority_score,
            "risk_level": risk_level,
            "risk_flags": risk_flags,
            "mapping_status": mapping_status,
            "mapping_suggestions": {"subject": subject_matches, "object": object_matches},
            "machine_review": self._machine_review_group_summary(ordered, machine_reviews or {}),
            "sampling_stratum": f"{priority_tier}|{relation_type}|{primary_bucket}",
            "representative_evidence_quotes": evidence_quotes[:3],
            "grouping_policy": "Candidates are grouped by normalized literal subject, relation type, and object only; aliases are never merged automatically. Node suggestions exclude source-page nodes.",
        }

    def _relation_review_groups(
        self, review_status: str, documents: dict[str, dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        documents = documents if documents is not None else self.documents_by_id()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for candidate in self._relation_candidates_for_status(review_status):
            grouped.setdefault(_review_group_id(candidate), []).append(candidate)
        node_name_index = self._graph_node_name_index()
        machine_reviews = self._latest_machine_reviews(
            {str(candidate.get("candidate_id")) for candidates in grouped.values() for candidate in candidates}
        )
        groups = [
            self._review_group_summary(candidates, documents, node_name_index, machine_reviews)
            for _, candidates in sorted(grouped.items())
        ]
        tier_rank = {tier: index for index, tier in enumerate(REVIEW_TIERS)}
        groups.sort(
            key=lambda group: (
                tier_rank.get(str(group["priority_tier"]), len(tier_rank)),
                -int(group["priority_score"]),
                str(group["review_group_id"]),
            )
        )
        return groups

    @staticmethod
    def _filter_relation_review_groups(
        groups: Iterable[dict[str, Any]],
        tier: str | None = None,
        relation_type: str | None = None,
        source_type: str | None = None,
        risk_level: str | None = None,
        machine_verdict: str | None = None,
    ) -> list[dict[str, Any]]:
        filtered = list(groups)
        if tier:
            filtered = [group for group in filtered if group["priority_tier"] == tier]
        if relation_type:
            filtered = [group for group in filtered if group["relation_type"] == relation_type]
        if source_type:
            filtered = [group for group in filtered if source_type in group["source_types"]]
        if risk_level:
            filtered = [group for group in filtered if group["risk_level"] == risk_level]
        if machine_verdict:
            filtered = [
                group
                for group in filtered
                if (group.get("machine_review") or {}).get("group_verdict") == machine_verdict
            ]
        return filtered

    def relation_review_triage_summary(
        self, review_status: str = "pending_review", groups: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        candidates = self._relation_candidates_for_status(review_status)
        groups = groups if groups is not None else self._relation_review_groups(review_status)
        group_counts_by_tier = Counter(group["priority_tier"] for group in groups)
        candidate_counts_by_tier = Counter(
            group["priority_tier"] for group in groups for _ in range(int(group["candidate_count"]))
        )
        group_counts_by_risk = Counter(group["risk_level"] for group in groups)
        candidate_counts_by_risk = Counter(
            group["risk_level"] for group in groups for _ in range(int(group["candidate_count"]))
        )
        relation_counts = Counter(str(candidate.get("relation_type") or "unknown") for candidate in candidates)
        source_counts = Counter(str(candidate.get("source_type") or "unknown") for candidate in candidates)
        mapping_counts = Counter(str(group["mapping_status"]) for group in groups)
        return {
            "review_status": review_status,
            "candidate_count": len(candidates),
            "group_count": len(groups),
            "by_tier": {
                tier: {"groups": group_counts_by_tier[tier], "candidates": candidate_counts_by_tier[tier]}
                for tier in REVIEW_TIERS
            },
            "by_risk_level": {
                risk: {"groups": group_counts_by_risk[risk], "candidates": candidate_counts_by_risk[risk]}
                for risk in REVIEW_RISK_LEVELS
            },
            "by_relation_type": dict(sorted(relation_counts.items())),
            "by_source_type": dict(sorted(source_counts.items())),
            "mapping_readiness": dict(sorted(mapping_counts.items())),
            "policy": "Triage changes human review order only. It never approves, rejects, maps aliases, or writes graph edges automatically.",
        }

    def relation_machine_review_summary(
        self, review_status: str = "pending_review", groups: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Summarize advisory model reports without treating them as decisions."""
        candidates = self._relation_candidates_for_status(review_status)
        candidate_ids = {str(candidate.get("candidate_id")) for candidate in candidates}
        reports = self._latest_machine_reviews(candidate_ids)
        status_counts = Counter(str(report.get("review_status") or "unknown") for report in reports.values())
        verdict_counts = Counter(str(report.get("verdict") or "unknown") for report in reports.values())
        model_counts = Counter(
            "/".join(
                part
                for part in (
                    str((report.get("model_reviewer") or {}).get("provider") or ""),
                    str((report.get("model_reviewer") or {}).get("model") or ""),
                )
                if part
            )
            or "unknown"
            for report in reports.values()
        )
        groups = groups if groups is not None else self._relation_review_groups(review_status)
        group_verdict_counts = Counter(
            str((group.get("machine_review") or {}).get("group_verdict") or "unreviewed") for group in groups
        )
        return {
            "review_status": review_status,
            "candidate_count": len(candidates),
            "reported_candidate_count": len(reports),
            "unreviewed_candidate_count": len(candidates) - len(reports),
            "completed_candidate_count": sum(
                1 for report in reports.values() if report.get("review_status") in {"completed", "local_policy"}
            ),
            "audit_eligible_candidate_count": sum(bool(report.get("audit_eligible")) for report in reports.values()),
            "by_status": dict(sorted(status_counts.items())),
            "by_verdict": dict(sorted(verdict_counts.items())),
            "by_model": dict(sorted(model_counts.items())),
            "group_verdict_counts": dict(sorted(group_verdict_counts.items())),
            "policy": "Reports are non-binding. Only human candidate decisions can create verified graph edges.",
        }

    def relation_review_summary(self) -> dict[str, Any]:
        job_counts = Counter(job.get("status", "unknown") for job in _read_jsonl(self.review_jobs_path))
        candidate_counts = Counter(candidate.get("review_status", "unknown") for candidate in _read_jsonl(self.review_candidates_path))
        pending_groups = self._relation_review_groups("pending_review")
        return {
            "jobs": {"total": sum(job_counts.values()), "by_status": dict(sorted(job_counts.items()))},
            "candidates": {"total": sum(candidate_counts.values()), "by_status": dict(sorted(candidate_counts.items()))},
            "approved_edges": len(_read_jsonl(self.reviewed_edges_path)),
            "triage": self.relation_review_triage_summary(groups=pending_groups),
            "machine_review": self.relation_machine_review_summary(groups=pending_groups),
            "policy": "Only human-approved candidates mapped to existing graph nodes become verified narrative edges.",
        }

    def _entity_node_candidates_for_status(self, review_status: str) -> list[dict[str, Any]]:
        return [
            candidate
            for candidate in _read_jsonl(self.entity_candidates_path)
            if candidate.get("review_status") == review_status
        ]

    def entity_review_summary(self) -> dict[str, Any]:
        """Summarize the independent missing-entity approval queue."""
        candidates = _read_jsonl(self.entity_candidates_path)
        approved_nodes = [
            node for node in _read_jsonl(self.approved_entity_nodes_path) if node.get("review_status") == "verified"
        ]
        status_counts = Counter(str(candidate.get("review_status") or "unknown") for candidate in candidates)
        pending = [candidate for candidate in candidates if candidate.get("review_status") == "pending_review"]
        return {
            "candidates": {"total": len(candidates), "by_status": dict(sorted(status_counts.items()))},
            "pending_relation_candidates_covered": sum(
                len(candidate.get("relation_candidate_ids") or []) for candidate in pending
            ),
            "by_node_type": dict(
                sorted(Counter(str(candidate.get("proposed_node_type") or "unknown") for candidate in pending).items())
            ),
            "approved_nodes": len(approved_nodes),
            "policy": (
                "Only a human-approved entity candidate creates a reusable graph node. "
                "Creating a node never approves any dependent narrative relation."
            ),
        }

    def entity_review_candidates(
        self, review_status: str = "pending_review", limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        candidates = self._entity_node_candidates_for_status(review_status)
        candidates.sort(
            key=lambda candidate: (
                -len(candidate.get("relation_candidate_ids") or []),
                str(candidate.get("proposed_node_type") or ""),
                str(candidate.get("entity_name") or ""),
            )
        )
        documents = self.documents_by_id()
        selected = candidates[offset : offset + limit]
        return {
            "review_status": review_status,
            "total": len(candidates),
            "offset": offset,
            "limit": limit,
            "candidates": [
                {
                    **candidate,
                    "evidence": self._relation_candidate_evidence(candidate, documents),
                }
                for candidate in selected
            ],
            "policy": (
                "Entity candidates are generated from literal relation endpoints. "
                "They have no graph effect until an individual human approval."
            ),
        }

    def relation_review_candidates(self, review_status: str = "pending_review", limit: int = 20) -> list[dict[str, Any]]:
        documents = self.documents_by_id()
        machine_reviews = self._latest_machine_reviews()
        return [
            {
                **candidate,
                "machine_review": machine_reviews.get(str(candidate.get("candidate_id") or "")),
                "evidence": self._relation_candidate_evidence(candidate, documents),
            }
            for candidate in self._relation_candidates_for_status(review_status)[:limit]
        ]

    def relation_review_groups(
        self,
        review_status: str = "pending_review",
        limit: int = 20,
        offset: int = 0,
        tier: str | None = None,
        relation_type: str | None = None,
        source_type: str | None = None,
        risk_level: str | None = None,
        machine_verdict: str | None = None,
    ) -> dict[str, Any]:
        groups = self._filter_relation_review_groups(
            self._relation_review_groups(review_status),
            tier,
            relation_type,
            source_type,
            risk_level,
            machine_verdict,
        )
        safe_offset = max(offset, 0)
        safe_limit = max(limit, 1)
        return {
            "review_status": review_status,
            "total": len(groups),
            "offset": safe_offset,
            "limit": safe_limit,
            "groups": groups[safe_offset : safe_offset + safe_limit],
        }

    def relation_review_group_detail(
        self,
        review_group_id: str,
        review_status: str = "pending_review",
        candidate_limit: int | None = None,
        candidate_offset: int = 0,
    ) -> dict[str, Any] | None:
        documents = self.documents_by_id()
        matching = [
            candidate
            for candidate in self._relation_candidates_for_status(review_status)
            if _review_group_id(candidate) == review_group_id
        ]
        if not matching:
            return None
        machine_reviews = self._latest_machine_reviews(
            {str(candidate.get("candidate_id")) for candidate in matching}
        )
        summary = self._review_group_summary(matching, documents, self._graph_node_name_index(), machine_reviews)
        candidates = sorted(matching, key=lambda candidate: str(candidate.get("candidate_id") or ""))
        safe_offset = max(candidate_offset, 0)
        safe_limit = len(candidates) if candidate_limit is None else max(candidate_limit, 1)
        visible_candidates = candidates[safe_offset : safe_offset + safe_limit]
        return {
            **summary,
            "candidate_total": len(candidates),
            "candidate_offset": safe_offset,
            "candidate_limit": safe_limit,
            "candidates": [
                {
                    **candidate,
                    "machine_review": machine_reviews.get(str(candidate.get("candidate_id") or "")),
                    "evidence": self._relation_candidate_evidence(candidate, documents),
                }
                for candidate in visible_candidates
            ],
        }

    @staticmethod
    def _stable_audit_rank(seed: str, group: dict[str, Any]) -> str:
        return sha256(f"{seed}\x1f{group['review_group_id']}".encode("utf-8")).hexdigest()

    def relation_review_audit_sample(
        self,
        review_status: str = "pending_review",
        size: int = 12,
        seed: str = "project-snow-audit-v1",
        tier: str | None = None,
        relation_type: str | None = None,
        source_type: str | None = None,
        risk_level: str | None = None,
        machine_verdict: str | None = None,
    ) -> dict[str, Any]:
        groups = self._filter_relation_review_groups(
            self._relation_review_groups(review_status),
            tier,
            relation_type,
            source_type,
            risk_level,
            machine_verdict,
        )
        requested_size = min(max(size, 1), len(groups))
        by_tier: dict[str, list[dict[str, Any]]] = {tier_name: [] for tier_name in REVIEW_TIERS}
        for group in groups:
            by_tier.setdefault(str(group["priority_tier"]), []).append(group)
        active_tiers = [tier_name for tier_name in REVIEW_TIERS if by_tier.get(tier_name)]

        quotas = {tier_name: 0 for tier_name in REVIEW_TIERS}
        if requested_size:
            if requested_size < len(active_tiers):
                for tier_name in active_tiers[:requested_size]:
                    quotas[tier_name] = 1
            else:
                for tier_name in active_tiers:
                    quotas[tier_name] = 1
                remaining = requested_size - len(active_tiers)
                total_groups = sum(len(by_tier[tier_name]) for tier_name in active_tiers)
                raw_allocations = {
                    tier_name: remaining * len(by_tier[tier_name]) / total_groups for tier_name in active_tiers
                }
                for tier_name in active_tiers:
                    quotas[tier_name] += int(raw_allocations[tier_name])
                remainder = remaining - sum(int(raw_allocations[tier_name]) for tier_name in active_tiers)
                for tier_name in sorted(
                    active_tiers,
                    key=lambda name: (-(raw_allocations[name] % 1), REVIEW_TIERS.index(name)),
                )[:remainder]:
                    quotas[tier_name] += 1

        selected: list[dict[str, Any]] = []
        for tier_name in active_tiers:
            strata: dict[str, list[dict[str, Any]]] = {}
            for group in by_tier[tier_name]:
                strata.setdefault(str(group["sampling_stratum"]), []).append(group)
            for queue in strata.values():
                queue.sort(key=lambda group: self._stable_audit_rank(seed, group))
            stratum_order = sorted(strata, key=lambda name: (-len(strata[name]), name))
            tier_selected = 0
            while tier_selected < quotas[tier_name]:
                progressed = False
                for stratum in stratum_order:
                    if tier_selected >= quotas[tier_name]:
                        break
                    if strata[stratum]:
                        selected.append(strata[stratum].pop(0))
                        tier_selected += 1
                        progressed = True
                if not progressed:
                    break

        return {
            "review_status": review_status,
            "seed": seed,
            "requested_size": size,
            "sample_size": len(selected),
            "available_group_count": len(groups),
            "tier_quotas": {tier_name: quotas[tier_name] for tier_name in active_tiers},
            "groups": selected,
            "policy": "Deterministic stratified sampling covers priority tiers and relation/source strata. Sampled groups still require individual human decisions.",
        }

    def decide_relation_candidate(
        self,
        candidate_id: str,
        decision: str,
        reviewer_id: str,
        note: str,
        from_node_id: str | None = None,
        to_node_id: str | None = None,
    ) -> dict[str, Any]:
        # The local UI can issue overlapping requests. Serialize the complete
        # read-modify-write sequence so one human decision cannot erase another.
        with _REVIEW_WRITE_LOCK:
            candidates = _read_jsonl(self.review_candidates_path)
            candidate = next((row for row in candidates if row.get("candidate_id") == candidate_id), None)
            if candidate is None:
                raise KeyError(candidate_id)
            if candidate.get("review_status") not in {"pending_review", "needs_human_review"}:
                raise ValueError("Only pending_review or needs_human_review candidates can receive a human decision.")
            if decision not in {"approved", "rejected"}:
                raise ValueError("Decision must be approved or rejected.")

            reviewed_at = _utc_now()
            review_group_id = _review_group_id(candidate)
            candidate.update(
                {
                    "review_status": decision,
                    "reviewer_id": reviewer_id,
                    "review_note": note,
                    "reviewed_at": reviewed_at,
                }
            )
            approved_edge: dict[str, Any] | None = None
            if decision == "approved":
                if not from_node_id or not to_node_id:
                    raise ValueError("Approved candidates require explicit source and target graph node IDs.")
                nodes = self.graph_nodes()
                if from_node_id not in nodes or to_node_id not in nodes:
                    raise ValueError("Approved candidates must map to existing graph node IDs.")
                from_node_type = str(nodes[from_node_id].get("node_type") or "")
                to_node_type = str(nodes[to_node_id].get("node_type") or "")
                if from_node_type not in _ACTOR_NODE_TYPES:
                    raise ValueError("Approved candidates require an actor-type source graph node, not a source-page node.")
                allowed_target_types = _object_endpoint_node_types(str(candidate.get("relation_type") or ""))
                if to_node_type not in allowed_target_types:
                    allowed_description = ", ".join(sorted(allowed_target_types)) or "a relation-compatible endpoint"
                    raise ValueError(
                        f"Approved {candidate.get('relation_type')} candidates require a target node type in: {allowed_description}."
                    )
                documents = self.documents_by_id()
                source_types = list(
                    dict.fromkeys(
                        source_type
                        for source_type in (
                            str(candidate.get("source_type") or ""),
                            *(
                                str(documents[document_id].get("source_type") or "")
                                for document_id in candidate.get("evidence_document_ids", [])
                                if document_id in documents
                            ),
                        )
                        if source_type
                    )
                )
                page_ids = list(
                    dict.fromkeys(
                        documents[document_id]["page_id"]
                        for document_id in candidate.get("evidence_document_ids", [])
                        if document_id in documents
                    )
                )
                if not page_ids:
                    raise ValueError("Approved candidates require at least one existing evidence document.")
                edge_id = "edge_review_" + sha256(
                    f"{candidate_id}\x1f{from_node_id}\x1f{candidate['relation_type']}\x1f{to_node_id}".encode("utf-8")
                ).hexdigest()[:16]
                approved_edge = {
                    "edge_id": edge_id,
                    "from_id": from_node_id,
                    "relation_type": candidate["relation_type"],
                    "to_id": to_node_id,
                    "evidence_page_ids": page_ids,
                    "source_manifests": ["human_relation_review"],
                    "confidence": "human_approved",
                    "review_status": "verified",
                    "source_types": source_types,
                    "narrative_scope": narrative_scope(str(candidate.get("relation_type") or ""), source_types),
                    "candidate_id": candidate_id,
                    "review_group_id": review_group_id,
                    "reviewer_id": reviewer_id,
                    "review_note": note,
                    "created_at": reviewed_at,
                }
                approved_edges = [
                    edge for edge in _read_jsonl(self.reviewed_edges_path) if edge.get("candidate_id") != candidate_id
                ]
                approved_edges.append(approved_edge)
                _write_jsonl(self.reviewed_edges_path, approved_edges)

            _write_jsonl(self.review_candidates_path, candidates)
            events = _read_jsonl(self.review_events_path)
            events.append(
                {
                    "event_id": "review_" + sha256(f"{candidate_id}\x1f{reviewed_at}".encode("utf-8")).hexdigest()[:16],
                    "candidate_id": candidate_id,
                    "review_group_id": review_group_id,
                    "decision": decision,
                    "reviewer_id": reviewer_id,
                    "note": note,
                    "from_node_id": from_node_id,
                    "to_node_id": to_node_id,
                    "created_at": reviewed_at,
                    "policy": "One candidate decision only; no group or automatic approval is performed.",
                }
            )
            _write_jsonl(self.review_events_path, events)
            self.graph_edges.cache_clear()
            return {**candidate, "approved_edge": approved_edge}

    def decide_entity_node_candidate(
        self, entity_candidate_id: str, decision: str, reviewer_id: str, note: str
    ) -> dict[str, Any]:
        """Approve or reject one proposed location/event node without touching relations."""
        with _REVIEW_WRITE_LOCK:
            candidates = _read_jsonl(self.entity_candidates_path)
            candidate = next(
                (row for row in candidates if row.get("entity_candidate_id") == entity_candidate_id), None
            )
            if candidate is None:
                raise KeyError(entity_candidate_id)
            if candidate.get("review_status") not in {"pending_review", "needs_human_review"}:
                raise ValueError("Only pending_review or needs_human_review entity candidates can receive a human decision.")
            if decision not in {"approved", "rejected"}:
                raise ValueError("Entity candidate decision must be approved or rejected.")

            node_type = str(candidate.get("proposed_node_type") or "")
            entity_name = str(candidate.get("entity_name") or "").strip()
            expected_node_id = _review_node_id(node_type, entity_name)
            if node_type not in {"location", "event"} or not entity_name:
                raise ValueError("Only non-empty location or event node candidates may be approved.")
            if str(candidate.get("proposed_node_id") or "") != expected_node_id:
                raise ValueError("Entity candidate has an invalid proposed graph node ID.")

            reviewed_at = _utc_now()
            candidate.update(
                {
                    "review_status": decision,
                    "reviewer_id": reviewer_id,
                    "review_note": note,
                    "reviewed_at": reviewed_at,
                }
            )
            approved_node: dict[str, Any] | None = None
            if decision == "approved":
                nodes = self.graph_nodes()
                if expected_node_id in nodes:
                    raise ValueError("A graph node already exists for this entity candidate. Reload the review queue.")
                normalized_name = _normalized_review_entity(entity_name)
                if any(
                    node.get("node_type") == node_type
                    and _normalized_review_entity(node.get("name")) == normalized_name
                    for node in nodes.values()
                ):
                    raise ValueError("An exact graph node name already exists for this entity type. Reload the review queue.")
                evidence_page_ids = list(dict.fromkeys(str(page_id) for page_id in candidate.get("evidence_page_ids", []) if page_id))
                if not evidence_page_ids:
                    raise ValueError("Approved entity nodes require at least one traceable evidence page.")
                approved_node = {
                    "node_id": expected_node_id,
                    "node_type": node_type,
                    "name": entity_name,
                    "attributes": {
                        "source": "human_entity_review",
                        "entity_candidate_id": entity_candidate_id,
                        "evidence_page_ids": evidence_page_ids,
                        "source_types": list(candidate.get("source_types") or []),
                        "relation_candidate_ids": list(candidate.get("relation_candidate_ids") or []),
                    },
                    "confidence": "human_approved",
                    "review_status": "verified",
                    "reviewer_id": reviewer_id,
                    "review_note": note,
                    "created_at": reviewed_at,
                }
                approved_nodes = [
                    node
                    for node in _read_jsonl(self.approved_entity_nodes_path)
                    if node.get("node_id") != expected_node_id
                    and node.get("attributes", {}).get("entity_candidate_id") != entity_candidate_id
                ]
                approved_nodes.append(approved_node)
                _write_jsonl(self.approved_entity_nodes_path, approved_nodes)
                candidate["approved_node_id"] = expected_node_id

            _write_jsonl(self.entity_candidates_path, candidates)
            events = _read_jsonl(self.entity_review_events_path)
            events.append(
                {
                    "event_id": "entity_review_"
                    + sha256(f"{entity_candidate_id}\x1f{reviewed_at}".encode("utf-8")).hexdigest()[:16],
                    "entity_candidate_id": entity_candidate_id,
                    "decision": decision,
                    "reviewer_id": reviewer_id,
                    "note": note,
                    "node_id": expected_node_id if decision == "approved" else None,
                    "created_at": reviewed_at,
                    "policy": "One entity decision only; node approval never approves a dependent relationship.",
                }
            )
            _write_jsonl(self.entity_review_events_path, events)
            self.graph_nodes.cache_clear()
            self.graph_edges.cache_clear()
            return {**candidate, "approved_node": approved_node}

    def _is_allowed_context(self, document: dict[str, Any], character_id: str | None) -> bool:
        metadata = document.get("metadata", {})
        document_character_ids = set(metadata.get("related_character_ids", []) or [])
        if metadata.get("character_id"):
            document_character_ids.add(metadata["character_id"])
        if character_id and document_character_ids and character_id not in document_character_ids:
            return False
        return True

    def _fts_query(self, query: str) -> str:
        # Query the derived `terms` column. It contains CJK bigrams and avoids
        # treating a full natural-language Chinese question as a single token.
        tokens: list[str] = []
        for segment in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", query):
            if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
                tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
                if len(segment) == 1:
                    tokens.append(segment)
            else:
                tokens.append(segment.lower())
        return " OR ".join(f'terms:"{token}"' for token in dict.fromkeys(tokens)) or '""'

    def lexical_search(self, query: str, limit: int = 40) -> list[tuple[str, int]]:
        if not self.lexical_path.exists():
            return []
        connection = sqlite3.connect(self.lexical_path)
        try:
            rows = connection.execute(
                """
                SELECT documents.document_id, bm25(documents_fts) AS score
                FROM documents_fts
                JOIN documents ON documents.document_id = documents_fts.document_id
                WHERE documents_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (self._fts_query(query), limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            connection.close()
        return [(document_id, rank) for rank, (document_id, _) in enumerate(rows, start=1)]

    @lru_cache(maxsize=1)
    def _vectors(self) -> dict[str, list[float]]:
        return {row["document_id"]: row["vector"] for row in _read_jsonl(self.vectors_path)}

    def _embed_query(self, query: str) -> list[float] | None:
        if not self.vectors_path.exists() or self._embedding_unavailable:
            return None
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            self._embedding_unavailable = True
            return None
        try:
            if self._embedding_model is None:
                # Retrieval must remain responsive on an offline deployment.
                # If the model has not been installed locally, fall back to
                # lexical retrieval instead of attempting a network download.
                self._embedding_model = SentenceTransformer(
                    self.settings.embedding_model,
                    local_files_only=True,
                )
            return self._embedding_model.encode([query], normalize_embeddings=True)[0].tolist()
        except Exception:
            # Avoid repeating a failed model load for every subsequent message.
            # `hybrid_search` treats a missing query vector as lexical-only.
            self._embedding_model = None
            self._embedding_unavailable = True
            return None

    def vector_search(self, query: str, limit: int = 40) -> list[tuple[str, int]]:
        query_vector = self._embed_query(query)
        vectors = self._vectors()
        if query_vector is None or not vectors:
            return []
        scored = [
            (document_id, sum(left * right for left, right in zip(query_vector, vector, strict=True)))
            for document_id, vector in vectors.items()
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [(document_id, rank) for rank, (document_id, _) in enumerate(scored[:limit], start=1)]

    def hybrid_search(
        self,
        query: str,
        character_id: str | None,
        limit: int,
    ) -> tuple[str, bool, list[dict[str, Any]]]:
        lexical = self.lexical_search(query)
        vectors = self.vector_search(query)
        combined: dict[str, dict[str, float | int | None]] = {}
        for document_id, rank in lexical:
            combined.setdefault(document_id, {"score": 0.0, "lexical_rank": None, "vector_rank": None})
            combined[document_id]["score"] = float(combined[document_id]["score"]) + 1 / (60 + rank)
            combined[document_id]["lexical_rank"] = rank
        for document_id, rank in vectors:
            combined.setdefault(document_id, {"score": 0.0, "lexical_rank": None, "vector_rank": None})
            combined[document_id]["score"] = float(combined[document_id]["score"]) + 1 / (60 + rank)
            combined[document_id]["vector_rank"] = rank
        documents = self.documents_by_id()
        results = []
        for document_id, ranking in combined.items():
            document = documents.get(document_id)
            if document is None or not self._is_allowed_context(document, character_id):
                continue
            adjusted_score = float(ranking["score"]) * float(document["metadata"].get("source_priority", 0.5))
            results.append(
                {
                    "citation": {
                        "document_id": document_id,
                        "page_id": document["page_id"],
                        "title": document["title"],
                        "source_type": document["source_type"],
                        "canonical_url": document.get("canonical_url"),
                        "local_path": document.get("local_path"),
                        "source_license": document.get("source_license"),
                    },
                    "text": document["text"],
                    "score": round(adjusted_score, 8),
                    "lexical_rank": ranking["lexical_rank"],
                    "vector_rank": ranking["vector_rank"],
                    "metadata": document["metadata"],
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return ("rrf" if vectors else "lexical_only"), bool(vectors), results[:limit]

    def serving_graph_context(
        self,
        query: str,
        character_id: str | None,
        intents: tuple[str, ...],
    ) -> dict[str, Any]:
        """Optional production graph hook; local artifact mode stays unchanged."""

        return {"status": "not_configured", "nodes": [], "edges": []}

    def neighborhood(self, graph_node_id: str) -> dict[str, Any] | None:
        node = self.graph_nodes().get(graph_node_id)
        if node is None:
            return None
        edges = [
            edge
            for edge in self.graph_edges()
            if edge.get("review_status") == "verified" and (edge["from_id"] == graph_node_id or edge["to_id"] == graph_node_id)
        ]
        adjacent_ids = {
            edge["to_id"] if edge["from_id"] == graph_node_id else edge["from_id"]
            for edge in edges
        }
        nodes = self.graph_nodes()
        return {"node": node, "edges": edges, "adjacent_nodes": [nodes[value] for value in adjacent_ids if value in nodes]}
