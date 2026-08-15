from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.snow_app.config import Settings
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.public_knowledge import PublicKnowledge
from backend.snow_app.repository import RuntimeRepository
from backend.snow_app.user_fact_store import UserFactStore


EXPECTED_RELATIONSHIPS = {
    "ca0144ccd81b": ("里芙", "亲爱的"),
    "1b0a6b35719a": ("芬妮", "达令"),
    "25b23cb64398": ("凯茜娅", "亲爱的"),
    "673ba6851b05": ("苔丝", "亲爱的"),
    "cf0569ac6de9": ("肴", "郎君"),
    "daab0f4cceb4": ("茉莉安", "亲爱的"),
    "9f5804761c56": ("安卡希雅", "亲爱的"),
    "98322bd505f4": ("辰星", "郎君"),
}


class PublicKnowledgeTests(unittest.TestCase):
    def test_reviewed_relationship_release_is_the_exact_formal_roster(self) -> None:
        knowledge = PublicKnowledge()

        self.assertEqual(knowledge.schema_version, 1)
        self.assertTrue(knowledge.version.startswith("snow-canon-"))
        self.assertEqual(
            {
                item["character_id"]: (
                    item["display_name"],
                    item["preferred_address"],
                )
                for item in knowledge.formal_relationships()
            },
            EXPECTED_RELATIONSHIPS,
        )
        self.assertFalse(knowledge.public_metadata()["policy"]["user_chat_can_modify"])


class UserFactBoundaryTests(unittest.TestCase):
    def test_user_facts_are_structured_and_separate_from_conversations(self) -> None:
        knowledge = PublicKnowledge()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = UserFactStore(root / "user_facts.sqlite3")

            self.assertEqual(store.seed_public_relationships(knowledge), 8)
            self.assertEqual(store.seed_public_relationships(knowledge), 0)
            self.assertEqual(len(store.active_facts()), 8)

            with closing(sqlite3.connect(store.database_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(user_facts)").fetchall()
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }

            self.assertNotIn("messages", tables)
            self.assertNotIn("agent_runs", tables)
            self.assertTrue(
                {"message", "answer", "summary", "scene", "location", "agent_run"}
                .isdisjoint(columns)
            )

    def test_revocation_is_append_only_audited(self) -> None:
        knowledge = PublicKnowledge()
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = UserFactStore(Path(temporary_directory) / "user_facts.sqlite3")
            store.seed_public_relationships(knowledge)
            fact = store.relationship("ca0144ccd81b")
            self.assertIsNotNone(fact)

            self.assertTrue(store.revoke(str(fact["fact_id"]), "test_revocation"))
            self.assertIsNone(store.relationship("ca0144ccd81b"))
            with closing(sqlite3.connect(store.database_path)) as connection:
                events = connection.execute(
                    "SELECT action, detail_json FROM user_fact_events WHERE fact_id=? ORDER BY event_id",
                    (fact["fact_id"],),
                ).fetchall()
            self.assertEqual([row[0] for row in events], ["seeded", "revoked"])
            self.assertIn("test_revocation", events[-1][1])

    def test_mvp_service_places_user_facts_next_to_an_overridden_chat_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = root / "runtime"
            for name in ("lakehouse", "graph", "review"):
                (runtime / name).mkdir(parents=True, exist_ok=True)
            settings = Settings(
                data_root=root / "Data",
                runtime_root=runtime,
                chat_enabled=False,
                embedding_model="",
                allowed_origins=("http://127.0.0.1:8080",),
            )
            chat_path = root / "isolated" / "conversations.sqlite3"
            from unittest.mock import patch

            with patch.dict("os.environ", {"MVP_CHAT_DATABASE_PATH": str(chat_path)}):
                service = MVPService(settings, RuntimeRepository(settings))

            self.assertEqual(
                service.user_fact_store.database_path,
                chat_path.parent / "user_facts.sqlite3",
            )
            self.assertTrue(service.user_fact_store.database_path.exists())
            self.assertNotEqual(
                service.user_fact_store.database_path,
                service.conversation_store.database_path,
            )


if __name__ == "__main__":
    unittest.main()
