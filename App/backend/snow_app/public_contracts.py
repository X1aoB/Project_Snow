"""Typed wire contracts for the public, registration-free API."""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ByokSessionRequest(StrictModel):
    provider: str = Field(min_length=1, max_length=24)
    api_key: str = Field(min_length=8, max_length=4096)
    turnstile_token: str = Field(default="", max_length=4096)
    accepted_transit_notice: bool
    accepted_cost_notice: bool
    accepted_local_history_notice: bool


class ModelDiscoveryRequest(StrictModel):
    provider: str = Field(min_length=1, max_length=24)
    credential: str = Field(min_length=20, max_length=8192)
    request_id: UUID


class ContentBlock(StrictModel):
    """A renderable public message block.

    Stickers deliberately carry an opaque manifest id rather than a client
    supplied URL or filename.  The server resolves the id against the signed
    media release before it is rendered or passed to a model.
    """

    type: Literal["message", "speech", "action", "sticker"]
    text: str = Field(default="", max_length=2000)
    asset_id: str = Field(default="", max_length=64)
    caption: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_shape(self) -> "ContentBlock":
        if self.type == "sticker":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{5,63}", self.asset_id or ""):
                raise ValueError("sticker asset_id is invalid")
            self.text = ""
            return self
        if not self.text.strip():
            raise ValueError("text is required for non-sticker blocks")
        self.asset_id = ""
        self.caption = ""
        return self


def _normalize_blocks(
    communication_channel: Literal["text", "in_person"],
    blocks: list[ContentBlock],
    fallback_text: str,
) -> tuple[list[ContentBlock], str]:
    if not blocks:
        if not fallback_text.strip():
            raise ValueError("message or content_blocks is required")
        default_type = "message" if communication_channel == "text" else "speech"
        blocks = [ContentBlock(type=default_type, text=fallback_text)]
    allowed = {"message", "sticker"} if communication_channel == "text" else {"speech", "action"}
    if any(block.type not in allowed for block in blocks):
        raise ValueError("content block type does not match communication_channel")
    if sum(1 for block in blocks if block.type == "sticker") > 1:
        raise ValueError("a message may contain at most one sticker")
    sticker_indexes = [index for index, block in enumerate(blocks) if block.type == "sticker"]
    if sticker_indexes and sticker_indexes[-1] != len(blocks) - 1:
        raise ValueError("sticker must follow the message text")
    rendered = "\n".join(block.text.strip() for block in blocks if block.text.strip()).strip()
    if not rendered and not any(block.type == "sticker" for block in blocks):
        raise ValueError("content_blocks must contain text")
    if len(rendered) > 2000:
        raise ValueError("content_blocks may contain at most 2000 characters")
    return blocks, rendered


class HistoryTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=2000)
    communication_channel: Literal["text", "in_person"] = "text"
    content_blocks: list[ContentBlock] = Field(default_factory=list, max_length=8)
    created_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def normalize_content(self) -> "HistoryTurn":
        blocks, rendered = _normalize_blocks(
            self.communication_channel,
            self.content_blocks,
            self.content,
        )
        self.content_blocks = blocks
        self.content = rendered
        return self


class ChatRequest(StrictModel):
    request_id: UUID
    provider: str = Field(min_length=1, max_length=24)
    credential: str = Field(min_length=20, max_length=8192)
    model: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=12, max_length=32)
    message: str = Field(default="", max_length=2000)
    communication_channel: Literal["text", "in_person"] = "text"
    content_blocks: list[ContentBlock] = Field(default_factory=list, max_length=8)
    recent_history: list[HistoryTurn] = Field(default_factory=list, max_length=24)
    history_summary: str = Field(default="", max_length=6000)
    state_package: str = Field(default="", max_length=32768)
    continuity_decision: Literal["", "continue_previous", "start_today"] = ""
    local_day_key: str = Field(default="", max_length=32)

    @field_validator("recent_history")
    @classmethod
    def bounded_twelve_rounds(cls, value: list[HistoryTurn]) -> list[HistoryTurn]:
        if len(value) > 24:
            raise ValueError("recent_history may contain at most 12 rounds")
        return value

    @model_validator(mode="after")
    def normalize_message(self) -> "ChatRequest":
        blocks, rendered = _normalize_blocks(
            self.communication_channel,
            self.content_blocks,
            self.message,
        )
        self.content_blocks = blocks
        self.message = rendered
        return self


class SummarizeRequest(StrictModel):
    request_id: UUID
    provider: str = Field(min_length=1, max_length=24)
    credential: str = Field(min_length=20, max_length=8192)
    model: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=12, max_length=32)
    turns: list[HistoryTurn] = Field(min_length=2, max_length=24)
    previous_summary: str = Field(default="", max_length=6000)


