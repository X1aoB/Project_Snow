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

`narrative_relation_jobs.jsonl` contains source pages awaiting AI-assisted relation extraction. It is not a candidate list. `narrative_relation_candidates.jsonl` remains empty until a privately configured provider has extracted evidence-backed proposals. Each candidate must retain subject, relation type, object, confidence, rationale and evidence document IDs; it must remain `pending_review` and is never retrievable before human approval. A human approval additionally records reviewer, note and explicit existing source/target graph node IDs, then writes a separate `approved_narrative_edges.jsonl` artifact with `review_status=verified`.

### High-precision review view

The review service derives an in-memory `review_group_id` from a normalized **literal** `(subject, relation_type, object)` tuple. This is a review convenience only: whitespace and presentation punctuation are normalized, but aliases, nicknames, pronouns and character identities are never auto-merged. A group retains every candidate, direct quote, evidence document, evidence page, source type and extractor identity.

Groups expose a deterministic priority tier, source authority, risk flags and exact-name graph-node suggestions. Suggestions are constrained to relationship endpoint types (for example characters, senders, enemies, items or events); source-page, story, mail, voice and random-event document nodes are never offered as graph mappings. These fields determine review order and highlight context checks; they are not truth labels or approval scores. In particular, model confidence does not cause approval, `MENTIONS` remains low priority, and time-sensitive relationship/event claims retain a context warning.

`GET /api/v1/review/relations/groups` provides the ordered, filterable view; `GET /api/v1/review/relations/audit-sample` provides fixed-seed stratified samples across priority and relation/source strata. Group-detail responses support bounded candidate pages so a heavily corroborated relation can be inspected without rendering every candidate at once. None of these endpoints mutates candidates. The only mutation endpoint remains the individual candidate decision endpoint. Its audit event records the derived group ID, reviewer, note and explicit node mapping; there is deliberately no group-decision or batch-approval endpoint.

The decision endpoint verifies both mapped IDs and their endpoint types. A source must be an actor-type node; the target must be compatible with the relation vocabulary (for example, an event for `PARTICIPATES_IN_EVENT` or an item-type node for `OWNS_ITEM`). Source-document nodes such as `page`, `story`, `mail`, `voice` and `random_event` cannot be approved as relationship endpoints.

### Independent model-review reports

`relation_model_review_reports.jsonl` is a separate, append-only advisory layer for a second provider/model. Each report is tied to one candidate and preserves its literal review group, evidence-document IDs, input hash, review-policy version, provider/model label, verdict, direct supporting quote, scope, risk flags and retry metadata. A report may be `completed`, `failed`, or `local_policy`; it never changes `narrative_relation_candidates.jsonl` and never creates a graph edge.

The browser displays only the newest report for each candidate and derives a conservative group summary. `recommend_approve`, `recommend_reject`, `abstain`, and deterministic low-value exclusions are review aids, not candidate decisions. The only path to `review_status=verified` remains the individual human-decision endpoint with explicit compatible graph node IDs.

## API stage lock

Retrieval, persona and graph inspection endpoints are available for internal validation. Every retrieval response declares the fixed `conversation_identity.user_role` as `分析员`. The chat endpoint remains disabled unless `CHAT_ENABLED=true`; this is a deployment guard, not a mechanism for bypassing review.
