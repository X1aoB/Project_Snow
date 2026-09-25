# Architecture decisions

## Boundaries

- `Data/` is a source contract owned by the crawler. Application code only reads it.
- `App/runtime/` contains all derived tables, indexes, databases, review queues and logs.
- `Data/Manifest/page_manifest.jsonl` supplies active/deprecated status and provenance. Specialized `*_index.jsonl` files are the only corpus-discovery inputs.

## Public send and retrieval/reply contract

The public face-to-face send path is intentionally ordered and idempotent:

```text
HTTP admission and idempotency
  -> canonical content blocks, channel and scene validation
  -> query planning
  -> concurrent FTS5 and vector recall
  -> reciprocal-rank fusion and source-aware filtering
  -> character, evidence and current-scene constraints
  -> bounded context assembly
  -> at most two provider calls (initial generation plus one controlled repair)
  -> content-block and role validation
  -> deterministic safety/continuity fallback
  -> signed state, durable terminal result and SSE presentation
```

Every public HTTP or SSE failure carries a stable error code, a bounded
`stage`, a retryability flag, and the request UUID when it is available. The
stages are `content_validation`, `state_validation`, `character_data`,
`generation_queue`, `retrieval`, `provider`, `generation_validation`,
`idempotency_store`, `transport`, and `unknown`.
Transport or admission failures are uncertain until the same UUID is
reconciled through the durable request record; clients must reuse that UUID
instead of blindly starting a second paid request. A known terminal generation
failure may be explicitly retried with a new UUID.

The channel contract is server-enforced. Text communication accepts only
`message` and `sticker`; face-to-face communication accepts only `speech` and
`action`. The user is always the Analyst. The model cannot turn an unstated
physical action, visual observation, or feeling into a fact. Current scene state
is authoritative; historical story evidence cannot move the current scene or
change the communication channel. Character and cross-character evidence must
remain source-bound, and graph relations marked `pending_review` are excluded
from production retrieval.

| Rule area | Required behavior | Enforcement point |
| --- | --- | --- |
| Persona | The user is always the Analyst; immersive mode never exposes retrieval, tools, or model mechanics. | Prompt contract and response guard |
| Channel | Text accepts `message`/`sticker`; face-to-face accepts `speech`/`action`. | Input block validator and output normalizer |
| Analyst facts | Unstated analyst actions, visuals, and feelings remain unknown. | Guardrail and deterministic fallback |
| Scene precedence | Current scene state wins; history cannot move the location or change the medium. | Scene filter and state guard |
| Evidence scope | Character, source document, and graph evidence stay bound; unreviewed relations stay out. | Retrieval filter and citation validation |
| Dependency failure | Vector or graph timeouts degrade to lexical or no-graph retrieval; database failure is fail-closed. | Retrieval adapter and idempotency store |
| Budgets | Keep 12 history rounds, 64 KiB request bodies, 3-second retrieval, 4 active/8 queued generation slots, and at most two provider calls. | Contracts, queue gate, and generation budget |

Retrieval degradation is bounded and observable: vector failure falls back to
lexical search, graph failure removes graph context, and the shared three-second
retrieval deadline remains in force. Diagnostics record fusion mode, hit count,
source categories, degraded dependencies, retrieval latency, guard outcomes,
provider-call count, and error stage, without prompts, responses, credentials,
raw IPs, or private provider identifiers.

The existing resource limits are part of the public compatibility contract:
recent history is capped at 12 rounds, each request body at 64 KiB, retrieval
at 3 seconds, the generation gate at 4 active slots with 8 queued slots, and
each operation at no more than two provider calls. These limits remain fixed
while retrieval weights and reranking are evaluated.

The synchronous public store is isolated behind a bounded adapter with five
transaction workers and eight waiting positions. This is deliberately aligned
with the 4-active/8-queued generation envelope: a short rate-limit, claim or
terminal-write transaction cannot reject an otherwise admitted chat only
because the adapter has fewer waiting positions than the generation gate. The
adapter remains fail-closed when all 13 positions are occupied; that response
is retryable, carries `Retry-After: 2`, and is included in backpressure
observability. This is a database capacity bound, not permission to add
Uvicorn workers or to bypass the single-process generation gate.

Retrieval optimization is incremental. First measure synthetic golden queries
for persona facts, current state, relationships, costume/style, casual turns,
cross-character mentions, and dependency failures. Then tune lane weighting or
deduplication behind a feature flag, using evidence hit rate, recall@k, P95
retrieval latency, guard-fallback rate, terminal error rate, and duplicate-request
rate as acceptance gates. The default ranking and source data remain unchanged
until those measurements pass review.

