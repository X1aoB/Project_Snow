"""Typed wire contracts for the public, registration-free API."""

from __future__ import annotations

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
    type: Literal["message", "speech", "action"]
    text: str = Field(min_length=1, max_length=2000)


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
    allowed = {"message"} if communication_channel == "text" else {"speech", "action"}
    if any(block.type not in allowed for block in blocks):
        raise ValueError("content block type does not match communication_channel")
    rendered = "\n".join(block.text.strip() for block in blocks if block.text.strip()).strip()
    if not rendered:
        raise ValueError("content_blocks must contain text")
    if len(rendered) > 2000:
        raise ValueError("content_blocks may contain at most 2000 characters")
    return blocks, rendered


class HistoryTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=2000)
    communication_channel: Literal["text", "in_person"] = "text"
    content_blocks: list[ContentBlock] = Field(default_factory=list, max_length=8)

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
    request_stage: str = Field(default="", max_length=80)
    error_code: str = Field(default="", max_length=80)
    degraded_services: list[str] = Field(default_factory=list, max_length=8)
    ui_surface: str = Field(default="immersive-web", max_length=80)


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
    state_scope: Literal["session_simulation", "conversation_confirmed"] = "session_simulation"


class StateEvent(StrictModel):
    event_id: str = Field(min_length=8, max_length=160)
    event_type: Literal["presence_transition", "arrival", "communication"]
    character_id: str = Field(min_length=12, max_length=32)
    communication_channel: Literal["text", "in_person"]
    location: str | None = Field(default=None, max_length=120)
    arrival_decision: Literal["noticed", "unnoticed"] | None = None


class StatePayload(StrictModel):
    schema_version: Literal["public-state-2"] = "public-state-2"
    data_version: str
    revision: int = Field(default=0, ge=0)
    analyst_location: str | None = Field(default=None, max_length=120)
    presence: dict[str, PresenceState] = Field(default_factory=dict)
    relationships: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[StateEvent] = Field(default_factory=list, max_length=4)
