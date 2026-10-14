"""One bounded resolver shared by structured and conversational invitations."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .dialogue_turn import TurnFrame

_MOVE = re.compile(r"去|来|走|逛|散步|见面|出发|坐坐|找你|赴约")
_CONTINUE = re.compile(r"走吧|出发|那就|我过去|我过来|去找你|来找你|你那里|你那边")


@dataclass(frozen=True)
class SceneIntent:
    location_id: str
    activity_id: str
    definition: dict[str, Any]
    time_scope: str = "now"
    operation: str = "invite"


def resolve_invitation(frame: TurnFrame, catalog: dict[str, dict[str, Any]], *,
                       selected_location: str = "", current_location: str = "",
                       active_invitation: Any = None) -> SceneIntent | None:
    candidates: dict[str, tuple[dict[str, Any], str]] = {}
    # A short refusal/defer applies only to an independently signed agreement,
    # never to a destination reconstructed from a transcript.
    if active_invitation and active_invitation.status in {"proposed", "accepted", "deferred", "clarify"}:
        key = active_invitation.location_id
        controls = [clause for clause in frame.clauses if clause.domain == "casual"
                    and not clause.quoted and clause.time_scope in {"now", "future"}]
        cancel = any(clause.action == "movement_decline"
                     and (clause.time_scope == "now" or active_invitation.time_scope == "future")
                     and not re.search(r"[？?]|要不要|能不能", clause.text)
                     for clause in controls)
        defer = any(clause.time_scope == "future" and re.search(r"那就|改成|改到|改天|下次|换到|改为", clause.text)
                    for clause in controls)
        if key in catalog and (cancel or defer):
            definition = catalog[key]
            return SceneIntent(key, active_invitation.activity_id, definition,
                               "now" if cancel else "future", "cancel" if cancel else "defer")
    for clause in frame.clauses:
        if clause.domain != "casual" or clause.time_scope not in {"now", "future"} or clause.quoted or clause.negated:
            continue
        text = clause.text
        if not _MOVE.search(text):
            continue
        if re.search(r"喜欢|习惯|经常|通常|为什么|意思|怎么去|怎样去", text) and not re.search(
            r"一起|陪我|跟我|和我|要不要|想不想|邀请|走吧|好吗|好不好|吧", text
        ):
            continue
        text = text.replace("我房间", "我的房间").replace("你房间", "你的房间")
        matches = []
        # A named place always wins over activity aliases (mall vs shopping).
        for key, definition in catalog.items():
            aliases = tuple(definition.get("explicit_aliases") or ())
            if any(alias and alias in text for alias in aliases):
                matches.append((key, definition))
        if not matches:
            for key, definition in catalog.items():
                if any(alias and alias in text for alias in definition.get("aliases") or ()):
                    matches.append((key, definition))
        if not matches and _CONTINUE.search(text):
            if active_invitation and active_invitation.status in {"proposed", "accepted", "deferred", "clarify"}:
                key = active_invitation.location_id
                if key in catalog:
                    matches = [(key, catalog[key])]
            elif re.search(r"你那里|你那边|去找你|来找你", text):
                matches = [(key, value) for key, value in catalog.items()
                           if current_location and current_location == value.get("location")]
        for key, definition in matches:
            candidates[key] = (definition, clause.time_scope)
    if len(candidates) != 1:
        return None
    location_id, (definition, time_scope) = next(iter(candidates.items()))
    if selected_location and selected_location != location_id:
        return None
    activity_id = str(definition.get("resolved_activity_id") or next(iter(definition.get("activities") or {}), ""))
    if not activity_id:
        return None
    return SceneIntent(location_id, activity_id, definition, time_scope)
