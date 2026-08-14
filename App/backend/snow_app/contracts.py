"""Versioned API contracts for inspection and later conversation layers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    character_id: str | None = None
    # ``chat`` is retained as a backwards-compatible alias for older clients;
    # the browser and new callers use the two explicit conversation modes.
    mode: Literal["immersive", "assistant", "chat"] = "immersive"
    limit: int = Field(default=8, ge=1, le=20)


class Citation(BaseModel):
    document_id: str
    page_id: str
    title: str
    source_type: str
    canonical_url: str | None = None
    local_path: str | None = None
    source_license: str | None = None


class RetrievalHit(BaseModel):
    citation: Citation
    text: str
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None
    metadata: dict[str, Any]


class ConversationIdentity(BaseModel):
    """A fixed project invariant rather than a field supplied by the caller."""

    user_role: Literal["分析员"] = "分析员"
    policy: str = "当前用户即分析员；聊天与助手模式均不得改变角色与分析员的既有叙事关系。"


class RetrievalResponse(BaseModel):
    query: str
    mode: Literal["immersive", "assistant", "chat"]
    character_id: str | None
    conversation_identity: ConversationIdentity
    fusion: Literal["rrf", "lexical_only"]
    vector_available: bool
    results: list[RetrievalHit]


class AnalystContentBlock(BaseModel):
    """One analyst-authored block for a chat turn.

    ``message`` remains the backwards-compatible plain-text request field.
    The optional blocks let the in-person client distinguish a spoken line
    from an explicitly authored analyst action without granting the model the
    ability to invent actions on the analyst's behalf.
    """

    type: Literal["speech", "action", "message"]
    text: str = Field(min_length=1, max_length=1200)


class ModelOverride(BaseModel):
    provider_id: str = Field(min_length=1, max_length=120)
    model_name: str = Field(min_length=1, max_length=200)


class PersonaPairingRequest(BaseModel):
    label: str = Field(default="Codex", min_length=1, max_length=120)
    default_character_id: str | None = Field(default=None, max_length=120)


class ProviderConfigRequest(BaseModel):
    provider_id: str | None = Field(default=None, max_length=120)
    display_name: str = Field(min_length=1, max_length=160)
    kind: Literal["openai", "dashscope", "zhipu", "deepseek", "moonshot", "openai-compatible"]
    base_url: str = Field(min_length=8, max_length=1000)
    api_key: str = Field(default="", max_length=4000, repr=False)
    enabled: bool = True
    trusted_data_types: list[Literal["text", "image", "audio", "document", "account_data"]] = Field(default_factory=lambda: ["text"], max_length=5)
    config: dict[str, Any] = Field(default_factory=dict)


class ProviderProbeRequest(BaseModel):
    model_name: str = Field(min_length=1, max_length=200)
    capabilities: dict[str, bool | int | float | None] = Field(default_factory=dict)
    quality_score: float = Field(default=0, ge=0, le=100)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)


class ModelDefaultsRequest(BaseModel):
    text: ModelOverride | None = None
    immersive_text: ModelOverride | None = None
    assistant_text: ModelOverride | None = None
    assistant_agent: ModelOverride | None = None
    vision: ModelOverride | None = None
    speech_to_text: ModelOverride | None = None
    text_to_speech: ModelOverride | None = None


class VoicePreviewRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=120)
    text: str = Field(default="分析员，我在。", min_length=1, max_length=500)


class AgentRunRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=120)
    task: str = Field(min_length=1, max_length=12000)
    session_id: str | None = Field(default=None, max_length=160)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    model_override: ModelOverride | None = None
    authorized_roots: list[str] = Field(default_factory=list, max_length=20)
    client_run_id: str | None = Field(default=None, min_length=8, max_length=160)
    thinking_mode: Literal["auto", "off", "on"] = "auto"


class AgentApprovalRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=2000)


class ConnectorConfigRequest(BaseModel):
    connector_id: str | None = Field(default=None, max_length=120)
    connector_type: Literal["imap_smtp", "caldav", "webdav", "microsoft_graph", "google"]
    account_label: str = Field(min_length=1, max_length=200)
    secret: str = Field(default="", max_length=8000, repr=False)
    config: dict[str, Any] = Field(default_factory=dict)


class AttachmentTranscriptionRequest(BaseModel):
    transcript: str | None = Field(default=None, max_length=100_000)
    model_override: ModelOverride | None = None


class ConnectorOAuthStartRequest(BaseModel):
    connector_id: str = Field(min_length=1, max_length=120)
    redirect_uri: str | None = Field(default=None, max_length=1000)


class ConnectorOAuthCallbackRequest(BaseModel):
    connector_id: str = Field(min_length=1, max_length=120)
    code: str = Field(min_length=1, max_length=10000)
    state: str = Field(min_length=16, max_length=200)


class MVPChatRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=120)
    message: str = Field(default="", max_length=12000)
    session_id: str | None = Field(default=None, max_length=160)
    # Character chat histories stay isolated, while this ID keeps the
    # present-time world scene consistent when the user switches characters.
    world_session_id: str | None = Field(default=None, max_length=160)
    # Immersive companionship is the safe/default mode.  Assistant mode is an
    # explicit opt-in and is isolated at the session boundary.
    mode: Literal["immersive", "assistant"] = "immersive"
    communication_channel: Literal["in_person", "text"] | None = None
    presence_action: Literal["join_character"] | None = None
    costume_context: str | None = Field(default=None, max_length=240)
    # Optional idempotency key generated by the client. Older clients can omit
    # it and retain the pre-persistence request behavior.
    client_message_id: str | None = Field(default=None, min_length=8, max_length=160)
    # Optional structured analyst input.  The backend validates that an
    # ``action`` can only be submitted in an in-person turn; older clients
    # simply omit this field and continue to send the ``message`` string.
    analyst_content_blocks: list[AnalystContentBlock] = Field(default_factory=list, max_length=8)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    model_override: ModelOverride | None = None
    voice_reply: bool = False
    agent_mode: bool = False
    thinking_mode: Literal["auto", "off", "on"] = "auto"
    limit: int = Field(default=8, ge=1, le=12)
    attachment_transcripts: dict[str, str] = Field(default_factory=dict, max_length=10)

    @model_validator(mode="after")
    def validate_multimodal_turn(self) -> "MVPChatRequest":
        if not self.message.strip() and not self.attachment_ids and not self.analyst_content_blocks:
            raise ValueError("消息或附件至少需要提供一项。")
        if self.agent_mode and self.mode != "assistant":
            raise ValueError("Agent 执行仅在角色助手模式可用。")
        return self


class MVPPresenceResolveRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=120)
    world_session_id: str | None = Field(default=None, max_length=160)


class MVPPresenceTransitionRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=120)
    session_id: str | None = Field(default=None, max_length=160)
    world_session_id: str | None = Field(default=None, max_length=160)
    target_channel: Literal["in_person", "text"]
    action: Literal["join_character", "open_communicator"]

    @model_validator(mode="after")
    def validate_transition_action(self) -> "MVPPresenceTransitionRequest":
        expected = "join_character" if self.target_channel == "in_person" else "open_communicator"
        if self.action != expected:
            raise ValueError("场景动作与目标交流媒介不匹配。")
        return self


class MVPPresenceArrivalRequest(BaseModel):
    character_id: str = Field(min_length=1, max_length=120)
    arrival_id: str = Field(min_length=8, max_length=160)
    session_id: str | None = Field(default=None, max_length=160)
    world_session_id: str | None = Field(default=None, max_length=160)


class MVPFeedbackRequest(BaseModel):
    character_id: str | None = Field(default=None, min_length=1, max_length=120)
    session_id: str | None = Field(default=None, min_length=1, max_length=160)
    message_id: str | None = Field(default=None, max_length=160)
    scope: Literal["product", "conversation", "message"] | None = None
    ui_surface: Literal["landing", "immersive", "assistant", "workspace"] | None = None
    selected_options: list[str] = Field(default_factory=list, max_length=10)
    category: Literal[
        "character_portrayal",
        "knowledge_memory",
        "conversation_experience",
        "client_function",
        "other",
    ] | None = None
    free_text: str = Field(default="", max_length=4000)
    mode: Literal["immersive", "assistant"] | None = None
    communication_channel: Literal["in_person", "text"] | None = None
    registry_version: str | None = Field(default=None, max_length=80)
    client_version: str | None = Field(default=None, max_length=80)
    message_excerpt: str = Field(default="", max_length=1200)
    answer_excerpt: str = Field(default="", max_length=1800)
    agent_run_id: str | None = Field(default=None, max_length=160)
    actual_model: dict[str, Any] = Field(default_factory=dict)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    failed_stage: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_feedback_scope(self) -> "MVPFeedbackRequest":
        effective_scope = self.scope or ("message" if self.message_id else "conversation")
        if effective_scope in {"conversation", "message"} and (
            not self.character_id or not self.session_id
        ):
            raise ValueError("会话或消息反馈必须包含角色和会话标识。")
        if effective_scope == "message" and not self.message_id:
            raise ValueError("消息反馈必须包含消息标识。")
        return self


class MVPFeedbackTriageRequest(BaseModel):
    status: Literal["pending_triage", "planned", "resolved", "ignored"]
    note: str = Field(default="", max_length=2000)


class MVPFeedbackIssueStatusRequest(BaseModel):
    status: Literal[
        "open",
        "planned",
        "needs_verification",
        "fixed_verified",
        "not_reproduced",
        "duplicate",
        "superseded_by_architecture",
    ]
    note: str = Field(default="", max_length=2000)
    verification_tests: list[str] = Field(default_factory=list, max_length=30)
    code_version: str | None = Field(default=None, max_length=120)


class GraphEdge(BaseModel):
    edge_id: str
    from_id: str
    relation_type: str
    to_id: str
    evidence_page_ids: list[str]
    confidence: str
    review_status: str
    source_types: list[str] = Field(default_factory=list)
    narrative_scope: Literal["stable", "situational", "costume_specific", "unknown"] = "unknown"


class GraphNeighborhood(BaseModel):
    node: dict[str, Any]
    edges: list[GraphEdge]
    adjacent_nodes: list[dict[str, Any]]


class RelationReviewDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewer_id: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    from_node_id: str | None = Field(default=None, max_length=240)
    to_node_id: str | None = Field(default=None, max_length=240)


class EntityNodeReviewDecision(BaseModel):
    """Human decision for a proposed location/event graph node."""

    decision: Literal["approved", "rejected"]
    reviewer_id: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)


class ReviewAutomationRunRequest(BaseModel):
    """Explicitly confirmed creation of a paid or no-charge batch review run."""

    mode: Literal["test", "calibration", "production"] = "calibration"
    estimate_hash: str = Field(min_length=16, max_length=128)
    calibration_run_id: str | None = Field(default=None, max_length=160)
    confirmation: Literal["submit_qwen_batch"]


class ReviewAutomationCalibrationLabel(BaseModel):
    correct: bool
    critical_error: bool = False
    error_category: Literal[
        "none",
        "identity_confusion",
        "wrong_node_type",
        "context_contamination",
        "fabricated_quote",
        "wrong_relation",
        "other",
    ] = "none"
    reviewer_id: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)


class ReviewAutomationAction(BaseModel):
    confirmation: Literal["apply_machine_decisions", "rollback_machine_decisions"]


class DeepSeekReviewCompletionRunRequest(BaseModel):
    """Create and optionally start a no-price-gate unresolved-review run."""

    selection_hash: str | None = Field(default=None, min_length=16, max_length=128)
    concurrency: int = Field(default=12, ge=1, le=128)
    start_immediately: bool = True
    confirmation: Literal["submit_deepseek_completion"]


class DeepSeekReviewCompletionAction(BaseModel):
    confirmation: Literal[
        "resume_deepseek_completion",
        "apply_deepseek_decisions",
        "rollback_deepseek_decisions",
    ]
    concurrency: int = Field(default=12, ge=1, le=128)


class StageLockResponse(BaseModel):
    code: Literal["conversation_stage_locked"]
    message: str
    required_stages: list[str]
    conversation_identity: ConversationIdentity
