# Architecture decisions

## Boundaries

- `Data/` is a source contract owned by the crawler. Application code only reads it.
- `App/runtime/` contains all derived tables, indexes, databases, review queues and logs.
- `Data/Manifest/page_manifest.jsonl` supplies active/deprecated status and provenance. Specialized `*_index.jsonl` files are the only corpus-discovery inputs.

## B: persona-first hybrid retrieval

1. The lakehouse creates source-aware chunks from specialized manifests and their referenced raw pages.
2. SQLite FTS5 provides lexical recall for exact names, chapter labels, armour and costume names.
3. A local Chinese sentence-transformer provides semantic recall; Qdrant is an optional serving copy.
4. Reciprocal-rank fusion combines lexical and semantic candidates. The selected character constrains character-specific evidence; armour and costume material remain available as source-backed context. Their lower source priority prevents situational costume text from displacing core story evidence.
5. Persona profiles are evidence inventories, not free-form model summaries. Candidate traits always retain source chunk IDs and need review before activation.

## Conversation modes and style context

The MVP has two explicit, server-enforced conversation modes. `immersive` is
the default: the character lives in the game world, treats the user as the
Analyst, hides retrieval/model/tool concepts, and cannot call tools. `assistant`
is an opt-in mode: the character may explain evidence and controlled tool
results while preserving the same personality and relationship. A session is
scoped by character and mode; switching mode with an existing session ID starts
an isolated context instead of leaking assistant meta-knowledge into
immersive companionship.

Armor and costumes are not selectable identities. The server resolves an armor
or costume name from the message (or an explicit `costume_context` override),
links a costume to its `armor_id`, filters costume evidence to that exact
costume, and keeps the resolved style in the current session. Naming an armor
alone never unlocks all of its costumes; omitting a style never randomly mixes
costume dialogue into the character's core voice. A reset phrase such as
“换回本体” clears the style context.

## Communication medium and scene state

`communication_channel` is orthogonal to the persona mode and currently has
two values: `in_person` and `text` (`voice` is reserved but not exposed).
New sessions default to `in_person`; the browser stores a per-character
preference and unsent mode draft in local storage. Full display history and
durable session/world snapshots are stored in
`App/runtime/chat/conversations.sqlite3`. Generation does not ingest the whole
display history: it reads bounded turns from the current mode plus explicit
shared relationship, style and world-state continuity.
Each response retains the legacy `answer` and also returns structured
`content_blocks`: `speech`/`action` for face-to-face and `message` for text.
The server validates the block types and rejects unreported visual knowledge or
completed physical actions in text mode, with one controlled model rewrite and
a deterministic fallback.

The shared lightweight world state contains each character's location and
`analyst_location`. The first face-to-face request co-locates the Analyst with
the selected character. Later cross-location face-to-face requests return a
structured 409 (`communication_context_conflict`) offering `join_character`
or `switch_to_text`; joining updates the Analyst location once and reports a
single scene transition. Historical story scenes are evidence only and never
change the current medium. A dialogue request to switch medium takes effect
after that reply, while an explicit current-state declaration such as “我现在
正在用通讯器给你发消息” takes effect immediately.

## Local product surfaces

- `/` is the 22-character chat client.
- `/workspace/` contains evidence retrieval, persona inspection, relation and
  entity review, feedback triage and the legacy dialogue debugger.
- `POST /api/v1/mvp/chat` remains backward compatible while optionally accepting
  a client idempotency key.
- Feedback source records and triage events are append-only. Conversation
  deletion affects only the selected local chat history.
- The Electron application is a sandboxed web shell with no Node or filesystem
  API exposed to page content.

## C: two-layer knowledge graph

- Deterministic edges are built only from explicit manifest fields and enter the graph as `review_status=verified`.
- Narrative relation extraction produces `review_status=pending_review` candidates with evidence, confidence and a restricted relation vocabulary. Such edges are excluded from graph retrieval until a reviewer approves them.
- An optional independent OpenAI-compatible second-review pipeline evaluates the proposed triple against its original evidence without receiving the extractor rationale or confidence. It writes advisory reports only; deterministic evidence checks, the second model, and browser triage never mutate a candidate or graph edge.
- Quality calibration uses fixed-seed, stratified human samples from the model-suggested pool. A future batch-admission policy may be considered only after its measured error rate is acceptable; it is not part of the current graph-write path.
- Neo4j is a serving projection. JSONL node/edge files are the portable, versionable graph source of truth.

## Non-negotiable role rule

The current user is always the Analyst (分析员), not an independently configurable persona. Immersive and assistant modes alter task behavior only; they never remove or replace the character-to-Analyst relationship. The retrieval API returns this invariant explicitly, and generation enforces it server-side. Assistant task results remain structurally visible so character style cannot corrupt factual output.
