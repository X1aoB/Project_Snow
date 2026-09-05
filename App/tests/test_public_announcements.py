from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from unittest import TestCase


APP_ROOT = Path(__file__).resolve().parents[1]


class PublicAnnouncementCatalogTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = json.loads((APP_ROOT / "public_frontend/announcements.json").read_text(encoding="utf-8"))
        cls.registry = json.loads((APP_ROOT / "backend/snow_app/mvp_character_registry.json").read_text(encoding="utf-8"))["characters"]
        cls.sources = json.loads((APP_ROOT / "config/public_knowledge/data_license_review.json").read_text(encoding="utf-8"))["sources"]

    def test_birthdays_cover_every_registered_character_with_reviewed_sources(self):
        expected = {item["character_id"]: item["display_name"] for item in self.registry}
        actual = {item["character_id"]: item["display_name"] for item in self.feed["birthdays"]}
        self.assertEqual(actual, expected)
        self.assertEqual(len(self.feed["birthdays"]), len(expected))
        reviewed = {source["fixed_revision_url"] for source in self.sources}
        for item in self.feed["birthdays"]:
            with self.subTest(character=item["display_name"]):
                date(2024, item["month"], item["day"])
                self.assertIn(item["source_url"], reviewed)
                self.assertTrue(item["source_url"].startswith("https://wiki.biligame.com/sonw/"))

    def test_birthdays_match_original_profile_or_armor_fields_when_available(self):
        if not (APP_ROOT.parent / "Data/Source/character_armors").is_dir():
            self.skipTest("Original local data package is not present in portable CI")
        sources = {source["fixed_revision_url"]: source for source in self.sources}
        for item in self.feed["birthdays"]:
            matches = set()
            for local in sources[item["source_url"]]["local_sources"]:
                path = APP_ROOT.parent / local["local_path"]
                if not path.is_file():
                    continue
                body = path.read_text(encoding="utf-8")
                pairs = re.findall(r"<th[^>]*>\s*生日\s*</th>\s*<td[^>]*>\s*(\d+)月(\d+)日", body)
                pairs += re.findall(r"\|\s*生日\s*=\s*(\d+)月(\d+)日", body)
                matches.update((int(month), int(day)) for month, day in pairs)
            self.assertEqual(matches, {(item["month"], item["day"])}, item["display_name"])

    def test_updates_have_stable_identifiers_dates_and_plain_text(self):
        self.assertEqual(self.feed["schema_version"], 1)
        self.assertEqual(self.feed["timezone"], "Asia/Shanghai")
        updates = self.feed["updates"]
        self.assertTrue(updates)
        self.assertEqual(len(updates), len({item["id"] for item in updates}))
        for item in updates:
            self.assertRegex(item["id"], r"^[a-zA-Z0-9._-]{1,96}$")
            self.assertIsNotNone(datetime.fromisoformat(item["published_at"]).tzinfo)
            if item.get("updated_at"):
                self.assertIsNotNone(datetime.fromisoformat(item["updated_at"]).tzinfo)
            self.assertTrue(item["items"])
            for value in [item["title"], item["summary"], *item["items"]]:
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip())
                self.assertNotRegex(value, r"<[^>]+>")

    def test_notice_feed_revalidates_without_model_credentials(self):
        caddy = (APP_ROOT / "infra/Caddyfile").read_text(encoding="utf-8")
        matcher = next(line for line in caddy.splitlines() if "@frontend_assets path" in line)
        self.assertIn("/announcements.json", matcher)
        self.assertIn('header @frontend_assets Cache-Control "no-store, max-age=0"', caddy)
