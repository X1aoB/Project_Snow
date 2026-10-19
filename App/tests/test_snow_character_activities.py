from __future__ import annotations

from collections import Counter
import unittest
from unittest.mock import patch

from backend.snow_app.mvp_policy import (
    MVP_CHARACTERS,
    MVP_DEFAULT_SCENE_TEMPLATES,
    MVP_SCENE_TEMPLATES,
    scene_templates_for,
    scene_visual_key,
)
from backend.snow_app.mvp_service import MVPService


LOCATION_KEYS = {
    "观景区": "observation",
    "资料室": "archive",
    "资料阅览区": "archive",
    "医务室附近": "medical",
    "基地走廊": "corridor",
    "个人房间": "quarters",
    "基地休息区": "lounge",
    "基地公共区": "lounge",
    "餐厅": "canteen",
    "食堂": "canteen",
    "训练区": "training",
}

# Synthetic session, frozen expected choices. This protects the complete roster
# ordering, hash-derived start and existing first-unused-location policy.
EXPECTED_SYNTHETIC_WORLD = (
    ("伊切尔", "餐厅", "在细细嚼着排骨"),
    ("克罗瑞娜", "基地休息区", "在暖意里享受片刻茶歇"),
    ("凯茜娅", "观景区", "在为眼前的景色留影"),
    ("卜卜", "个人房间", "在给百宝柜里的东西归位"),
    ("奈莉德", "训练区", "在练习一段弦乐旋律"),
    ("妮塔", "基地走廊", "刚喝完训练后的一口水"),
    ("安卡希雅", "资料室", "在翻看收藏的刀剑展门票"),
    ("恩雅", "资料室", "在翻看准备讲的童话"),
    ("晴", "医务室附近", "在检查机械臂的维护状态"),
    ("猫汐尔", "资料阅览区", "在翻看充满奇思妙想的书"),
    ("琴诺", "资料室", "在记下一道菜的做法"),
    ("瑟瑞斯", "基地公共区", "在看鱼缸里的金鱼游动"),
    ("米娅", "食堂", "在零食柜里找喜欢的冰淇淋"),
    ("肴", "个人房间", "在被炉边懒懒地伸腰"),
    ("胧嫣", "餐厅", "在检查果篮里水果的新鲜程度"),
    ("芙提雅", "资料室", "在琢磨一串二进制暗语"),
    ("芬妮", "餐厅", "在用心挑一套茶具"),
    ("苔丝", "个人房间", "在动手制作新的小道具"),
    ("茉莉安", "个人房间", "在修整花瓶里枝条的形状"),
    ("薇蒂雅", "基地公共区", "在细看海报里的电影画面"),
    ("辰星", "个人房间", "在长案前温习读过的书"),
    ("里芙", "观景区", "在静静推演棋盘上的走法"),
)