Public feedback follows the same append-only triage principle as the internal
MVP feedback stream. The PostgreSQL `public_feedback_triage` table records the
latest operator decision without rewriting the submitted row; the private
admin list joins that decision while keeping the default `pending_triage`
state for new reports. The receipt outbox is separate: a public report is
queued at submission, the dedicated mailer sends its public number, timestamp,
submitted feedback body and optional QQ contact, and an authenticated loopback
admin action can safely requeue the latest report or an explicit number.
Conversation context, diagnostics, IP data and SMTP credentials never enter
that mail path. The mailer role is granted only `body_text` and encrypted
`qq_cipher`; the QQ key is mounted separately and is used only while rendering
this operator email.

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

## v0.5.0 immersive and legacy Agent boundary

The browser product has a stable entry page at `/`, a deep ice-blue immersive
surface at `/immersive/`, and a fixed-sidebar internal workspace at
`/workspace/`. The former standalone Agent surface has been removed. Historical
assistant records and the legacy AgentRuntime remain isolated debugging data;
task traces and ordinary technical conversation never enter immersive
generation context.

Provider and model configuration lives in `runtime/chat/agent.sqlite3`; secrets
do not. The database stores an opaque credential reference while keyring uses
Windows Credential Manager/DPAPI for the actual key. A model capability is
routable only after an adapter declaration plus active probe or explicit user
override. Routing first matches capabilities and the Provider's approved data
types, then uses quality score and health; private attachments never fall back
to an untrusted Provider.

Attachments are content-addressed below `runtime/chat/attachments/`. Local
parsers extract bounded text from PDF, DOCX, XLSX, PPTX and text/code formats;
images are verified locally and GIF contributes only its first frame to a
vision request. Audio is duration-checked where metadata permits and requires a
configured STT model before entering a conversation. None of these inputs can
be promoted to `Data/`, persona evidence or graph facts without a separate,
explicit memory workflow.

Agent runs use a second durable state machine and never write technical task
history into immersive chat context:

```text
queued -> planning -> running -> awaiting_approval
       -> running -> succeeded | failed | cancelled
```

The executor accepts only registered JSON-schema-shaped calls. Real paths are
resolved before execution and must remain inside Project Snow or an explicitly
authorized root. Read and scoped writes can run automatically; external writes,
system changes and destructive operations require approval, with destructive
operations requiring a second confirmation. Every step stores only bounded
input/output summaries. User-visible execution summaries are not hidden
reasoning, and the character-rendering layer may not alter tool facts, numbers
or paths. Web and attachment content is always untrusted data.

The connector boundary exposes IMAP search, SMTP drafts/sending, calendar
read/write, and WebDAV/Graph/Google-style cloud file operations through an
explicit connector id. OAuth-capable connectors use a local PKCE callback;
access/refresh tokens remain in the OS credential vault. SQLite keeps only
connector type, account label, non-secret endpoint metadata and an opaque
credential reference. Connector writes are `external_write` (or
`destructive` for cloud deletion), so a model cannot silently send, publish,
upload, modify a calendar or delete remote data.

Browser automation is a single bounded Playwright action rather than an
unrestricted browser handle. Public navigation, extraction, filling and
download are schema-checked; login, upload, final submit, purchase, publish,
share and delete actions are risk-gated. Page content is untrusted and the URL
passes the same public-host/SSRF guard as web research.

PDFs with no extractable text are flagged `vision_required` and only a small
downscaled page sample is rendered locally for a Provider explicitly trusted
for both documents and images. Audio transcriptions can be edited before the
turn is sent; document excerpts are indexed for the current session only.

## C: two-layer knowledge graph

- Deterministic edges are built only from explicit manifest fields and enter the graph as `review_status=verified`.
- Narrative relation extraction produces `review_status=pending_review` candidates with evidence, confidence and a restricted relation vocabulary. Such edges are excluded from graph retrieval until a human reviewer approves them or the calibrated automation gate admits them.
- An optional independent OpenAI-compatible second-review pipeline evaluates the proposed triple against its original evidence without receiving the extractor rationale or confidence. It writes advisory reports only; deterministic evidence checks, the second model, and browser triage never mutate a candidate or graph edge.
- Qwen Batch automation uses fixed-seed, stratified human calibration. Admission remains disabled per decision category until its quota, accuracy threshold and zero-critical-error rule pass. Machine-created graph artifacts carry `model_approved_audited` provenance and a run ID, and can be rolled back without changing human decisions.
- Neo4j is a serving projection. JSONL node/edge files are the portable, versionable graph source of truth.

## Non-negotiable role rule

The current user is always the Analyst (分析员), not an independently configurable persona. Immersive and assistant modes alter task behavior only; they never remove or replace the character-to-Analyst relationship. The retrieval API returns this invariant explicitly, and generation enforces it server-side. Assistant task results remain structurally visible so character style cannot corrupt factual output.
