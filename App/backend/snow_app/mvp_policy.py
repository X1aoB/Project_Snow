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
    search_tokens: tuple[str, ...] = ()
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
            tuple(dict.fromkeys(str(token).strip().casefold() for token in record.get("search_tokens", []) if str(token).strip())),
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
    "78aa7ab99154": (  # 伊切尔
        ("训练区", "在练习攀爬时的平衡"),
        ("资料室", "在复习黑板上的功课"),
        ("个人房间", "在细心整理随身的武器"),
        ("基地休息区", "在暖炉旁蜷着身子取暖"),
        ("观景区", "在端详鹿角模型"),
        ("餐厅", "在细细嚼着排骨"),
        ("基地走廊", "在检查随身的露营用品"),
        ("基地公共区", "在试着荡一会儿秋千"),
    ),
    "d5ecfceba959": (  # 克罗瑞娜
        ("资料阅览区", "在翻读一本有趣的书"),
        ("餐厅", "在细选配茶的小点心"),
        ("观景区", "在欣赏花朵映出的光影"),
        ("基地走廊", "在听立钟轻缓的报时声"),
        ("个人房间", "在试闻一款香水的味道"),
        ("基地休息区", "在暖意里享受片刻茶歇"),
        ("训练区", "在借一把伞练习舞步"),
        ("基地公共区", "在挑选照片的展示方式"),
    ),
    "25b23cb64398": (  # 凯茜娅
        ("个人房间", "在试弹贝斯的一小段旋律"),
        ("基地公共区", "在挑一张适合放松的唱片"),
        ("训练区", "在敲一段电鼓节拍"),
        ("餐厅", "在斟一小杯珍藏的酒"),
        ("基地休息区", "在调一杯合心意的饮品"),
        ("观景区", "在为眼前的景色留影"),
        ("基地走廊", "在琢磨下次冲浪的动作"),
        ("个人房间", "在飘窗软垫上舒展双腿"),
    ),
    "6455a5dcff6a": (  # 卜卜
        ("资料室", "在推敲手边的一支签文"),
        ("基地休息区", "在摆弄皮影戏的小机关"),
        ("训练区", "在对着面具练习剑法"),
        ("观景区", "在为盆栽挑一个好方位"),
        ("个人房间", "在端详香炉里的袅袅轻烟"),
        ("基地走廊", "在欣赏朔州式样的小挂饰"),
        ("个人房间", "在给百宝柜里的东西归位"),
        ("资料阅览区", "在细看扇面上的山水"),
    ),
    "41b7444e39cc": (  # 奈莉德
        ("基地休息区", "在切换水族投影的深海景色"),
        ("训练区", "在练习一段弦乐旋律"),
        ("资料室", "在端详收集来的植物标本"),
        ("个人房间", "在闻培养瓶透出的植物香气"),
        ("基地公共区", "在观察水母夜灯的柔光"),
        ("餐厅", "在吧台慢慢调一杯饮品"),
        ("观景区", "在对着远景轻哼曲调"),
        ("基地走廊", "在回想鱼骨吊饰里的寓言"),
    ),
    "4370a74d6fda": (  # 妮塔
        ("训练区", "在试一试哑铃的分量"),
        ("资料室", "在盯着股票走势仔细琢磨"),
        ("餐厅", "在翻找还没过期的优惠券"),
        ("医务室附近", "在整理备用的抑制剂"),
        ("基地休息区", "在掂着骰子练手感"),
        ("个人房间", "在擦拭金灿灿的招财猫"),
        ("基地走廊", "刚喝完训练后的一口水"),
        ("训练区", "在和沙袋较一较劲"),
    ),
    "9f5804761c56": (  # 安卡希雅
        ("个人房间", "在挑战游戏里的难关"),
        ("基地休息区", "在回味刚打过的游戏关卡"),
        ("资料室", "在翻看收藏的刀剑展门票"),
        ("训练区", "在练习刀剑的基本动作"),
        ("餐厅", "在尝试没吃过的零食口味"),
        ("基地走廊", "在挑下一部想看的老电影"),
        ("观景区", "在听音响里熟悉的旋律"),
        ("个人房间", "在欣赏刚摆好的游戏海报"),
    ),
    "43f05917bfa1": (  # 恩雅
        ("资料室", "在翻看准备讲的童话"),
        ("餐厅", "在调配烤饼干用的面糊"),
        ("基地休息区", "在用积木拼一座游乐园"),
        ("观景区", "在观察生态箱里的小生命"),
        ("基地走廊", "在调试展示画作的投影"),
        ("个人房间", "在抱着熊熊玩偶歇一会儿"),
        ("训练区", "在轻推不倒翁试试回弹"),
        ("餐厅", "在摆好刚烤出的茶点"),
    ),
    "921f9ef0cc4e": (  # 晴
        ("训练区", "在认真做柔韧性练习"),
        ("训练区", "在调整打沙袋时的姿势"),
        ("医务室附近", "在检查机械臂的维护状态"),
        ("资料阅览区", "在整理尚未摆出的勋章"),
        ("个人房间", "在仔细收好珍藏的发夹"),
        ("基地休息区", "在调试奖杯模型的投影"),
        ("观景区", "在照看刚开始培育的花卉"),
        ("基地走廊", "在核对随身修护液的余量"),
    ),
    "6862c43d2ac9": (  # 猫汐尔
        ("资料室", "在琢磨古拉利卡书中的谜题"),
        ("资料阅览区", "在翻看充满奇思妙想的书"),
        ("训练区", "在亲手测量手边的物件"),
        ("个人房间", "在补写电子日志里的记录"),
        ("基地休息区", "在地毯上放空一会儿"),
        ("观景区", "在用天秤比较羽毛的重量"),
        ("基地走廊", "在观察零区地图的投影"),
        ("个人房间", "在把箱中物品分到收纳架上"),
    ),
    "8d5b5c3912bb": (  # 琴诺
        ("餐厅", "在用厨具试做一份小菜"),
        ("餐厅", "在为餐桌多留一个座位"),
        ("个人房间", "在叠好柔软的摇粒绒毯"),
        ("基地休息区", "在摆弄一对企鹅手偶"),
        ("资料室", "在记下一道菜的做法"),
        ("观景区", "在抱着靠枕安静看风景"),
        ("基地走廊", "在检查自己的通讯装置"),
        ("个人房间", "在清点食品柜里的储备"),
    ),
    "85b205f6f623": (  # 瑟瑞斯
        ("观景区", "在用拍立得捕捉眼前风景"),
        ("个人房间", "在把珍惜的照片贴上木板"),
        ("资料室", "在回听录音笔里的小事"),
        ("基地休息区", "在拼一幅自己印制的拼图"),
        ("餐厅", "在录下不想忘记的日常"),
        ("训练区", "在练习拍照时的取景"),
        ("基地走廊", "在核对平板上的见面记录"),
        ("基地公共区", "在看鱼缸里的金鱼游动"),
    ),
    "702f4375675b": (  # 米娅
        ("个人房间", "在摆好床边的台灯和书本"),
        ("餐厅", "在给水果拼出新鲜造型"),
        ("基地休息区", "在摸云朵装饰软软的表面"),
        ("观景区", "在窗边地毯上享受微风"),
        ("资料室", "在回看去过的地方的留影"),
        ("训练区", "在轻轻练习一段舞步"),
        ("基地走廊", "在记事板上写下今日心情"),
        ("食堂", "在零食柜里找喜欢的冰淇淋"),
    ),
    "cf0569ac6de9": (  # 肴
        ("基地休息区", "在躺椅上找最省力的姿势"),
        ("个人房间", "在被炉边懒懒地伸腰"),
        ("资料室", "在盘算下一次调休的安排"),
        ("资料阅览区", "在翻看想去的风景海报"),
        ("观景区", "在慢慢摆放露营的小物件"),
        ("训练区", "在瑜伽垫上舒展身体"),
        ("基地走廊", "刚收好今天要挂的海报"),
        ("个人房间", "在琢磨给绿植省心浇水的办法"),
    ),
    "447ed3c401c9": (  # 胧嫣
        ("资料室", "在整理手边的商会文书"),
        ("基地休息区", "在推敲一笔买卖的得失"),
        ("餐厅", "在检查果篮里水果的新鲜程度"),
        ("食堂", "在给杯中的花茶续水"),
        ("基地公共区", "在挑一份配茶的点心"),
        ("个人房间", "在斟酌待批复的文件"),
        ("个人房间", "在辨着香炉里飘来的气味"),
        ("观景区", "在窗边歇一会儿回回神"),
    ),
    "a2ffc5b44d7f": (  # 芙提雅
        ("资料室", "在琢磨一串二进制暗语"),
        ("个人房间", "在工作台上摆弄手边的零件"),
        ("训练区", "在跑步机上给自己加把劲"),
        ("餐厅", "在挑一罐补充精神的饮品"),
        ("基地休息区", "在抱着方块抱枕想事情"),
        ("观景区", "在观察常青绿植的新叶"),
        ("基地走廊", "在研究电子钟的提醒设置"),
        ("资料阅览区", "在核对辅助机整备的步骤"),
    ),
    "1b0a6b35719a": (  # 芬妮
        ("训练区", "在重看一段作战记录"),
        ("资料室", "在给最近的训练做总结"),
        ("个人房间", "在给首饰挑合适的收纳位置"),
        ("观景区", "在照着窗影调整帽檐"),
        ("基地休息区", "在试一段乐器的新旋律"),
        ("餐厅", "在用心挑一套茶具"),
        ("基地走廊", "在想今天的装束搭配"),
        ("个人房间", "在为收藏品腾出展柜的位置"),
    ),
    "673ba6851b05": (  # 苔丝
        ("个人房间", "在检查下一场魔术的小道具"),
        ("训练区", "在反复练习一段魔术手法"),
        ("基地休息区", "在试探惊吓盒子的机关"),
        ("资料室", "在整理照片海报和奖状"),
        ("餐厅", "在准备一杯不苦的奶茶"),
        ("观景区", "在陪魔术搭档兔子玩耍"),
        ("基地走廊", "在拢好出门时披着的风衣"),
        ("个人房间", "在动手制作新的小道具"),
    ),
    "daab0f4cceb4": (  # 茉莉安
        ("训练区", "在做反应力与射击练习"),
        ("餐厅", "在用摩卡壶慢慢煮咖啡"),
        ("资料室", "在翻看珍藏的实体门票"),
        ("个人房间", "在修整花瓶里枝条的形状"),
        ("基地休息区", "在为茶歇布置舒适的座位"),
        ("观景区", "在挑选相框里展示的影像"),
        ("基地走廊", "在看一页军火展览的介绍"),
        ("个人房间", "在重温收藏的电影蓝光碟"),
    ),
    "5157b8972632": (  # 薇蒂雅
        ("个人房间", "在享受独自看电影的时光"),
        ("基地休息区", "在随着唱片的旋律轻哼"),
        ("资料室", "在读一本让人脸红的小说"),
        ("资料阅览区", "在琢磨故事里的人物设定"),
        ("观景区", "在欣赏带着热带气息的绿植"),
        ("基地走廊", "在听机械风扇轻轻转动"),
        ("个人房间", "在镜前挑选要穿的衣服"),
        ("基地公共区", "在细看海报里的电影画面"),
    ),
    "98322bd505f4": (  # 辰星
        ("训练区", "在依着口诀调整呼吸"),
        ("资料室", "在安静研读纸质古籍"),
        ("资料阅览区", "在临摹投影里的一行字"),
        ("个人房间", "在辨别熏香细微的香气"),
        ("基地休息区", "在端详绳结纹样的寓意"),
        ("观景区", "在斟酌山水造景的布局"),
        ("基地走廊", "在整理带来的小收藏"),
        ("个人房间", "在长案前温习读过的书"),
    ),
    "ca0144ccd81b": (  # 里芙
        ("训练区", "在跑步机上保持平稳的步频"),
        ("训练区", "在反复调整投飞镖的姿势"),
        ("资料室", "在核对兵人棋盘的新规则"),
        ("个人房间", "在把旧照片扫描进相册"),
        ("基地休息区", "在试着听一张复古唱片"),
        ("基地走廊", "在检查无人机支架的连接"),
        ("医务室附近", "在放松训练后紧绷的肌肉"),
        ("观景区", "在静静推演棋盘上的走法"),
    ),
}

