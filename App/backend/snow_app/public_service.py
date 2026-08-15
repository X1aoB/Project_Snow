"""Stateless public chat facade around the evidence-backed immersive engine."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import threading
import tempfile
import time
from typing import Any, Iterator

from .config import PublicSettings, Settings
from .mvp_policy import MVP_CHARACTERS, scene_visual_key
from .mvp_service import MVPService, _SESSION_STATES, _SESSION_LOCK, _WORLD_STATES, _WORLD_STATE_LOCK
from .public_contracts import (
    ChatRequest,
    HistoryTurn,
    PresenceArrivalRequest,
    PresenceResolveRequest,
    PresenceTransitionRequest,
    StateEvent,
    StatePayload,
    SummarizeRequest,
)
from .public_providers import ProviderSpec, simple_completion
from .public_repository import PublicRuntimeRepository
from .public_security import PublicSecurityError, safe_output, sign_state, verify_state


class StatelessConversationStore:
    """Deliberately inert replacement for the internal SQLite chat store."""

    def duplicate_response(self, _request_key: str | None) -> None:
        return None

    def claim_request(self, _request_key: str, _character_id: str) -> bool:
        return True

    def release_request(self, _request_key: str | None) -> None:
        return None

    def session_state(self, _session_id: str) -> None:
        return None

    def world_state(self, _world_session_id: str) -> None:
        return None


class GenerationBusy(RuntimeError):
    pass


class CharacterUnavailable(RuntimeError):
    pass


_PUBLIC_WHOLE_ANSWER_FALLBACKS = frozenset(
    {
        "immersive_boundary_fallback",
        "live_scene_guard",
        "companion_social_guard",
        "fenny_voice_guard",
        "communication_guard",
        "analyst_premise_guard",
        "session_premise_guard",
        "casual_state_guard",
        "current_food_guard",
        "shared_meal_guard",
        "routine_activity_guard",
        "open_invitation_guard",
        "signature_frequency_guard",
        "cross_character_guard",
        "dual_persona_guard",
        "direct_answer_guard",
        "scene_privacy_guard",
        "continuity_guard",
        "interaction_hint_guard",
        "natural_dialogue_guard",
        "plot_recap_guard",
        "mechanical_dialogue_guard",
        "visit_location_guard",
        "repetition_guard",
        "logistics_evidence_fallback",
        "relationship_roster_guard",
        "empty_model_output_guard",
    }
)


class GenerationGate:
    def __init__(self, active: int = 4, queued: int = 8, queue_timeout: float = 30):
        self.semaphore = asyncio.Semaphore(active)
        self.queued_limit = queued
        self.queue_timeout = queue_timeout
        self._lock = asyncio.Lock()
        self._waiting = 0

    @contextmanager
    def _noop(self) -> Iterator[None]:
        yield

    async def run(self, callback):
        async with self._lock:
            if self.semaphore.locked() and self._waiting >= self.queued_limit:
                raise GenerationBusy("generation_queue_full")
            self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=self.queue_timeout)
            except TimeoutError as exc:
                raise GenerationBusy("generation_queue_timeout") from exc
        finally:
            async with self._lock:
                self._waiting = max(0, self._waiting - 1)
        try:
            return await callback()
        finally:
            self.semaphore.release()


def _history_turns(history: list[HistoryTurn]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    pending_user: HistoryTurn | None = None
    for item in history[-24:]:
        if item.role == "user":
            pending_user = item
        elif pending_user:
            turns.append(
                {
                    "user": pending_user.content,
                    "assistant": item.content,
                    "communication_channel": item.communication_channel,
                    "analyst_content_blocks": [
                        block.model_dump() for block in pending_user.content_blocks
                    ],
                    "content_blocks": [block.model_dump() for block in item.content_blocks],
                    "mode": "immersive",
                }
            )
            pending_user = None
    return turns[-12:]


def _trim_content_blocks(
    blocks: list[dict[str, str]],
    *,
    limit: int = 1200,
) -> tuple[list[dict[str, str]], bool]:
    remaining = limit
    trimmed: list[dict[str, str]] = []
    truncated = False
    for block in blocks:
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        separator_cost = 1 if trimmed else 0
        available = max(0, remaining - separator_cost)
        if available <= 0:
            truncated = True
            break
        if len(text) > available:
            text = text[:available]
            truncated = True
        trimmed.append({"type": str(block["type"]), "text": text})
        remaining -= len(text) + separator_cost
        if truncated:
            break
    return trimmed, truncated


def _render_blocks(blocks: list[dict[str, str]]) -> str:
    return "\n".join(str(block.get("text") or "").strip() for block in blocks).strip()


class PublicChatService:
    def __init__(self, settings: Settings, public_settings: PublicSettings):
        self.public_settings = public_settings
        self.repository = PublicRuntimeRepository(settings, public_settings)
        # Pass the inert store at construction time: the public process never
        # opens, reads, or writes the internal SQLite conversation database.
        ephemeral_database = (
            Path(tempfile.gettempdir())
            / "project-snow-public"
            / str(os.getpid())
            / "conversations.sqlite3"
        )
        self.mvp = MVPService(
            settings,
            self.repository,
            force_chat_enabled=True,
            conversation_database_path=ephemeral_database,
            conversation_store=StatelessConversationStore(),
        )
        self.gate = GenerationGate()
        self._state_cleanup_lock = threading.RLock()

    def close(self) -> None:
        self.mvp.close()
        self.repository.close()

    def characters(self) -> list[dict[str, Any]]:
        avatars = self.mvp._avatar_manifest()
        result = []
        for character in MVP_CHARACTERS:
            avatar = avatars.get(character.character_id) or {}
            result.append(
                {
                    "character_id": character.character_id,
                    "display_name": character.display_name,
                    "aliases": list(character.aliases),
                    # Wiki-derived media never enters the GPL application
                    # image. A separately licensed content package may add a
                    # public_asset_path after its provenance checks pass.
                    "avatar": avatar.get("public_asset_path") if avatar.get("public_asset_approved") else None,
                    "source_page": avatar.get("source_page"),
                    "license": "CC BY-NC-SA; version unspecified by source",
                }
            )
        return result

    def _default_state(self, subject_hash: str) -> dict[str, Any]:
        world_id = "public_state_" + sha256(subject_hash.encode()).hexdigest()[:20]
        world = self.mvp._world_snapshot(world_id)
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=0,
            analyst_location=world.get("analyst_location"),
            presence=dict(world.get("presence") or {}),
            relationships={},
            recent_events=[],
        ).model_dump()

    def _normalized_state(self, token: str, subject_hash: str) -> dict[str, Any]:
        raw = verify_state(self.public_settings, token)
        defaults = self._default_state(subject_hash)
        if not raw:
            return defaults

        schema_version = str(raw.get("schema_version") or "public-state-1")
        if schema_version not in {"public-state-1", "public-state-2"}:
            raise PublicSecurityError("Public state schema is not supported")
        if schema_version == "public-state-1":
            old_world = raw.get("world") if isinstance(raw.get("world"), dict) else {}
            incoming_presence = old_world.get("presence") or {}
            analyst_location = old_world.get("analyst_location")
            recent_events: list[dict[str, Any]] = []
        else:
            incoming_presence = raw.get("presence") or {}
            analyst_location = raw.get("analyst_location")
            recent_events = list(raw.get("recent_events") or [])[-4:]

        canonical = {character.character_id: character for character in MVP_CHARACTERS}
        presence: dict[str, dict[str, Any]] = {}
        for character_id, character in canonical.items():
            fallback = dict(defaults["presence"][character_id])
            candidate = (
                dict(incoming_presence.get(character_id) or {})
                if isinstance(incoming_presence, dict)
                else {}
            )
            presence[character_id] = {
                "character_id": character_id,
                "character_name": character.display_name,
                "location": str(candidate.get("location") or fallback["location"])[:120],
                "activity": str(candidate.get("activity") or fallback["activity"])[:240],
                "state_scope": (
                    "conversation_confirmed"
                    if candidate.get("state_scope") == "conversation_confirmed"
                    else "session_simulation"
                ),
            }

        validated_events: list[dict[str, Any]] = []
        for event in recent_events:
            try:
                validated_events.append(StateEvent.model_validate(event).model_dump())
            except (TypeError, ValueError):
                continue
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=max(0, int(raw.get("revision") or 0)),
            analyst_location=str(analyst_location)[:120] if analyst_location else None,
            presence=presence,
            relationships=dict(raw.get("relationships") or {}),
            recent_events=validated_events[-4:],
        ).model_dump()

    @staticmethod
    def _scene_state(state: dict[str, Any], character_id: str) -> dict[str, Any]:
        scene = dict((state.get("presence") or {}).get(character_id) or {})
        analyst_location = str(state.get("analyst_location") or "").strip() or None
        character_location = str(scene.get("location") or "").strip() or None
        return {
            "analyst_location": analyst_location,
            "character_location": character_location,
            "character_activity": scene.get("activity"),
            "visual_key": scene_visual_key(character_location),
            "co_located": bool(
                analyst_location
                and character_location
                and analyst_location == character_location
            ),
            "state_scope": str(scene.get("state_scope") or "session_simulation"),
        }

    def _state_with_event(
        self,
        state: dict[str, Any],
        *,
        event: StateEvent,
        analyst_location: str | None = None,
        world_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        presence = dict(state.get("presence") or {})
        if world_snapshot:
            for character_id, scene in (world_snapshot.get("presence") or {}).items():
                if character_id not in presence:
                    continue
                previous = dict(presence[character_id])
                previous.update(
                    {
                        key: value
                        for key, value in dict(scene).items()
                        if key in {"location", "activity", "state_scope"} and value
                    }
                )
                presence[character_id] = previous
            analyst_location = world_snapshot.get("analyst_location")
        events = [*(state.get("recent_events") or []), event.model_dump()][-4:]
        return StatePayload(
            data_version=self.public_settings.data_version,
            revision=int(state.get("revision") or 0) + 1,
            analyst_location=analyst_location,
            presence=presence,
            relationships=dict(state.get("relationships") or {}),
            recent_events=events,
        ).model_dump()

    def resolve_presence(
        self,
        request: PresenceResolveRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "scene_state": self._scene_state(state, request.character_id),
            "state_package": sign_state(self.public_settings, state),
            "schema_version": "public-state-2",
        }

    def transition_presence(
        self,
        request: PresenceTransitionRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        before = self._scene_state(state, request.character_id)
        next_location = state.get("analyst_location")
        if request.target_channel == "in_person":
            next_location = before.get("character_location")
            if not next_location:
                raise ValueError("character scene has no location")
        event = StateEvent(
            event_id=str(request.request_id),
            event_type="presence_transition",
            character_id=request.character_id,
            communication_channel=request.target_channel,
            location=next_location,
        )
        next_state = self._state_with_event(
            state,
            event=event,
            analyst_location=next_location,
        )
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "communication_channel": request.target_channel,
            "scene_state": self._scene_state(next_state, request.character_id),
            "channel_transition": {
                "status": "applied_immediately",
                "from": "text" if request.target_channel == "in_person" else "in_person",
                "to": request.target_channel,
                "trigger": "presence_ui",
            },
            "presence_transition": {
                "status": (
                    "joined_character"
                    if request.target_channel == "in_person"
                    else "communicator_opened"
                ),
                "location": next_location,
            },
            "state_package": sign_state(self.public_settings, next_state),
            "model_called": False,
        }

    def prepare_presence_arrival(
        self,
        request: PresenceArrivalRequest,
        subject_hash: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        scene = self._scene_state(state, request.character_id)
        location = scene.get("character_location")
        if not location:
            raise ValueError("character scene has no location")
        decision = "noticed" if secrets.randbelow(2) == 0 else "unnoticed"
        next_state = self._state_with_event(
            state,
            event=StateEvent(
                event_id=str(request.arrival_id),
                event_type="arrival",
                character_id=request.character_id,
                communication_channel="in_person",
                location=location,
                arrival_decision=decision,
            ),
            analyst_location=location,
        )
        return {
            "arrival_id": str(request.arrival_id),
            "character_id": request.character_id,
            "communication_channel": "in_person",
            "scene_state": self._scene_state(next_state, request.character_id),
            "decision": decision,
            "status": "completed",
            "reaction": None,
            "state_package": sign_state(self.public_settings, next_state),
            "model_called": False,
            "terminal_error": "",
            "state": next_state,
        }

    @staticmethod
    def failed_presence_arrival(
        prepared: dict[str, Any],
        code: str,
        *,
        model_called: bool,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                **prepared,
                "reaction": None,
                "model_called": model_called,
                "terminal_error": code,
                "diagnostics": diagnostics or {},
            }.items()
            if key != "state"
        }

    async def finish_presence_arrival(
        self,
        prepared: dict[str, Any],
        request: PresenceArrivalRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        async def generate():
            return await asyncio.to_thread(
                self._finish_presence_arrival_sync,
                prepared,
                request,
                subject_hash,
                provider,
                api_key,
            )

        return await self.gate.run(generate)

    def _finish_presence_arrival_sync(
        self,
        prepared: dict[str, Any],
        request: PresenceArrivalRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        total_started = time.perf_counter()
        self.repository.reset_request_health()
        self.mvp.reset_generation_diagnostics()
        synthetic = ChatRequest(
            request_id=request.arrival_id,
            provider=request.provider,
            credential=request.credential,
            model=request.model,
            character_id=request.character_id,
            message="（到场事件）分析员刚刚来到你身边。请主动问候，并自然承接当前角色最近的聊天内容。",
            communication_channel="in_person",
            recent_history=request.recent_history,
            history_summary=request.history_summary,
            state_package=prepared["state_package"],
        )
        with self._request_state(
            synthetic,
            subject_hash,
            state_override=dict(prepared["state"]),
        ) as (session_id, world_id, _state):
            try:
                result = self.mvp.chat(
                    request.character_id,
                    synthetic.message,
                    session_id=session_id,
                    world_session_id=world_id,
                    communication_channel="in_person",
                    analyst_content_blocks=[],
                    model_settings=(provider.base_url, api_key, request.model),
                    model_info={
                        "provider_id": provider.provider_id,
                        "model_name": request.model,
                        "reason": "public_presence_arrival",
                    },
                    thinking_decision={
                        "requested": "off",
                        "effective": "off",
                        "reason": "public_immersive_policy",
                        "provider_kind": provider.provider_id,
                    },
                    max_tokens_override=1600,
                    persist_exchange=False,
                    remember_session=False,
                    presence_arrival=True,
                )
            except Exception as exc:
                from .mvp_service import MVPProviderError

                if not isinstance(exc, MVPProviderError):
                    raise
                return self.failed_presence_arrival(
                    prepared,
                    "provider_request_failed",
                    model_called=True,
                    diagnostics=self._diagnostics(total_started),
                )

        adjustments = list(result.get("response_adjustments") or [])
        rejected_adjustments = sorted(
            set(adjustments).intersection(_PUBLIC_WHOLE_ANSWER_FALLBACKS)
        )
        if rejected_adjustments:
            code = (
                "upstream_invalid_response"
                if "empty_model_output_guard" in rejected_adjustments
                else "role_guard_rejected"
            )
            diagnostics = self._diagnostics(total_started)
            diagnostics["error_stage"] = "generation_validation"
            diagnostics["rejected_adjustments"] = rejected_adjustments
            return self.failed_presence_arrival(
                prepared,
                code,
                model_called=True,
                diagnostics=diagnostics,
            )
        blocks, answer, truncated, safety_category = self._public_generation_content(
            result,
            "in_person",
        )
        if not any(block.get("type") == "speech" for block in blocks):
            return self.failed_presence_arrival(
                prepared,
                "role_guard_rejected",
                model_called=True,
                diagnostics=self._diagnostics(total_started),
            )
        reaction = {
            "message_id": "presence_message_" + sha256(
                f"{subject_hash}\x1f{request.arrival_id}".encode()
            ).hexdigest()[:16],
            "character_id": request.character_id,
            "communication_channel": "in_person",
            "answer": answer,
            "content_blocks": blocks,
            "source": "presence_arrival",
            "arrival_id": str(request.arrival_id),
            "usage": result.get("usage") or {},
            "truncated": truncated,
            "safety_category": safety_category,
        }
        return {
            key: value
            for key, value in {
                **prepared,
                "reaction": reaction,
                "model_called": True,
                "terminal_error": "",
                "diagnostics": self._diagnostics(total_started),
            }.items()
            if key != "state"
        }

    @contextmanager
    def _request_state(
        self,
        request: ChatRequest,
        subject_hash: str,
        *,
        state_override: dict[str, Any] | None = None,
    ) -> Iterator[tuple[str, str, dict[str, Any]]]:
        state = state_override or self._normalized_state(request.state_package, subject_hash)
        session_id = "public_session_" + sha256(
            f"{subject_hash}\x1f{request.request_id}\x1f{request.character_id}".encode()
        ).hexdigest()[:20]
        world_id = "public_world_" + sha256(f"{subject_hash}\x1f{request.request_id}".encode()).hexdigest()[:20]
        turns = _history_turns(request.recent_history)
        session_state = {
            "character_id": request.character_id,
            "mode": "immersive",
            "mode_turns": {"immersive": turns, "assistant": []},
            "turns": turns,
            "premises": (
                [{"kind": "browser_history_summary", "value": request.history_summary[:6000]}]
                if request.history_summary
                else []
            ),
            "style_context": None,
            "communication_channel": request.communication_channel,
            "recent_story_titles": [],
        }
        world_state = {
            "world_session_id": world_id,
            "created_at": "browser_state",
            "analyst_location": state.get("analyst_location"),
            "presence": dict(state.get("presence") or {}),
        }
        if not world_state["presence"]:
            world_state = self.mvp._world_snapshot(world_id)
        with _SESSION_LOCK:
            _SESSION_STATES[session_id] = session_state
        with _WORLD_STATE_LOCK:
            _WORLD_STATES[world_id] = world_state
        try:
            yield session_id, world_id, state
        finally:
            with self._state_cleanup_lock:
                with _SESSION_LOCK:
                    _SESSION_STATES.pop(session_id, None)
                with _WORLD_STATE_LOCK:
                    _WORLD_STATES.pop(world_id, None)

    async def chat(
        self,
        request: ChatRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        async def generate():
            return await asyncio.to_thread(self._chat_sync, request, subject_hash, provider, api_key)

        return await self.gate.run(generate)

    def policy_rejection(
        self,
        request: ChatRequest,
        subject_hash: str,
        provider: ProviderSpec,
        safety_category: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        state = self._normalized_state(request.state_package, subject_hash)
        scene = self._scene_state(state, request.character_id)
        next_state = self._state_with_event(
            state,
            event=StateEvent(
                event_id=str(request.request_id),
                event_type="communication",
                character_id=request.character_id,
                communication_channel=request.communication_channel,
                location=scene.get("analyst_location"),
            ),
            analyst_location=state.get("analyst_location"),
        )
        answer = "抱歉，这个方向我不能继续提供具体指导。我们可以换成安全、合法且不伤害他人的话题。"
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "provider": provider.provider_id,
            "model": request.model,
            "answer": answer,
            "communication_channel": request.communication_channel,
            "content_blocks": [
                {
                    "type": "message" if request.communication_channel == "text" else "speech",
                    "text": answer,
                }
            ],
            "truncated": False,
            "state_package": sign_state(self.public_settings, next_state),
            "degraded_services": [],
            "retrieval": {},
            "usage": {},
            "safety_category": safety_category,
            "generation_outcome": "valid_initial",
            "response_adjustments": [],
            "terminal_error": "",
            "diagnostics": {
                "timings_ms": {"total": 0, "provider_http_calls": 0},
                "dependency_health": {},
            },
        }

    def _chat_sync(
        self,
        request: ChatRequest,
        subject_hash: str,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        if request.character_id not in self.mvp._views():
            raise CharacterUnavailable(request.character_id)
        total_started = time.perf_counter()
        self.repository.reset_request_health()
        self.mvp.reset_generation_diagnostics()
        with self._request_state(request, subject_hash) as (session_id, world_id, prior_state):
            try:
                result = self.mvp.chat(
                    request.character_id,
                    request.message,
                    session_id=session_id,
                    world_session_id=world_id,
                    communication_channel=request.communication_channel,
                    analyst_content_blocks=[
                        block.model_dump() for block in request.content_blocks
                    ],
                    client_message_id=None,
                    model_settings=(provider.base_url, api_key, request.model),
                    model_info={
                        "provider_id": provider.provider_id,
                        "model_name": request.model,
                        "reason": "public_byok",
                    },
                    thinking_decision={
                        "requested": "off",
                        "effective": "off",
                        "reason": "public_immersive_policy",
                        "provider_kind": provider.provider_id,
                    },
                    max_tokens_override=1600,
                    persist_exchange=False,
                    remember_session=False,
                )
            except Exception as exc:
                from .mvp_service import MVPProviderError

                if not isinstance(exc, MVPProviderError):
                    raise
                return self._terminal_result(
                    request,
                    provider,
                    total_started,
                    code="provider_request_failed",
                    error_stage="provider",
                )
            adjustments = list(result.get("response_adjustments") or [])
            rejected_adjustments = sorted(
                set(adjustments).intersection(_PUBLIC_WHOLE_ANSWER_FALLBACKS)
            )
            if rejected_adjustments:
                terminal_error = (
                    "upstream_invalid_response"
                    if "empty_model_output_guard" in rejected_adjustments
                    else "role_guard_rejected"
                )
                return self._terminal_result(
                    request,
                    provider,
                    total_started,
                    code=terminal_error,
                    error_stage="generation_validation",
                    result=result,
                    response_adjustments=adjustments,
                    rejected_adjustments=rejected_adjustments,
                )
            content_blocks, answer, truncated, safety_category = self._public_generation_content(
                result,
                request.communication_channel,
            )
            if not content_blocks or not answer:
                return self._terminal_result(
                    request,
                    provider,
                    total_started,
                    code="upstream_invalid_response",
                    error_stage="generation_validation",
                    result=result,
                    response_adjustments=adjustments,
                )
            world_snapshot = self.mvp._world_snapshot(world_id)
            next_state = self._state_with_event(
                prior_state,
                event=StateEvent(
                    event_id=str(request.request_id),
                    event_type="communication",
                    character_id=request.character_id,
                    communication_channel=request.communication_channel,
                    location=world_snapshot.get("analyst_location"),
                ),
                world_snapshot=world_snapshot,
            )
            request_health = self.repository.request_health()
            degraded = sorted(service for service, status in request_health.items() if status != "ok")
            generation_outcome = (
                "valid_rewrite"
                if "answer_guardrail_retry" in adjustments
                else "normalized"
                if adjustments
                else "valid_initial"
            )
            return {
                "request_id": str(request.request_id),
                "character_id": request.character_id,
                "provider": provider.provider_id,
                "model": request.model,
                "answer": answer,
                "communication_channel": request.communication_channel,
                "content_blocks": content_blocks,
                "truncated": truncated,
                "state_package": sign_state(self.public_settings, next_state),
                "degraded_services": degraded,
                "retrieval": result.get("retrieval") or {},
                "usage": result.get("usage") or {},
                "safety_category": safety_category,
                "generation_outcome": generation_outcome,
                "response_adjustments": adjustments,
                "terminal_error": "",
                "diagnostics": self._diagnostics(total_started),
            }

    @staticmethod
    def _public_generation_content(
        result: dict[str, Any],
        communication_channel: str,
    ) -> tuple[list[dict[str, str]], str, bool, str | None]:
        allowed = {"message"} if communication_channel == "text" else {"speech", "action"}
        raw_blocks: list[dict[str, str]] = []
        for item in result.get("content_blocks") or []:
            if not isinstance(item, dict):
                continue
            block_type = str(item.get("type") or "").casefold()
            text = str(item.get("text") or "").strip()
            if block_type in allowed and text:
                raw_blocks.append({"type": block_type, "text": text})
            if len(raw_blocks) >= 8:
                break
        if not raw_blocks:
            answer = str(result.get("answer") or "").strip()
            default_type = "message" if communication_channel == "text" else "speech"
            if answer:
                raw_blocks = [{"type": default_type, "text": answer}]
        rendered = _render_blocks(raw_blocks)
        safe_answer, safety_category = safe_output(rendered)
        if safety_category:
            default_type = "message" if communication_channel == "text" else "speech"
            raw_blocks = [{"type": default_type, "text": safe_answer}]
        trimmed, truncated = _trim_content_blocks(raw_blocks)
        return trimmed, _render_blocks(trimmed), truncated, safety_category

    def _terminal_result(
        self,
        request: ChatRequest,
        provider: ProviderSpec,
        total_started: float,
        *,
        code: str,
        error_stage: str,
        result: dict[str, Any] | None = None,
        response_adjustments: list[str] | None = None,
        rejected_adjustments: list[str] | None = None,
    ) -> dict[str, Any]:
        result = result or {}
        diagnostics = self._diagnostics(total_started)
        diagnostics["error_stage"] = error_stage
        if rejected_adjustments:
            diagnostics["rejected_adjustments"] = rejected_adjustments
        request_health = self.repository.request_health()
        return {
            "request_id": str(request.request_id),
            "character_id": request.character_id,
            "provider": provider.provider_id,
            "model": request.model,
            "answer": "",
            "communication_channel": request.communication_channel,
            "content_blocks": [],
            "truncated": False,
            "state_package": request.state_package,
            "degraded_services": sorted(
                service for service, service_status in request_health.items() if service_status != "ok"
            ),
            "retrieval": result.get("retrieval") or {},
            "usage": result.get("usage") or {},
            "safety_category": None,
            "generation_outcome": "rejected",
            "response_adjustments": response_adjustments or [],
            "terminal_error": code,
            "diagnostics": diagnostics,
        }

    def _diagnostics(self, total_started: float) -> dict[str, Any]:
        repository_diagnostics = self.repository.request_diagnostics()
        timings = dict(repository_diagnostics.get("timings_ms") or {})
        timings.update(self.mvp.generation_diagnostics())
        timings["total"] = max(0, int((time.perf_counter() - total_started) * 1000))
        return {
            "timings_ms": timings,
            "dependency_health": repository_diagnostics.get("dependency_health") or {},
        }

    async def summarize(
        self,
        request: SummarizeRequest,
        provider: ProviderSpec,
        api_key: str,
    ) -> dict[str, Any]:
        transcript = "\n".join(
            f"{item.role}[{item.communication_channel}]: "
            + " | ".join(f"{block.type}:{block.text}" for block in item.content_blocks)
            for item in request.turns
        )
        prompt = json.dumps(
            {
                "previous_summary": request.previous_summary,
                "new_turns": transcript,
                "required": {
                    "confirmed_relationships": "仅记录用户明确确认的关系与称呼",
                    "world_state": "仅记录明确发生或确认的状态",
                    "open_threads": "仍待继续的话题",
                    "summary": "不超过1200字的中文摘要",
                },
            },
            ensure_ascii=False,
        )
        content, usage = await simple_completion(
            provider,
            api_key,
            request.model,
            system_prompt="你是会话压缩器。只总结明确内容，不推断隐私，不输出Markdown代码块。",
            user_prompt=prompt,
            max_tokens=1200,
        )
        return {"request_id": str(request.request_id), "summary": content[:6000], "usage": usage}
