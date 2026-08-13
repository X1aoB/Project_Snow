# Application data contract

## Corpus document

Every `documents.jsonl` row has a stable `document_id`, `page_id`, `source_type`, `text`, `chunk_ordinal`, `canonical_url`, `local_path`, source manifest name, source license and a metadata object.

`metadata` contains known character, armour and costume IDs; source tier; whether a fragment comes from a costume page; and the current source status. The retrieval layer excludes inactive/deprecated source records, but armour and costume fragments remain eligible evidence for their associated character. Costume-specific tone is a later generation-context rule, not a retrieval exclusion rule.

## Character identity policy

The crawler snapshot may use both a given name and a full name for the same operator. Before runtime artifacts are built, application-level identity normalization merges those aliases into one stable character ID and displays the given name in the companion selector. NPC and world-lore entities remain in the corpus and verified graph, but are excluded from `persona_profiles.jsonl` when they are not approved as companion characters.

## Persona evidence

`persona_profiles.jsonl` only contains verified identity links and source-backed evidence buckets. `persona_trait_candidates.jsonl` is deliberately separate. A candidate must have a trait type, value, evidence chunk IDs, extraction version, confidence and `pending_review` state.

## Graph edge

Every edge includes `edge_id`, `from_id`, `relation_type`, `to_id`, `evidence_page_ids`, `source_manifest`, `confidence`, and `review_status`. Only `verified` edges are returned by normal graph lookup.

## Narrative relation review queue

`narrative_relation_jobs.jsonl` contains source pages awaiting AI-assisted relation extraction. It is not a candidate list. `narrative_relation_candidates.jsonl` remains empty until a privately configured provider has extracted evidence-backed proposals. Each candidate retains subject, relation type, object, confidence, rationale and evidence document IDs and is never retrievable while pending. A human approval records reviewer, note and explicit existing source/target graph node IDs. The calibrated Batch path may instead record `decision_source=model_policy`, its run/report/policy provenance and exact compatible node mappings. Both paths write a separate `approved_narrative_edges.jsonl` artifact with `review_status=verified`; their confidence and source manifests remain distinguishable.

### High-precision review view

The review service derives an in-memory `review_group_id` from a normalized **literal** `(subject, relation_type, object)` tuple. This is a review convenience only: whitespace and presentation punctuation are normalized, but aliases, nicknames, pronouns and character identities are never auto-merged. A group retains every candidate, direct quote, evidence document, evidence page, source type and extractor identity.

Groups expose a deterministic priority tier, source authority, risk flags and exact-name graph-node suggestions. Suggestions are constrained to relationship endpoint types (for example characters, senders, enemies, items or events); source-page, story, mail, voice and random-event document nodes are never offered as graph mappings. These fields determine review order and highlight context checks; they are not truth labels or approval scores. In particular, model confidence does not cause approval, `MENTIONS` remains low priority, and time-sensitive relationship/event claims retain a context warning.

`GET /api/v1/review/relations/groups` provides the ordered, filterable view; `GET /api/v1/review/relations/audit-sample` provides fixed-seed stratified samples across priority and relation/source strata. Group-detail responses support bounded candidate pages. These inspection endpoints do not mutate candidates. Human decisions remain individual. Separate `/api/v1/review/automation/*` endpoints require explicit paid-submit, calibrated-admission and rollback confirmations and store run-scoped audit events.

The decision endpoint verifies both mapped IDs and their endpoint types. A source must be an actor-type node; the target must be compatible with the relation vocabulary (for example, an event for `PARTICIPATES_IN_EVENT` or an item-type node for `OWNS_ITEM`). Source-document nodes such as `page`, `story`, `mail`, `voice` and `random_event` cannot be approved as relationship endpoints.

### Independent model-review reports

`relation_model_review_reports.jsonl` remains an append-only evidence layer. Each report is tied to one candidate and preserves its input hash, policy, provider/model, pass, verdict, quote, scope and validation flags. Reports alone never change a candidate. Only a separately confirmed automation admission may consume eligible reports after calibration and local graph mapping.

The browser displays conservative report summaries. `recommend_approve`, `recommend_reject` and `abstain` are never decisions by themselves. Human approval remains available; the additional automatic path requires category-specific calibration, local evidence validation, exact compatible node IDs, explicit admission and machine provenance.

## API stage lock

Retrieval, persona and graph inspection endpoints are available for internal validation. Every retrieval response declares the fixed `conversation_identity.user_role` as `分析员`. The chat endpoint remains disabled unless `CHAT_ENABLED=true`; this is a deployment guard, not a mechanism for bypassing review.
