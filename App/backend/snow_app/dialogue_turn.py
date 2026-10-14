"""Pure turn interpretation; a frame describes intent, never authorizes facts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Literal, Mapping

Domain = Literal["casual", "canon_recall", "session_recall"]
TimeScope = Literal["now", "past", "future", "hypothetical"]
Action = Literal["none", "movement_invitation", "movement_acceptance", "movement_decline", "movement_defer"]
_DOMAINS = {"casual", "canon_recall", "session_recall"}
_TIMES = {"now", "past", "future", "hypothetical"}
_ACTIONS = {"none", "movement_invitation", "movement_acceptance", "movement_decline", "movement_defer"}
_QUOTES = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'", "`": "`"}
_CANON = re.compile(r"原作|原著|剧情|主线|故事(?:里|中|里的)|章节|第[一二三四五六七八九十百\d]+章")
_CANON_PAST = re.compile(r"过去的经历|曾经|过去(?:的|在)|当时|那时|那次(?:任务|战斗|事件)")
_SESSION = re.compile(r"上次|刚才|刚刚|昨天|昨晚|今早|早上|之前|记得|还记得|回忆|上周|上个月|去年|前天|前几天")
_HYPOTHETICAL = re.compile(r"假如|假设|如果|要是|设想|假想")
_FUTURE = re.compile(r"明天|后天|以后|改天|下次|稍后|等会|一会儿|待会|将来|周末|下周|下个月|明年|今晚|明晚|明早")
_PAST = re.compile(r"过去的|曾经|当时|那时|上次|之前|昨天|昨晚|今早|早上|刚才|刚刚|已经|刚完成|刚结束|上周|上个月|去年|前天|前几天|(?:去|到|来|逛|喝|吃)过|(?:去|到|来|逛|喝|吃).{0,12}了(?:吗|么|没)")
_HISTORICAL_MOVEMENT = re.compile(r"(?:去|来|到|逛)过|(?:去|到|来|逛)[^。！？!?\r\n]{0,18}了(?:[吗么没？?。，,！!]|$)")
_DATED_PAST = re.compile(r"上周|上个月|去年|前天|前几天|昨天|昨晚|之前|当时|那时|曾经")
_ARRIVAL_CONFIRMATION = re.compile(r"^(?:那)?我(?:已经|这就|马上|现在)?(?:过去|过来|到了)(?:了|啦)?[。！!，,]*$")
_MOVEMENT_ATTRIBUTION = re.compile(r"(?:来|去|到).{0,24}(?:的是谁|的人是谁|的是哪位|的人是哪位)|谁.{0,16}(?:来|去|到)|看到.{0,20}邀请|收到.{0,20}邀请")
_NEGATION = re.compile(r"不要|不想|不愿|别(?:去|走|来|动)|不去|不能|不可以|取消|不是|没有|没(?:有|去|做|来|完成)")
_DECLINE_CANON_TOPIC = re.compile(r"(?:不要|别|不想|不愿).{0,4}(?:提|聊|讲|说).{0,8}(?:原作|原著|剧情|主线|故事|章节)|(?:不是|并非).{0,3}问.{0,8}(?:原作|剧情|主线|故事|章节)")
_KEEP_PLAN = re.compile(r"不(?:要|会|想|用|必)?取消|别取消|不是(?:不去|不想去|不愿去)|没有(?:不去|不想去)|不会不去")
_INVITATION = re.compile(r"一起(?:去|走|逛|散步|出发|来)|陪我(?:去|逛|走)|跟我去|和我去|我们去|去找|去见|来找|来见|(?:要不要|想不想|能不能|可不可以|愿不愿意).*(?:去|走|来)|(?:去|到|来).{0,35}(?:好吗|可以吗|行吗|吧|坐坐|聊聊|逛逛|看看|散步)|来(?:我|你|我的|你的)?(?:房间|宿舍|那里)")
_ACCEPTANCE = re.compile(r"走吧|出发吧|现在出发|现在走|就这么定|这就过去|我先.*等你|好[呀啊，,].*(?:去|走|来)|我(?:过去|过来|到了)(?:了|啦|[。！!?]|$)")
_COMPLETED_ORDINARY = re.compile(r"喝(?:了|完|一口)|吃(?:了|完|一口)|端起.{0,12}(?:杯|茶)|拿起.{0,12}(?:杯|茶)|散步.*(?:结束|完)|聊(?:了|完)|玩(?:了|完)|刚完成|刚结束|已经.*(?:喝|吃|聊|散步|玩)")
_MAJOR = re.compile(r"主线|剧情|任务|战争|死亡|牺牲|秘密身份|结婚|婚礼|恒约|受伤|伤亡|杀死|击杀")


@dataclass(frozen=True)
class TurnClause:
    clause_id: str
    text: str
    domain: Domain = "casual"
    time_scope: TimeScope = "now"
    negated: bool = False
    quoted: bool = False
    action: Action = "none"

    @property
    def actionable(self) -> bool:
        return (self.domain == "casual" and self.time_scope == "now"
                and not self.negated and not self.quoted
                and self.action in {"movement_invitation", "movement_acceptance"})

    @property
    def completed_ordinary(self) -> bool:
        return (self.domain == "casual" and self.time_scope == "now"
                and not self.negated and not self.quoted
                and bool(_COMPLETED_ORDINARY.search(self.text)) and not _MAJOR.search(self.text)
                and not re.search(r"(?:要不要|想不想|能不能|可不可以|可以|能否).*(?:喝|吃|聊|玩)|[？?]", self.text))

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "actionable": self.actionable}


@dataclass(frozen=True)
class TurnFrame:
    domain: Domain
    time_scope: TimeScope
    clauses: tuple[TurnClause, ...]
    ambiguous: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"domain": self.domain, "time_scope": self.time_scope,
                "clauses": [item.as_dict() for item in self.clauses], "ambiguous": self.ambiguous}


def _segments(message: str):
    """Split punctuation outside quotes, retaining quoted spans as separate clauses."""
    start = 0
    position = 0
    while position < len(message):
        character = message[position]
        apostrophe = (character == "'" and position > 0 and position + 1 < len(message)
                      and message[position - 1].isascii() and message[position + 1].isascii()
                      and message[position - 1].isalnum() and message[position + 1].isalnum())
        if character in _QUOTES and not apostrophe:
            if message[start:position].strip():
                yield message[start:position].strip(), False
            delimiter = "```" if character == "`" and message.startswith("```", position) else character
            closing = "```" if delimiter == "```" else _QUOTES[character]
            end = message.find(closing, position + len(delimiter))
            while end >= 0 and end > 0 and message[end - 1] == "\\":
                end = message.find(closing, end + len(closing))
            if end < 0:
                yield message[position:].strip(), True
                return
            yield message[position:end + len(closing)].strip(), True
            position = end + len(closing)
            start = position
            continue
        if character in "，,。！？!?；;\n":
            if message[start:position + 1].strip("，,。！？!?；;\n "):
                yield message[start:position + 1].strip(), False
            start = position + 1
        position += 1
    if message[start:].strip():
        yield message[start:].strip(), False


def parse_turn_frame(message: str, *, communication_channel: str = "text") -> TurnFrame:
    # Medium is intentionally not inferred from dialogue. Public presence owns it.
    del communication_channel
    clauses = []
    inherited_domain: Domain = "casual"
    inherited_time: TimeScope = "now"
    for text, quoted in _segments(str(message or "")):
        # Chinese alternative questions contain lexical negation but ask for
        # a choice. Remove only those complete forms, never an actual refusal.
        negation_text = re.sub(r"要不要|想不想|能不能|可不可以|好不好|愿不愿意", "", text)
        negated = bool(_NEGATION.search(negation_text))
        explicit_canon = bool(_CANON.search(text)) and not _DECLINE_CANON_TOPIC.search(text)
        arrival_confirmation = bool(_ARRIVAL_CONFIRMATION.fullmatch(text))
        explicit_past_movement = bool(re.search(r"(?:去|来|到|逛)过|已经(?:没|没有|并未|不曾).{0,18}(?:去|来|到)", text))
        historical_movement = (bool(_HISTORICAL_MOVEMENT.search(text)) and (not negated or explicit_past_movement)
                               or explicit_past_movement) and not arrival_confirmation
        session_subject = bool(re.search(r"我们|咱们|一起|上次|我.{0,12}(?:给你|和你|陪你)", text))
        recent_character = bool(re.search(r"刚才|刚刚|昨天|昨晚|今早|早上", text) and re.search(r"你|咱们|我们", text))
        session = (bool(_SESSION.search(text)) or bool(_CANON_PAST.search(text)) or historical_movement) and (session_subject or recent_character)
        canon_past = bool(_CANON_PAST.search(text)) and not session
        domain: Domain = "canon_recall" if explicit_canon or canon_past else "session_recall" if session else "casual"
        if _HYPOTHETICAL.search(text):
            time_scope: TimeScope = "hypothetical"
        elif _FUTURE.search(text):
            time_scope = "future"
        elif historical_movement or _DATED_PAST.search(text):
            # “已经去过” reports a completed trip. Unlike a present typed
            # small action, its “已经” must never authorize another move.
            time_scope = "past"
        elif arrival_confirmation:
            time_scope = "now"
        elif re.search(r"现在|此刻|这会儿|刚完成|刚结束|已经", text):
            time_scope = "now"
        elif _PAST.search(text) or domain in {"canon_recall", "session_recall"}:
            time_scope = "past"
        else:
            time_scope = "now"
        # A heading such as “主线那一章里，...” scopes its following question.
        reset = bool(re.search(r"现在|此刻|这会儿|今天|眼下|目前|回到(?:现在|今天)|说回现在", text))
        invitation_requested = bool(_INVITATION.search(text)) and not _MOVEMENT_ATTRIBUTION.search(text)
        present_invitation = bool(invitation_requested and re.search(r"我们|要不要|想不想|陪我|好吗|来我(?:的)?房间", text))
        future_invitation = bool(invitation_requested and _FUTURE.search(text))
        if (present_invitation or future_invitation) and not (explicit_canon or canon_past or session or _PAST.search(text) or _HYPOTHETICAL.search(text)):
            # An explicit daily invitation following a story question begins
            # its own domain. A future heading must still scope its time.
            domain = "casual"
            if inherited_domain != "casual" and not _FUTURE.search(text):
                time_scope = "now"
            reset = inherited_domain != "casual"
        if not explicit_canon and not canon_past and not session and not reset:
            if inherited_domain != "casual":
                domain, time_scope = inherited_domain, inherited_time
            elif inherited_time != "now" and not (_FUTURE.search(text) or _HYPOTHETICAL.search(text) or _PAST.search(text)):
                # “明天，去商场坐坐好吗” is one future invitation, not a
                # present action merely because punctuation split the phrase.
                time_scope = inherited_time
        if not quoted and not negated:
            inherited_domain, inherited_time = domain, time_scope
        action: Action = "none"
        if _KEEP_PLAN.search(text):
            action = "none"
        elif re.search(r"取消|不去|不想去|别去", text):
            action = "movement_decline"
        elif _FUTURE.search(text) and (invitation_requested or _ACCEPTANCE.search(text)):
            action = "movement_defer"
        elif invitation_requested:
            action = "movement_invitation"
        elif _ACCEPTANCE.search(text):
            action = "movement_acceptance"
        clauses.append(TurnClause(f"c{len(clauses)}", text, domain, time_scope, negated, quoted, action))
    if not clauses:
        clauses = [TurnClause("c0", str(message or "").strip())]
    active = [item for item in clauses if not item.quoted and not item.negated] or clauses
    selected = next((item for item in active if item.domain == "canon_recall"), None)
    selected = selected or next((item for item in active if item.domain == "session_recall"), None) or active[-1]
    return TurnFrame(selected.domain, selected.time_scope, tuple(clauses),
                     len({(item.domain, item.time_scope) for item in active}) > 1)


def coerce_turn_frame(value: Any, message: str = "", communication_channel: str = "text") -> TurnFrame:
    if isinstance(value, TurnFrame):
        return value
    if not isinstance(value, Mapping):
        return parse_turn_frame(message, communication_channel=communication_channel)
    clauses = []
    for index, item in enumerate(value.get("clauses") or []):
        if not isinstance(item, Mapping):
            continue
        domain, time_scope, action = item.get("domain"), item.get("time_scope"), item.get("action", "none")
        if domain not in _DOMAINS or time_scope not in _TIMES or action not in _ACTIONS:
            continue
        clauses.append(TurnClause(str(item.get("clause_id") or f"c{index}"), str(item.get("text") or ""),
                                  domain, time_scope, bool(item.get("negated")), bool(item.get("quoted")), action))
    if not clauses or value.get("domain") not in _DOMAINS or value.get("time_scope") not in _TIMES:
        return parse_turn_frame(message, communication_channel=communication_channel)
    return TurnFrame(value["domain"], value["time_scope"], tuple(clauses), bool(value.get("ambiguous")))
