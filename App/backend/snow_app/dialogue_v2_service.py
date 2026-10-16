"""Stateless dialogue/scene adapter: canonical knowledge never becomes session data."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, UTC
from typing import Any

from .arrival_ticket import sign_arrival_ticket, verify_arrival_ticket
from .dialogue_turn import parse_turn_frame
from .mvp_policy import scene_visual_key
from .public_contracts import (DialogueV2ChatRequest, SceneActionRequest, SessionContext,
                              SessionMemory, SceneOverlay, SceneInvitation, StatePayload,
                              StateEvent, PendingRendezvous)
from .public_security import PublicSecurityError, sign_state, verify_state
from .public_service import _CONTROLLED_JOINT_LOCATIONS
from .scene_intent import resolve_invitation
from .mvp_service import MVPService
from .session_context import (new_session_context, verify_session_context, sign_session_context,
                              sign_session_memory, verify_session_memory)


class DialogueV2Service:
    def __init__(self, chat_service, public_settings):
        self.legacy = chat_service
        self.settings = public_settings
        self.catalog = {key: value for key, value in _CONTROLLED_JOINT_LOCATIONS.items()
                        if value.get("public_invitation") is not False}
        self.catalog_version = hashlib.sha256(json.dumps(self.catalog, ensure_ascii=False,
                                                        sort_keys=True).encode()).hexdigest()[:16]

    def _definition(self, location_id, character_id):
        if location_id not in self.catalog:
            raise ValueError("unknown public location")
        return self.legacy._resolved_location_definition(location_id, self.catalog[location_id], character_id)

    def _context(self, envelope, subject):
        chat = getattr(envelope, "chat", envelope)
        context = verify_session_context(self.settings, envelope.session_context_package,
            subject_hash=subject, character_id=chat.character_id, segment_id=envelope.segment_id,
            catalog_version=self.catalog_version)
        if context is None:
            context = new_session_context(self.settings, subject_hash=subject,
                character_id=chat.character_id, segment_id=envelope.segment_id, catalog_version=self.catalog_version)
        context = context.model_copy(deep=True)
        # The browser owns selection and retention. An old sidecar must not
        # resurrect an experience removed from the local history/index.
        memories = {}
        if self.settings.session_memory_v2:
            for item in envelope.memory_candidates:
                try:
                    verified = verify_session_memory(self.settings, item.proof, subject_hash=subject,
                                                     character_id=chat.character_id)
                except PublicSecurityError:
                    # Old/imported history is never a verified scene authority.
                    continue
                if verified is not None and verified.model_dump() == item.memory.model_dump():
                    memories[verified.memory_id] = verified
        context.memories = list(memories.values())[-12:]
        if chat.continuity_decision == "start_today":
            context.scene_epoch += 1
            context.scene_overlay = None
            context.active_invitation = None
        return context

    def validate_chat(self, envelope: DialogueV2ChatRequest, subject: str):
        self.legacy.ensure_character_available(envelope.chat.character_id)
        context = self._context(envelope, subject)
        world = self._world(envelope.chat, context, subject)
        if envelope.chat.communication_channel == "in_person" and not self.legacy._scene_state(
            world, envelope.chat.character_id
        )["co_located"]:
            raise ValueError("in-person dialogue requires a confirmed meeting")
        frame = parse_turn_frame(envelope.chat.message, communication_channel=envelope.chat.communication_channel)
        intent = self._intent(envelope.chat, frame, context, world)
        if envelope.chat.movement_location_id and (not intent or intent.location_id != envelope.chat.movement_location_id):
            raise ValueError("invitation does not match selected location")
        return context, world, frame, intent

    def _intent(self, chat, frame, context, world):
        definitions = {key: self._definition(key, chat.character_id) for key in self.catalog}
        location = ((world.get("presence") or {}).get(chat.character_id) or {}).get("location", "")
        # Mentioning another character does not authorize moving this contact.
        # Keep canonical questions intact for retrieval; scope only execution.
        eligible = []
        for clause in frame.clauses:
            mentions = MVPService._resolve_character_mentions(clause.text)
            foreign = any(item["character_id"] != chat.character_id for item in mentions)
            addressed = any(item["character_id"] == chat.character_id for item in mentions)
            if foreign and not (addressed or re.search(r"你|我们|咱们", clause.text)):
                continue
            eligible.append(clause)
        frame = replace(frame, clauses=tuple(eligible))
        return resolve_invitation(frame, definitions, selected_location=chat.movement_location_id,
                                  current_location=location, active_invitation=context.active_invitation)

    def _world(self, chat, context, subject):
        raw = verify_state(self.settings, chat.state_package) or {}
        world = self.legacy._normalized_state(chat.state_package, subject)
        overlay = context.scene_overlay
        if overlay is not None:
            if overlay.scene_epoch != context.scene_epoch:
                raise ValueError("scene epoch mismatch")
            if overlay.location_id:
                self._definition(overlay.location_id, chat.character_id)
            # Only a verified conversation scene survives the next daily seed.
            restore = (overlay.schedule_date != world.get("schedule_date")
                       and chat.continuity_decision == "continue_previous")
            if restore:
                world["analyst_location"] = overlay.analyst_location
                scene = world["presence"][chat.character_id]
                scene.update(location=overlay.character_location or scene["location"],
                             activity=overlay.character_activity or scene["activity"],
                             state_scope="conversation_confirmed")
                if overlay.pending_rendezvous is not None:
                    pending = overlay.pending_rendezvous.model_dump()
                    self._definition(pending["location_id"], chat.character_id)
                    pending["schedule_date"] = world["schedule_date"]
                    world["pending_rendezvous"][chat.character_id] = pending
                world["revision"] = max(world["revision"], overlay.world_revision + 1)
            elif int(raw.get("revision") or 0) < overlay.world_revision:
                raise ValueError("stale world snapshot")
        if chat.continuity_decision == "start_today":
            defaults = self.legacy._default_state(subject)
            world["presence"][chat.character_id] = defaults["presence"][chat.character_id]
            world["analyst_location"] = defaults.get("analyst_location")
            world["pending_rendezvous"].pop(chat.character_id, None)
            world["revision"] += 1
        # Older clients retained the accepted invitation after a completed
        # joint move. Consume only that exact signed scene event, never a
        # future promise, unresolved invitation or remote waiting rendezvous.
        active = context.active_invitation
        if (active and active.status == "accepted" and active.time_scope == "now"
                and overlay and active.invitation_id == f"invite:{overlay.source_event_id}"
                and active.location_id == overlay.location_id
                and active.activity_id == overlay.activity_id
                and not world.get("pending_rendezvous", {}).get(chat.character_id)):
            target = self._definition(active.location_id, chat.character_id)["location"]
            scene = world["presence"][chat.character_id]
            if world.get("analyst_location") == scene["location"] == target:
                context.active_invitation = None
        return world

    def _memory(self, context, request_id, kind, value, location_id="", *, source_message_ids=None):
        memory_id = hashlib.sha256(f"{request_id}:{kind}:{value}".encode()).hexdigest()[:32]
        return SessionMemory(memory_id=memory_id, kind=kind, value=value[:120],
            source_request_id=request_id, segment_id=context.segment_id,
            occurred_at=datetime.now(UTC).isoformat(), location_id=location_id,
            scene_epoch=context.scene_epoch, source_message_ids=source_message_ids or [f"user:{request_id}", f"reply:{request_id}"])

    def _finish(self, result, context, world, chat, subject, new_memories=(), action=None):
        overlay = context.scene_overlay
        if (action and action.get("action") not in {"open_communicator", "resolve"}
                and action.get("type") != "invitation"
                and action.get("status") in {"applied", "character_waiting", "joined", "cancelled"}):
            scene = world["presence"][chat.character_id]
            pending = self.legacy._pending_for_character(world, chat.character_id)
            context.scene_overlay = SceneOverlay(scene_id=f"scene:{context.segment_id}",
                scene_epoch=context.scene_epoch, world_revision=world["revision"],
                schedule_date=world["schedule_date"], communication_channel=result["communication_channel"],
                analyst_location=world.get("analyst_location"), character_location=scene["location"],
                character_activity=scene["activity"], location_id=str(action.get("location_id") or ""),
                activity_id=str(action.get("activity_id") or ""), source_event_id=str(chat.request_id),
                pending_rendezvous=PendingRendezvous.model_validate(pending) if pending else None)
        elif overlay is not None and (overlay.schedule_date == world["schedule_date"]
                                      or chat.continuity_decision == "continue_previous"):
            scene = world["presence"][chat.character_id]
            pending = self.legacy._pending_for_character(world, chat.character_id)
            overlay.analyst_location = world.get("analyst_location")
            overlay.character_location = scene["location"]
            overlay.character_activity = scene["activity"]
            overlay.pending_rendezvous = PendingRendezvous.model_validate(pending) if pending else None
            overlay.world_revision = world["revision"]
            overlay.schedule_date = world["schedule_date"]
            overlay.communication_channel = result["communication_channel"]
        receipts = []
        if self.settings.session_memory_v2:
            for memory in new_memories:
                receipts.append({"memory": memory.model_dump(mode="json"),
                    "proof": sign_session_memory(self.settings, memory, subject_hash=subject, character_id=chat.character_id)})
            memories = {item.memory_id: item for item in context.memories}
            memories.update({item.memory_id: item for item in new_memories})
            context.memories = list(memories.values())[-12:]
        context.revision += 1
        context.local_day_key = chat.local_day_key or world["schedule_date"]
        context.issued_at = datetime.now(UTC).isoformat()
        # Bound transfer, not local history. The caller retains every receipt.
        while True:
            try:
                token = sign_session_context(self.settings, context)
                break
            except PublicSecurityError:
                if not context.memories:
                    raise
                context.memories.pop(0)
        result.update(state_package=sign_state(self.settings, StatePayload.model_validate(world).model_dump()),
                      scene_state=self.legacy._scene_state(world, chat.character_id),
                      pending_rendezvous=self.legacy._pending_for_character(world, chat.character_id),
                      session_context_package=token, session_memories=receipts, scene_action=action or {"status": "not_requested"})
        return result

    def _reply_refuses_invitation(self, answer, intent):
        refusal = re.compile(
            r"(?:不想|不愿|不能|不方便|没空)[^，。！？!?；;\n]{0,24}(?:去|来|见面|出发)|"
            r"(?:不|别|不要)(?:去|来|走|出发|见面)|"
            r"(?:不方便|没空)(?:[。！!；;,，\s]|$)|"
            r"(?:今天|现在)[^，。！？!?；;\n]{0,12}(?:不行|不方便|没空)|"
            r"(?:去|见面|出发)[^，。！？!?；;\n]{0,8}(?:不了|取消)"
        )
        for clause in parse_turn_frame(str(answer or "")).clauses:
            if clause.quoted or clause.domain != "casual" or clause.time_scope not in {"now", "future"}:
                continue
            if intent.time_scope == "future" and clause.time_scope == "now" and re.search(r"今天|现在", clause.text):
                continue
            places = {key for key, definition in self.catalog.items()
                      if any(alias and alias in clause.text for alias in definition.get("explicit_aliases") or ())}
            if places and intent.location_id not in places:
                continue
            if intent.time_scope == "now" and clause.time_scope == "future" and re.search(r"去|来|见面|出发", clause.text):
                return True
            if refusal.search(clause.text):
                return True
        return False

    async def chat(self, envelope, subject, provider, api_key, *, gate_reserved=False):
        context, world, frame, prepared_intent = self.validate_chat(envelope, subject)
        chat = envelope.chat
        intent = prepared_intent if self.settings.scene_actions_v2 else None
        catalog = []
        previous_invitation = context.active_invitation
        if intent:
            context.active_invitation = SceneInvitation(invitation_id=f"invite:{chat.request_id}",
                location_id=intent.location_id, activity_id=intent.activity_id, time_scope=intent.time_scope)
            catalog = [{"location_id": intent.location_id, "activity_id": intent.activity_id,
                        "movement_mode": "rendezvous" if chat.communication_channel == "text" else "joint",
                        "invitation_operation": intent.operation}]
        experiences = [{"experience_id": item.memory_id, "summary": item.value,
                        "source_message_ids": item.source_message_ids, "provenance": item.kind,
                        "domain": "session", "status": "confirmed" if item.kind == "shared_scene_event" else "reported"}
                       for item in context.memories] if self.settings.session_memory_v2 else []
        internal = chat.model_copy(update={"state_package": sign_state(self.settings, world)})
        result = await self.legacy.chat(internal, subject, provider, api_key, gate_reserved=gate_reserved,
            v2_context={"turn_frame": frame, "domain_routing": self.settings.dialogue_domains_v2,
                        "session_experiences": experiences, "movement_catalog": catalog})
        decision = result.pop("_v2_interaction_decision", None)
        memory_candidates = result.pop("_v2_memory_candidates", None) or []
        if result.get("terminal_error"):
            return result
        if intent and intent.operation in {"cancel", "defer"}:
            decision = {"decision": "decline" if intent.operation == "cancel" else "defer",
                        "location_id": intent.location_id, "activity_id": intent.activity_id}
        world = verify_state(self.settings, result["state_package"])
        memories = []
        action = {"status": "not_requested"}
        if intent and isinstance(decision, dict) and decision.get("decision") in {"accept_now", "decline", "defer", "clarify"}:
            valid_target = decision.get("location_id") == intent.location_id and decision.get("activity_id") == intent.activity_id
            disposition = decision["decision"] if valid_target else "clarify"
            if intent.operation in {"cancel", "defer"}:
                disposition = "decline" if intent.operation == "cancel" else "defer"
            elif disposition == "accept_now" and self._reply_refuses_invitation(result.get("answer"), intent):
                disposition = "clarify"
            context.active_invitation.status = {
                "accept_now": "accepted", "decline": "declined", "defer": "deferred",
            }.get(disposition, disposition)
            if disposition == "accept_now" and intent.time_scope == "now":
                world, action = self._move(world, chat, intent, waiting=chat.communication_channel == "text")
                if chat.communication_channel == "in_person":
                    context.active_invitation = None
                memories.append(self._memory(context, chat.request_id,
                    "assistant_commitment" if chat.communication_channel == "text" else "shared_scene_event",
                    f"约定在{intent.definition['location']}会合" if chat.communication_channel == "text" else f"一起到达{intent.definition['location']}", intent.location_id))
            else:
                action = {"status": disposition, "location_id": intent.location_id}
                if intent.operation in {"cancel", "defer"} or (disposition == "accept_now" and intent.time_scope == "future"):
                    pending = world["pending_rendezvous"].pop(chat.character_id, None)
                    if pending:
                        scene = world["presence"][chat.character_id]
                        scene["activity"] = f"在{scene['location']}休息"
                        world["revision"] += 1
                        action.update(status="cancelled", action=intent.operation)
                elif disposition == "decline" and world["pending_rendezvous"].get(chat.character_id) and previous_invitation:
                    context.active_invitation = previous_invitation
                if disposition == "accept_now" and intent.time_scope == "future":
                    action.update(status="defer", type="invitation")
        # Statements retain their source rank; they are not invented joint facts.
        if self.settings.session_memory_v2:
            for clause in frame.clauses:
                if clause.completed_ordinary:
                    memories.append(self._memory(context, chat.request_id, "user_statement", clause.text))
            if chat.communication_channel == "in_person" and self.legacy._scene_state(world, chat.character_id)["co_located"]:
                memories.extend(self._ordinary_experiences(context, chat, frame, result, memory_candidates))
        result["movement_status"] = {**action, "schedule_date": world["schedule_date"],
                                      "schedule_revision": world["schedule_revision"]}
        return self._finish(result, context, world, chat, subject, memories, action)

    def _ordinary_experiences(self, context, chat, frame, result, candidates):
        """A model candidate cannot establish history; both current actions must support it."""
        ordinary = (r"喝|饮|茶|水|咖啡", r"吃|饭|蛋糕|饼", r"玩|棋|游戏")
        not_completed = re.compile(
            r"如果|假如|打算|想要|准备|计划|提议|将要|将会|等会|稍后|没有|不愿|不要|"
            r"(?:没|未).{0,4}(?:完成|结束|走完|散步)|"
            r"主线|剧情|任务|战争|死亡|牺牲|秘密|身份|结婚|婚礼|戒指|恒约|受伤|伤亡|杀死|击杀"
        )
        completed_walk = re.compile(
            r"散步[^。！？!?\r\n]{0,16}(?:完成|结束|完毕)|"
            r"(?:完成|结束)[^。！？!?\r\n]{0,8}散步|"
            r"走完(?:了)?(?:一(?:小)?圈|这段路)|走了(?:一(?:小)?圈)"
        )
        # A walk receipt is not authority for physical intimacy, a new
        # relationship or a plot outcome embedded in the same action.
        walk_nonordinary = re.compile(r"牵|握.{0,5}手|拥抱|抱住|亲吻|搂|告白|恋人|伴侣|战斗|战场|击败|打败|摧毁|救出|救回")
        planned_walk = re.compile(r"(?:会|想|要|能).{0,8}(?:走完|散步)|尚未|还没|未能|没能")

        def current_action(text):
            return not not_completed.search(text) and all(
                clause.domain == "casual" and clause.time_scope == "now"
                and not clause.quoted and not clause.negated
                for clause in parse_turn_frame(text).clauses
            )

        user_actions = [block.text for block in chat.content_blocks if block.type == "action"]
        response = result.get("content_blocks") or []
        values = []
        for candidate in candidates[:4]:
            if not isinstance(candidate, dict) or candidate.get("event_type") != "ordinary_interaction":
                continue
            ids = candidate.get("source_clause_ids") or []
            clauses = [item for item in frame.clauses if item.clause_id in ids]
            if not clauses or len(clauses) != len(set(ids)) or any(
                item.domain != "casual" or item.time_scope != "now" or item.negated or item.quoted for item in clauses
            ):
                continue
            indices = candidate.get("response_block_indices") or []
            for index in indices:
                if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(response):
                    continue
                block = response[index]
                if block.get("type") != "action":
                    continue
                action = str(block.get("text") or "")
                if not current_action(action):
                    continue
                for user_action in user_actions:
                    if not current_action(user_action):
                        continue
                    ordinary_pair = any(re.search(pattern, user_action) and re.search(pattern, action) for pattern in ordinary)
                    walked_together = (completed_walk.search(user_action) and completed_walk.search(action)
                                       and not walk_nonordinary.search(user_action) and not walk_nonordinary.search(action)
                                       and not planned_walk.search(user_action) and not planned_walk.search(action))
                    if ordinary_pair or walked_together:
                        values.append(self._memory(context, chat.request_id, "shared_scene_event",
                            f"当轮互动：{user_action[:48]}；{action[:48]}"))
                        break
        return list({item.memory_id: item for item in values}.values())[:2]

    def _move(self, world, chat, intent, *, waiting):
        world = json.loads(json.dumps(world))
        definition = intent.definition
        location, activity = definition["location"], definition["activities"][intent.activity_id]
        world["presence"][chat.character_id].update(location=location,
            activity=definition.get("waiting_activity", f"在{location}等分析员") if waiting else activity,
            state_scope="conversation_confirmed")
        event_id = f"{chat.request_id}:rendezvous_waiting" if waiting else f"{chat.request_id}:joint_move"
        if waiting:
            world["pending_rendezvous"][chat.character_id] = PendingRendezvous(
                rendezvous_id=event_id, character_id=chat.character_id, location_id=intent.location_id,
                location_name=location, activity_id=intent.activity_id,
                waiting_activity=world["presence"][chat.character_id]["activity"], joined_activity=activity,
                created_at=datetime.now(UTC).isoformat(), schedule_date=world["schedule_date"]).model_dump()
        else:
            world["analyst_location"] = location
            world["pending_rendezvous"].pop(chat.character_id, None)
        event = StateEvent(event_id=event_id, event_type="rendezvous_waiting" if waiting else "joint_movement",
            character_id=chat.character_id, communication_channel=chat.communication_channel, location=location,
            location_id=intent.location_id, activity_id=intent.activity_id)
        world["recent_events"] = [*world["recent_events"], event.model_dump()][-4:]
        world["revision"] += 1
        return world, {"status": "character_waiting" if waiting else "applied", "location_id": intent.location_id,
            "location_name": location, "display_name": location, "activity_id": intent.activity_id,
            "character_id": chat.character_id}

    def scene_action(self, envelope: SceneActionRequest, subject: str):
        self.legacy.ensure_character_available(envelope.character_id)
        context = self._context(envelope, subject)
        world = self._world(envelope, context, subject)
        channel = context.scene_overlay.communication_channel if context.scene_overlay else "text"
        action = {"status": "not_requested", "action": envelope.action}
        memories = []
        pending = self.legacy._pending_for_character(world, envelope.character_id)
        if envelope.action in {"join_rendezvous", "cancel_rendezvous"}:
            active = context.active_invitation
            valid_pending = pending and pending["rendezvous_id"] == envelope.rendezvous_id
            valid_invitation = (envelope.action == "cancel_rendezvous" and not pending and active
                                and active.status in {"proposed", "accepted", "deferred", "clarify"}
                                and active.invitation_id == envelope.rendezvous_id)
            if not valid_pending and not valid_invitation:
                raise ValueError("rendezvous is stale")
        if envelope.action in {"join_character", "join_rendezvous"}:
            scene = world["presence"][envelope.character_id]
            if pending:
                self._definition(pending["location_id"], envelope.character_id)
                scene.update(location=pending["location_name"], activity=pending["joined_activity"],
                             state_scope="conversation_confirmed")
                world["pending_rendezvous"].pop(envelope.character_id, None)
            world["analyst_location"] = scene["location"]
            channel = "in_person"
            context.active_invitation = None
            action = {"status": "joined", "action": envelope.action, "location_name": scene["location"],
                      "location_id": pending["location_id"] if pending else "",
                      "activity_id": pending["activity_id"] if pending else ""}
            memories.append(self._memory(context, envelope.request_id, "shared_scene_event",
                                         f"在{scene['location']}会合", action["location_id"],
                                         source_message_ids=[f"reply:{envelope.request_id}"]))
        elif envelope.action == "open_communicator":
            channel = "text"
            action = {"status": "applied", "action": envelope.action}
        elif envelope.action == "cancel_rendezvous":
            world["pending_rendezvous"].pop(envelope.character_id, None)
            location_id = pending["location_id"] if pending else context.active_invitation.location_id
            if pending:
                scene = world["presence"][envelope.character_id]
                scene["activity"] = f"在{scene['location']}休息"
            context.active_invitation = None
            action = {"status": "cancelled", "action": envelope.action, "location_id": location_id,
                      "type": "scene" if pending else "invitation"}
        if envelope.action != "resolve":
            world["revision"] += 1
            event = StateEvent(event_id=str(envelope.request_id), event_type="arrival" if envelope.action in {"join_character", "join_rendezvous"} else "communication",
                               character_id=envelope.character_id, communication_channel=channel,
                               location=world.get("analyst_location"))
            world["recent_events"] = [*world["recent_events"], event.model_dump()][-4:]
        result = {"request_id": str(envelope.request_id), "character_id": envelope.character_id,
                  "communication_channel": channel, "movement_status": action, "model_called": False,
                  "usage": {"provider_calls": 0}, "terminal_error": "", "state_event": world["recent_events"][-1] if world["recent_events"] else None}
        result = self._finish(result, context, world, envelope, subject, memories, action)
        if action.get("status") == "joined":
            result["arrival_ticket"] = sign_arrival_ticket(self.settings,
                subject_hash=subject, character_id=envelope.character_id,
                segment_id=envelope.segment_id, arrival_id=envelope.request_id,
                state_package=result["state_package"],
                session_context_package=result["session_context_package"])
        return result

    def prepare_arrival(self, envelope, subject, joined):
        """Verify an existing join without reapplying movement or probability."""
        verify_arrival_ticket(self.settings, envelope, subject)
        if (not isinstance(joined, dict) or joined.get("request_id") != str(envelope.arrival_id)
                or joined.get("character_id") != envelope.character_id
                or joined.get("communication_channel") != "in_person"
                or joined.get("scene_action", {}).get("status") != "joined"
                or joined.get("scene_action", {}).get("action") not in {"join_character", "join_rendezvous"}
                or joined.get("state_package") != envelope.state_package
                or joined.get("session_context_package") != envelope.session_context_package
                or joined.get("arrival_ticket") != envelope.arrival_ticket):
            raise PublicSecurityError("Arrival ticket does not match a completed join")
        context = verify_session_context(self.settings, envelope.session_context_package,
            subject_hash=subject, character_id=envelope.character_id,
            segment_id=envelope.segment_id, catalog_version=self.catalog_version)
        world = StatePayload.model_validate(verify_state(self.settings, envelope.state_package)).model_dump()
        if (not context or not context.scene_overlay
                or context.scene_overlay.source_event_id != str(envelope.arrival_id)
                or context.scene_overlay.communication_channel != "in_person"
                or world["subject_binding"] != subject
                or not self.legacy._scene_state(world, envelope.character_id)["co_located"]):
            raise PublicSecurityError("Arrival ticket does not confirm a meeting")
        self.legacy.ensure_character_available(envelope.character_id)
        return {"arrival_id": str(envelope.arrival_id), "character_id": envelope.character_id,
                "communication_channel": "in_person", "decision": "noticed",
                "reaction": None, "model_called": False, "state": world,
                "state_package": envelope.state_package}