class FeedbackRequest(StrictModel):
    request_id: UUID
    chat_request_id: UUID | None = None
    body: str = Field(min_length=1, max_length=1000)
    turnstile_token: str = Field(default="", max_length=4096)
    qq: str = Field(default="", max_length=12, pattern=r"^$|^[0-9]{5,12}$")
    character_id: str = Field(default="", max_length=32)
    provider: str = Field(default="", max_length=24)
    model: str = Field(default="", max_length=200)
    user_message: str = Field(default="", max_length=2000)
    assistant_answer: str = Field(default="", max_length=1200)
    user_content_blocks: list[ContentBlock] = Field(default_factory=list, max_length=8)
    assistant_content_blocks: list[ContentBlock] = Field(default_factory=list, max_length=8)
    request_stage: str = Field(default="", max_length=80)
    error_code: str = Field(default="", max_length=80)
    degraded_services: list[str] = Field(default_factory=list, max_length=8)
    ui_surface: str = Field(default="immersive-web", max_length=80)
    include_conversation_context: bool = True

    @model_validator(mode="after")
    def bound_feedback_blocks(self) -> "FeedbackRequest":
        if not self.include_conversation_context:
            self.chat_request_id = None
            self.character_id = ""
            self.provider = ""
            self.model = ""
            self.user_message = ""
            self.assistant_answer = ""
            self.user_content_blocks = []
            self.assistant_content_blocks = []
            self.request_stage = ""
            self.error_code = ""
            self.degraded_services = []
        user_length = sum(len(block.text) for block in self.user_content_blocks)
        assistant_length = sum(len(block.text) for block in self.assistant_content_blocks)
        if user_length > 2000:
            raise ValueError("user_content_blocks may contain at most 2000 characters")
        if assistant_length > 1200:
            raise ValueError("assistant_content_blocks may contain at most 1200 characters")
        return self


class PresenceResolveRequest(StrictModel):
    request_id: UUID
    character_id: str = Field(min_length=12, max_length=32)
    state_package: str = Field(default="", max_length=32768)


class PresenceTransitionRequest(StrictModel):
    request_id: UUID
    character_id: str = Field(min_length=12, max_length=32)
    target_channel: Literal["text", "in_person"]
    action: Literal["join_character", "open_communicator"]
    state_package: str = Field(default="", max_length=32768)

    @model_validator(mode="after")
    def validate_action(self) -> "PresenceTransitionRequest":
        expected = "join_character" if self.target_channel == "in_person" else "open_communicator"
        if self.action != expected:
            raise ValueError("presence action does not match target channel")
        return self


class PresenceArrivalRequest(StrictModel):
    arrival_id: UUID
    provider: str = Field(min_length=1, max_length=24)
    credential: str = Field(min_length=20, max_length=8192)
    model: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=12, max_length=32)
    recent_history: list[HistoryTurn] = Field(default_factory=list, max_length=24)
    history_summary: str = Field(default="", max_length=6000)
    state_package: str = Field(default="", max_length=32768)

    @field_validator("recent_history")
    @classmethod
    def bounded_twelve_rounds(cls, value: list[HistoryTurn]) -> list[HistoryTurn]:
        if len(value) > 24:
            raise ValueError("recent_history may contain at most 12 rounds")
        return value


class PresenceState(StrictModel):
    character_id: str = Field(min_length=12, max_length=32)
    character_name: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=120)
    activity: str = Field(min_length=1, max_length=240)
    state_scope: Literal["session_simulation", "conversation_confirmed", "shared_daily"] = "session_simulation"


class StateEvent(StrictModel):
    event_id: str = Field(min_length=8, max_length=160)
    event_type: Literal["presence_transition", "arrival", "communication", "joint_movement"]
    character_id: str = Field(min_length=12, max_length=32)
    communication_channel: Literal["text", "in_person"]
    location: str | None = Field(default=None, max_length=120)
    arrival_decision: Literal["noticed", "unnoticed"] | None = None
    location_id: str | None = Field(default=None, max_length=64)
    activity_id: str | None = Field(default=None, max_length=64)
    target_character_id: str | None = Field(default=None, min_length=12, max_length=32)


class StateUpdateProposal(StrictModel):
    """Model-produced state intent; validated before it can touch a package."""

    type: Literal["joint_move"]
    location_id: str = Field(min_length=1, max_length=64)
    activity_id: str = Field(min_length=1, max_length=64)
    commit: Literal["now"] = "now"


class StatePayload(StrictModel):
    schema_version: Literal["public-state-2"] = "public-state-2"
    data_version: str
    revision: int = Field(default=0, ge=0)
    analyst_location: str | None = Field(default=None, max_length=120)
    presence: dict[str, PresenceState] = Field(default_factory=dict)
    relationships: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[StateEvent] = Field(default_factory=list, max_length=4)
    schedule_date: str = Field(default="", max_length=16)
    schedule_revision: int = Field(default=0, ge=0)
    generated_at: str = Field(default="", max_length=64)
    expires_at: str = Field(default="", max_length=64)
    subject_binding: str = Field(default="", max_length=64)
    state_key_id: str = Field(default="", max_length=32)