class SnowCharacterActivitiesTests(unittest.TestCase):
    def test_all_22_registered_characters_have_dedicated_choices(self) -> None:
        registry_ids = {character.character_id for character in MVP_CHARACTERS}
        self.assertEqual(set(MVP_SCENE_TEMPLATES), registry_ids)
        self.assertEqual(len(registry_ids), 22)
        for character in MVP_CHARACTERS:
            with self.subTest(character=character.display_name):
                templates = scene_templates_for(character.character_id)
                self.assertIs(templates, MVP_SCENE_TEMPLATES[character.character_id])
                self.assertGreaterEqual(len(templates), 8)
                self.assertNotEqual(templates, MVP_DEFAULT_SCENE_TEMPLATES)

    def test_short_present_time_descriptions_do_not_repeat_across_roster(self) -> None:
        descriptions = []
        for character in MVP_CHARACTERS:
            for location, activity in scene_templates_for(character.character_id):
                with self.subTest(character=character.display_name, activity=activity):
                    self.assertIsInstance(location, str)
                    self.assertIsInstance(activity, str)
                    self.assertTrue(activity.startswith(("在", "刚")))
                    self.assertEqual(activity, activity.strip())
                    self.assertLessEqual(len(activity), 20)
                    self.assertNotIn("\n", activity)
                descriptions.append(activity)
        self.assertEqual(len(descriptions), 176)
        self.assertEqual(Counter(descriptions).most_common(1)[0][1], 1)

    def test_locations_use_existing_aliases_and_spread_across_map_regions(self) -> None:
        all_regions = set()
        for character in MVP_CHARACTERS:
            regions = set()
            for location, _ in scene_templates_for(character.character_id):
                with self.subTest(character=character.display_name, location=location):
                    self.assertIn(location, LOCATION_KEYS)
                    key = scene_visual_key(location)
                    self.assertEqual(key, LOCATION_KEYS[location])
                    regions.add(key)
            self.assertGreaterEqual(len(regions), 5, character.display_name)
            all_regions.update(regions)
        self.assertEqual(all_regions, set(LOCATION_KEYS.values()))

    def test_unknown_character_and_existing_location_aliases_remain_compatible(self) -> None:
        self.assertEqual(
            scene_templates_for("synthetic-unknown-character"),
            (
                ("基地休息区", "在基地公共区域短暂休息"),
                ("基地公共区", "在处理手边的日常事务"),
                ("训练区", "刚结束一轮基础训练"),
                ("资料室", "在翻看手边的资料"),
                ("食堂", "在基地食堂挑选简单的食物"),
            ),
        )
        for alias, expected in {
            **LOCATION_KEYS,
            "宿舍": "quarters",
            "阅览区": "archive",
            "医务室": "medical",
            "走廊": "corridor",
            "": "generic",
            "synthetic-unmapped-location": "generic",
        }.items():
            with self.subTest(alias=alias):
                self.assertEqual(scene_visual_key(alias), expected)

    def test_synthetic_world_preserves_deterministic_selection_and_wire_shape(self) -> None:
        with patch.dict("backend.snow_app.mvp_service._WORLD_STATES", {}, clear=True):
            world = MVPService._world_snapshot("activity-map-regression-v1")
            repeated = MVPService._world_snapshot("activity-map-regression-v1")
        self.assertEqual(world, repeated)
        self.assertEqual(
            set(world), {"world_session_id", "created_at", "analyst_location", "presence"}
        )
        self.assertIsNone(world["analyst_location"])
        self.assertEqual(
            tuple(
                (presence["character_name"], presence["location"], presence["activity"])
                for presence in world["presence"].values()
            ),
            EXPECTED_SYNTHETIC_WORLD,
        )
        for character_id, presence in world["presence"].items():
            self.assertEqual(
                set(presence),
                {"character_id", "character_name", "location", "activity", "state_scope"},
            )
            self.assertEqual(presence["character_id"], character_id)
            self.assertEqual(presence["state_scope"], "session_simulation")

    def test_synthetic_new_worlds_never_show_duplicate_activities(self) -> None:
        with patch.dict("backend.snow_app.mvp_service._WORLD_STATES", {}, clear=True):
            for index in range(64):
                world = MVPService._world_snapshot(f"synthetic-map-activities-{index}")
                with self.subTest(world=index):
                    presence = world["presence"]
                    self.assertEqual(len(presence), 22)
                    self.assertEqual(len({p["activity"] for p in presence.values()}), 22)
                    for character_id, item in presence.items():
                        self.assertIn(
                            (item["location"], item["activity"]),
                            scene_templates_for(character_id),
                        )

    def test_existing_session_activity_is_not_rewritten_and_return_is_a_copy(self) -> None:
        synthetic_world = {
            "world_session_id": "synthetic-existing-world",
            "created_at": "2026-10-18T00:00:00+00:00",
            "analyst_location": "资料室",
            "presence": {
                "ca0144ccd81b": {
                    "character_id": "ca0144ccd81b",
                    "character_name": "里芙",
                    "location": "基地休息区",
                    "activity": "在窗边安静地休息",
                    "state_scope": "session_simulation",
                }
            },
        }
        with patch.dict(
            "backend.snow_app.mvp_service._WORLD_STATES",
            {"synthetic-existing-world": synthetic_world},
            clear=True,
        ):
            first = MVPService._world_snapshot("synthetic-existing-world")
            self.assertEqual(first, synthetic_world)
            first["presence"]["ca0144ccd81b"]["activity"] = "synthetic-client-mutation"
            second = MVPService._world_snapshot("synthetic-existing-world")
        self.assertEqual(second, synthetic_world)
        self.assertEqual(second["presence"]["ca0144ccd81b"]["activity"], "在窗边安静地休息")


if __name__ == "__main__":
    unittest.main()
