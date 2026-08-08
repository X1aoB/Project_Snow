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

## v0.5.0 dual-surface multimodal Agent boundary

The browser product has a stable selector at `/`, a deep ice-blue immersive
surface at `/immersive/`, a white/cobalt Agent surface at `/assistant/`, and a
fixed-sidebar internal workspace at `/workspace/`. Immersive and assistant
messages are queried and summarized separately. Only verified relationship,
address, costume context and world state are shared; task traces and ordinary
technical conversation never enter immersive generation context.

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
- Narrative relation extraction produces `review_status=pending_review` candidates with evidence, confidence and a restricted relation vocabulary. Such edges are excluded from graph retrieval until a reviewer approves them.
- An optional independent OpenAI-compatible second-review pipeline evaluates the proposed triple against its original evidence without receiving the extractor rationale or confidence. It writes advisory reports only; deterministic evidence checks, the second model, and browser triage never mutate a candidate or graph edge.
- Quality calibration uses fixed-seed, stratified human samples from the model-suggested pool. A future batch-admission policy may be considered only after its measured error rate is acceptable; it is not part of the current graph-write path.
- Neo4j is a serving projection. JSONL node/edge files are the portable, versionable graph source of truth.

## Non-negotiable role rule

The current user is always the Analyst (分析员), not an independently configurable persona. Immersive and assistant modes alter task behavior only; they never remove or replace the character-to-Analyst relationship. The retrieval API returns this invariant explicitly, and generation enforces it server-side. Assistant task results remain structurally visible so character style cannot corrupt factual output.
