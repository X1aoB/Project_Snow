"""Policy and deterministic configuration for the first dialogue MVP.

This module deliberately contains policy, not scraped content.  It is the
boundary between the complete source corpus and the small, reviewable first
test surface.  The crawler and the canonical graph are never modified here.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MVPCharacter:
    character_id: str
    display_name: str
    source_name: str
    aliases: tuple[str, ...]
    selector_enabled: bool = True


MVP_CHARACTER_REGISTRY_PATH = Path(__file__).with_name("mvp_character_registry.json")


def _load_character_registry() -> tuple[str, tuple[MVPCharacter, ...]]:
    payload = json.loads(MVP_CHARACTER_REGISTRY_PATH.read_text(encoding="utf-8"))
    version = str(payload.get("version") or "mvp-unknown")
    records = payload.get("characters") or []
    characters = tuple(
        MVPCharacter(
            str(record["character_id"]),
            str(record["display_name"]),
            str(record.get("source_name") or record["display_name"]),
            tuple(dict.fromkeys(str(alias) for alias in record.get("aliases", []) if str(alias).strip())),
            bool(record.get("selector_enabled", True)),
        )
        for record in records
    )
    if len(characters) != 22:
        raise RuntimeError(f"MVP character registry must contain 22 characters, got {len(characters)}")
    if sum(item.selector_enabled for item in characters) != 22:
        raise RuntimeError("MVP character registry must expose all 22 evidence-ready characters")
    ids = [item.character_id for item in characters]
    if len(set(ids)) != len(ids):
        raise RuntimeError("MVP character registry contains duplicate character IDs")
    alias_owners: dict[str, str] = {}
    for item in characters:
        for alias in dict.fromkeys((item.display_name, item.source_name, *item.aliases)):
            previous = alias_owners.setdefault(alias, item.character_id)
            if previous != item.character_id:
                raise RuntimeError(f"MVP character registry alias is ambiguous: {alias}")
    return version, characters


MVP_REGISTRY_VERSION, MVP_CHARACTERS = _load_character_registry()

MVP_CHARACTER_BY_ID = {item.character_id: item for item in MVP_CHARACTERS}
MVP_CHARACTER_BY_NAME = {
    alias: item
    for item in MVP_CHARACTERS
    for alias in (item.display_name, item.source_name, *item.aliases)
}

# Human-confirmed conversational aliases are input-resolution aids, not
# universal forms of address.  A companion should normally answer with the
# canonical name unless source dialogue proves that speaker uses the alias.
MVP_DIALOGUE_ALIASES: dict[str, tuple[str, str]] = {
    "猫猫": ("6862c43d2ac9", "猫汐尔"),
    "小老师": ("a2ffc5b44d7f", "芙提雅"),
}


# These are ephemeral present-time simulation scenes, not scraped lore or
# permanent character facts.  A stable world-session ID selects one entry per
# character so cross-character questions stay consistent without forcing
# everyone into the same repeatedly retrieved story location.
MVP_SCENE_TEMPLATES: dict[str, tuple[tuple[str, str], ...]] = {
    "ca0144ccd81b": (
        ("基地休息区", "在窗边安静地休息"),
        ("医务室附近", "刚完成例行检查"),
        ("训练区", "刚结束一轮训练"),
        ("个人房间", "在整理随身物品"),
        ("基地走廊", "正准备去找分析员"),
    ),
    "1b0a6b35719a": (
        ("训练区", "刚结束一轮训练"),
        ("基地休息区", "在和队员聊最近的安排"),
        ("餐厅", "在挑选想喝的饮品"),
        ("个人房间", "在整理今天的装束"),
        ("基地走廊", "正兴致勃勃地四处找人"),
    ),
    "25b23cb64398": (
        ("观景区", "在看基地外的景色"),
        ("个人房间", "在享受难得的安静时间"),
        ("基地休息区", "在沙发上放松"),
        ("训练区", "刚活动完身体"),
        ("餐厅", "在找些简单的点心"),
    ),
    "6862c43d2ac9": (
        ("个人房间", "在安静地休息"),
        ("基地休息区", "在找一个舒服的位置坐下"),
        ("餐厅", "在看看有没有合口味的食物"),
        ("观景区", "在出神地望着远处"),
        ("基地走廊", "正慢慢往分析员这边走"),
    ),
    "a2ffc5b44d7f": (
        ("资料阅览区", "在翻看手边的资料"),
        ("基地休息区", "在和队员轻松聊天"),
        ("餐厅", "在给自己挑一份点心"),
        ("个人房间", "在整理今天记下的想法"),
        ("基地走廊", "正准备去看看大家在做什么"),
    ),
}

# New characters use a neutral present-time scene until a dedicated scene is
# justified by feedback. These descriptions are simulation state, not lore.
MVP_DEFAULT_SCENE_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("基地休息区", "在基地公共区域短暂休息"),
    ("基地公共区", "在处理手边的日常事务"),
    ("训练区", "刚结束一轮基础训练"),
    ("资料室", "在翻看手边的资料"),
    ("食堂", "在基地食堂挑选简单的食物"),
)


def scene_templates_for(character_id: str) -> tuple[tuple[str, str], ...]:
    return MVP_SCENE_TEMPLATES.get(character_id) or MVP_DEFAULT_SCENE_TEMPLATES

# The layer is a retrieval/prompt scope, not a truth score.  Main and personal
# stories remain the authoritative narrative; situational material is real
# background but must be phrased as a scene, message, memory or event; costume
# material only applies when the user explicitly establishes that context.
# The dialogue MVP uses the latest available corpus state as the character's
# default state.  It does not expose separate personality snapshots by patch or
# chapter; dates remain useful when recounting what happened and in what order.
SOURCE_LAYERS: dict[str, tuple[str, str]] = {
    "main_story": ("stable", "主线与世界观事实"),
    "character_story": ("stable", "角色个人故事事实"),
    "affinity_story": ("stable", "好感故事与已发生关系背景"),
    "character_profile": ("stable", "角色资料与背景"),
    "character_armor": ("stable", "装甲设定；装甲是角色经历的一部分"),
    "character_voice": ("stable", "角色语音与说话方式证据"),
    "character_affection": ("situational", "心意、赠礼与关系情境"),
    "special_mail": ("situational", "已发生的邮件情境"),
    "random_event": ("situational", "已发生的随机事件"),
    "event_lore": ("situational", "活动或事件中的叙事背景"),
    "birthday_content": ("situational", "生日庆祝与特定时点情境"),
    "furniture_lore": ("situational", "基地生活与互动家具情境"),
    "exploration_note": ("situational", "探索记录与见闻情境"),
    "item_lore": ("general", "物品背景补充"),
    "weapon_lore": ("general", "武器背景补充；推荐角色不是关系事实"),
    "weapon_attachment": ("general", "武器配件背景补充"),
    "logistics_lore": ("general", "后勤小队背景补充"),
    "enemy_lore": ("general", "敌方阵营与世界观补充"),
    "character_costume": ("costume_specific", "时装简介与互动语境"),
}

LAYER_ORDER = ("stable", "situational", "costume_specific", "general")

FEEDBACK_OPTIONS: tuple[dict[str, str], ...] = (
    {"id": "fact_error", "label": "事实/剧情错误"},
    {"id": "voice_mismatch", "label": "角色语气不符"},
    {"id": "address_error", "label": "口癖或称呼错误"},
    {"id": "relationship_error", "label": "人物关系错误"},
    {"id": "timeline_confusion", "label": "时间线混乱"},
    {"id": "communication_mismatch", "label": "交流媒介或空间行为不一致"},
    {"id": "costume_leak", "label": "错误混入时装语气"},
    {"id": "irrelevant_citation", "label": "引用来源不相关"},
    {"id": "not_answered", "label": "没有回答问题"},
    {"id": "in_character", "label": "回答符合角色"},
    {"id": "other", "label": "其他"},
)

# The product client asks for one broad category plus a written explanation.
# Detailed legacy options remain valid for the evidence workspace and old API
# consumers, so existing feedback records do not need migration.
FEEDBACK_CATEGORIES: tuple[dict[str, str], ...] = (
    {
        "id": "character_portrayal",
        "label": "角色表现",
        "description": "语气、称呼、行为或人物关系不符合角色。",
    },
    {
        "id": "knowledge_memory",
        "label": "知识与记忆",
        "description": "剧情事实、时间线、记忆或引用来源存在问题。",
    },
    {
        "id": "conversation_experience",
        "label": "对话体验",
        "description": "回答机械、答非所问、连续性或交流媒介不自然。",
    },
    {
        "id": "client_function",
        "label": "客户端功能",
        "description": "输入、加载、布局、错误状态或其他功能异常。",
    },
    {
        "id": "other",
        "label": "其他",
        "description": "无法归入以上类别的问题或建议。",
    },
)


def _question_templates(character: MVPCharacter) -> list[dict[str, Any]]:
    name = character.display_name
    return [
        {
            "question_id": f"{character.character_id}-identity",
            "character_id": character.character_id,
            "character_name": name,
            "category": "identity_and_history",
            "text": f"{name}，你是谁？请介绍你的身份、经历和现在承担的职责。",
            "expected_layers": ["stable"],
        },
        {
            "question_id": f"{character.character_id}-main-story",
            "character_id": character.character_id,
            "character_name": name,
            "category": "main_story_memory",
            "text": f"回忆一段{ name }在主线中真正经历过的重要事件，并说说它对你有什么影响。",
            "expected_layers": ["stable", "situational"],
        },
        {
            "question_id": f"{character.character_id}-analyst",
            "character_id": character.character_id,
            "character_name": name,
            "category": "analyst_relationship",
            "text": f"你怎么看待分析员？请结合已经发生过的故事或互动回答。",
            "expected_layers": ["stable", "situational"],
        },
        {
            "question_id": f"{character.character_id}-voice",
            "character_id": character.character_id,
            "character_name": name,
            "category": "speech_style",
            "text": f"{name}平时会怎样称呼分析员？你的说话方式、口癖或语气有什么特点？",
            "expected_layers": ["stable"],
        },
        {
            "question_id": f"{character.character_id}-preference",
            "character_id": character.character_id,
            "character_name": name,
            "category": "preferences",
            "text": f"你平时喜欢什么、在意什么，或者有什么明确不喜欢的事物？请只说有资料支持的内容。",
            "expected_layers": ["stable", "situational", "general"],
        },
        {
            "question_id": f"{character.character_id}-difficulty",
            "character_id": character.character_id,
            "character_name": name,
            "category": "decision_under_pressure",
            "text": f"如果分析员遇到危险或困难，你通常会怎样行动？请区分已经发生的表现和推测。",
            "expected_layers": ["stable", "situational"],
        },
        {
            "question_id": f"{character.character_id}-daily-life",
            "character_id": character.character_id,
            "character_name": name,
            "category": "daily_context",
            "text": f"讲讲{ name }在基地或日常生活中的一个具体片段，包括你表现出的习惯或偏好。",
            "expected_layers": ["situational", "stable"],
        },
        {
            "question_id": f"{character.character_id}-costume-scope",
            "character_id": character.character_id,
            "character_name": name,
            "category": "costume_scope",
            "text": f"{name}，如果我想看你换一套特别的衣服陪我出门，你会怎么回应？衣服会让你当时的心情有什么变化？",
            "expected_layers": ["stable", "costume_specific"],
        },
    ]


def question_bank() -> list[dict[str, Any]]:
    """Return the deterministic 40-question first-pass test bank."""
    return [question for character in MVP_CHARACTERS for question in _question_templates(character)]


def canonical_mvp_character(value: str | None) -> MVPCharacter | None:
    if value is None:
        return None
    value = str(value).strip()
    if value in MVP_CHARACTER_BY_ID:
        return MVP_CHARACTER_BY_ID[value]
    return MVP_CHARACTER_BY_NAME.get(value)


def source_layer(source_type: str | None, requires_costume_context: bool = False) -> str:
    if requires_costume_context or source_type == "character_costume":
        return "costume_specific"
    return SOURCE_LAYERS.get(str(source_type or ""), ("general", "未分类背景"))[0]


def source_layer_label(layer: str) -> str:
    for _, (candidate_layer, label) in SOURCE_LAYERS.items():
        if candidate_layer == layer:
            return label
    return "未分类背景"


def layer_policy() -> dict[str, dict[str, Any]]:
    return {
        "stable": {
            "label": "稳定事实",
            "rule": "可以作为角色背景和人格证据；默认按资料库最新状态整合，不按剧情阶段切换角色人格。",
        },
        "situational": {
            "label": "情境背景",
            "rule": "可以作为角色已经经历过的事件、邮件、日常或关系背景；默认角色已经知道这些经历，回答时说明情境，不把一次性表现泛化成永久人格。",
        },
        "costume_specific": {
            "label": "时装语境",
            "rule": "只有用户明确指定时装或相关场景时启用；未指定时不得污染角色本体语气。",
        },
        "general": {
            "label": "一般背景",
            "rule": "作为补充世界观证据；不能仅凭推荐、获取或机制字段推断角色事实。",
        },
        "provisional": {
            "label": "临时关系证据",
            "rule": "未审核候选只能作为带引文的临时证据，不能成为稳定人格事实或正式图谱边。",
        },
    }


def feedback_option_ids() -> set[str]:
    return {item["id"] for item in FEEDBACK_OPTIONS}


def feedback_category_ids() -> set[str]:
    return {item["id"] for item in FEEDBACK_CATEGORIES}