# Unknown IDs retain the neutral fallback. All 22 registered characters have
# dedicated present-time templates; source rationale is in snow-character-activities.md.
MVP_DEFAULT_SCENE_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("基地休息区", "在基地公共区域短暂休息"),
    ("基地公共区", "在处理手边的日常事务"),
    ("训练区", "刚结束一轮基础训练"),
    ("资料室", "在翻看手边的资料"),
    ("食堂", "在基地食堂挑选简单的食物"),
)


def scene_templates_for(character_id: str) -> tuple[tuple[str, str], ...]:
    return MVP_SCENE_TEMPLATES.get(character_id) or MVP_DEFAULT_SCENE_TEMPLATES


def scene_visual_key(location: str | None) -> str:
    """Map simulated locations to stable, presentation-only scene keys."""

    normalized = str(location or "").strip()
    if not normalized:
        return "generic"
    mappings = (
        (("个人房间", "宿舍"), "quarters"),
        (("休息区", "公共区"), "lounge"),
        (("训练区",), "training"),
        (("资料室", "资料阅览区", "阅览区"), "archive"),
        (("餐厅", "食堂"), "canteen"),
        (("观景区",), "observation"),
        (("医务室",), "medical"),
        (("走廊",), "corridor"),
    )
    for terms, key in mappings:
        if any(term in normalized for term in terms):
            return key
    return "generic"

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
