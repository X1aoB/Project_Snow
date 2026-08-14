"""Typed wire contracts for the public, registration-free API."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


class HistoryTurn(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class ChatRequest(StrictModel):
    request_id: UUID
    provider: str = Field(min_length=1, max_length=24)
    credential: str = Field(min_length=20, max_length=8192)
    model: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=12, max_length=32)
    message: str = Field(min_length=1, max_length=2000)
    recent_history: list[HistoryTurn] = Field(default_factory=list, max_length=24)
    history_summary: str = Field(default="", max_length=6000)
    state_package: str = Field(default="", max_length=16384)

    @field_validator("recent_history")
    @classmethod
    def bounded_twelve_rounds(cls, value: list[HistoryTurn]) -> list[HistoryTurn]:
        if len(value) > 24:
            raise ValueError("recent_history may contain at most 12 rounds")
        return value


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


class StatePayload(StrictModel):
    schema_version: str = "public-state-1"
    data_version: str
    revision: int = Field(default=0, ge=0)
    relationships: dict[str, Any] = Field(default_factory=dict)
    world: dict[str, Any] = Field(default_factory=dict)
