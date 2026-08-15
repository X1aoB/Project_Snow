"""Stateless public chat facade around the evidence-backed immersive engine."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import tempfile
import time
from typing import Any, Iterator

from .config import PublicSettings, Settings
from .mvp_policy import MVP_CHARACTERS
from .mvp_service import MVPService, _SESSION_STATES, _SESSION_LOCK, _WORLD_STATES, _WORLD_STATE_LOCK
from .public_contracts import ChatRequest, HistoryTurn, StatePayload, SummarizeRequest
from .public_providers import ProviderSpec, simple_completion
from .public_repository import PublicRuntimeRepository
from .public_security import safe_output, sign_state, verify_state


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
    pending_user = ""
    for item in history[-24:]:
        if item.role == "user":
            pending_user = item.content
        elif pending_user:
            turns.append(
                {
                    "user": pending_user,
                    "assistant": item.content,
                    "communication_channel": "text",
                    "mode": "immersive",
                }
            )
            pending_user = ""
    return turns[-12:]


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

    @contextmanager
    def _request_state(
        self,
        request: ChatRequest,
        subject_hash: str,
    ) -> Iterator[tuple[str, str, dict[str, Any]]]:
        state = verify_state(self.public_settings, request.state_package)
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
            "communication_channel": "text",
            "recent_story_titles": [],
        }
        world_state = {
            "world_session_id": world_id,
            "created_at": "browser_state",
            "analyst_location": None,
            "presence": dict((state.get("world") or {}).get("presence") or {}),
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
                    communication_channel="text",
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
            answer, safety_category = safe_output(str(result.get("answer") or ""))
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
            truncated = len(answer) > 1200
            answer = answer[:1200]
            revision = int(prior_state.get("revision") or 0) + 1
            next_state = StatePayload(
                data_version=self.public_settings.data_version,
                revision=revision,
                relationships=dict(prior_state.get("relationships") or {}),
                world={
                    "presence": dict((self.mvp._world_snapshot(world_id).get("presence") or {})),
                },
            ).model_dump()
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
        transcript = "\n".join(f"{item.role}: {item.content}" for item in request.turns)
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
