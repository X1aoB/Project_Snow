from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from backend.snow_app.chat_store import ConversationStore
from backend.snow_app.config import Settings
from backend.snow_app.mvp_service import MVPService
from backend.snow_app.repository import RuntimeRepository


APP_ROOT = Path(__file__).resolve().parents[1]


def response(message_id: str, *, mode: str = "immersive", channel: str = "text") -> dict:
    answer = f"reply for {message_id}"
    return {
        "message_id": message_id,
        "character_id": "ca0144ccd81b",
        "character_name": "里芙",
        "session_id": "session_liv",
        "world_session_id": "world_shared",
        "mode": mode,
        "communication_channel": channel,
        "answer": answer,
        "content_blocks": [
            {"type": "message" if channel == "text" else "speech", "text": answer}
        ],
    }


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "chat" / "conversations.sqlite3"
        self.store = ConversationStore(self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def save(
        self,
        message_id: str,
        *,
        client_message_id: str,
        mode: str = "immersive",
        channel: str = "text",
    ) -> str:
        return self.store.save_exchange(
            character_id="ca0144ccd81b",
            session_id="session_liv",
            world_session_id="world_shared",
            client_message_id=client_message_id,
            user_text=f"question for {message_id}",
            response=response(message_id, mode=mode, channel=channel),
            session_state={
                "character_id": "ca0144ccd81b",
                "communication_channel": channel,
                "mode": mode,
                "turns": [],
                "mode_turns": {"immersive": [], "assistant": []},
            },
            world_state={"world_session_id": "world_shared", "analyst_location": "基地公共区"},
        )

    def test_history_and_state_survive_store_restart(self) -> None:
        conversation_id = self.save("assistant_one", client_message_id="client_one")

        restarted = ConversationStore(self.database_path)
        history = restarted.history("ca0144ccd81b", limit=20)

        self.assertEqual(history["conversation"]["conversation_id"], conversation_id)
        self.assertEqual([item["role"] for item in history["messages"]], ["user", "assistant"])
        self.assertEqual(restarted.session_state("session_liv")["character_id"], "ca0144ccd81b")
        self.assertEqual(restarted.world_state("world_shared")["analyst_location"], "基地公共区")
        self.assertEqual(restarted.active_world_session_id(), "world_shared")

    def test_idempotency_claim_and_saved_response(self) -> None:
        self.assertTrue(self.store.claim_request("client_claim", "ca0144ccd81b"))
        self.assertFalse(self.store.claim_request("client_claim", "ca0144ccd81b"))
        self.store.release_request("client_claim")
        self.assertTrue(self.store.claim_request("client_claim", "ca0144ccd81b"))
        self.store.release_request("client_claim")

        self.save("assistant_saved", client_message_id="client_saved")
        duplicate = self.store.duplicate_response("client_saved")
        self.assertIsNotNone(duplicate)
        self.assertTrue(duplicate["persisted"])
        self.assertEqual(duplicate["message_id"], "assistant_saved")

    def test_history_pagination_uses_stable_row_cursor(self) -> None:
        for index in range(3):
            self.save(f"assistant_{index}", client_message_id=f"client_{index}")

        latest = self.store.history("ca0144ccd81b", limit=2)
        self.assertEqual(len(latest["messages"]), 2)
        self.assertIsNotNone(latest["next_before"])
        older = self.store.history(
            "ca0144ccd81b", before=latest["next_before"], limit=2
        )
        self.assertEqual(len(older["messages"]), 2)
        self.assertTrue(
            {item["message_id"] for item in latest["messages"]}.isdisjoint(
                {item["message_id"] for item in older["messages"]}
            )
        )

    def test_mode_clear_preserves_other_mode_and_full_clear_removes_conversation(self) -> None:
        self.save("assistant_immersive", client_message_id="client_immersive")
        self.save(
            "assistant_assistant",
            client_message_id="client_assistant",
            mode="assistant",
        )

        cleared = self.store.clear("ca0144ccd81b", "immersive")
        self.assertTrue(cleared["cleared"])
        remaining = self.store.history("ca0144ccd81b", limit=20)
        self.assertEqual({item["mode"] for item in remaining["messages"]}, {"assistant"})

        self.store.clear("ca0144ccd81b")
        self.assertIsNone(self.store.history("ca0144ccd81b")["conversation"])
        self.assertIsNone(self.store.duplicate_response("client_assistant"))

    def test_session_id_cannot_be_reused_for_another_character(self) -> None:
        self.save("assistant_one", client_message_id="client_one")
        with self.assertRaisesRegex(ValueError, "其他角色"):
            self.store.save_exchange(
                character_id="different_character",
                session_id="session_liv",
                world_session_id="world_shared",
                client_message_id="client_two",
                user_text="question",
                response={**response("assistant_two"), "character_id": "different_character"},
                session_state={"character_id": "different_character"},
                world_state={"world_session_id": "world_shared"},
            )


class FeedbackWorkflowTests(unittest.TestCase):
    def test_broad_feedback_category_and_append_only_triage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_root = Path(temporary_directory)
            for directory in ("lakehouse", "graph", "review"):
                (runtime_root / directory).mkdir(parents=True, exist_ok=True)
            (runtime_root / "lakehouse" / "documents.jsonl").write_text("", encoding="utf-8")
            (runtime_root / "graph" / "nodes.jsonl").write_text("", encoding="utf-8")
            (runtime_root / "graph" / "edges.jsonl").write_text("", encoding="utf-8")
            (runtime_root / "mvp").mkdir(parents=True, exist_ok=True)
            (runtime_root / "mvp" / "character_views.jsonl").write_text(
                '{"character_id":"ca0144ccd81b","coverage":{}}\n',
                encoding="utf-8",
            )
            settings = Settings(
                data_root=APP_ROOT.parent / "Data",
                runtime_root=runtime_root,
                chat_enabled=False,
                embedding_model="unused",
                allowed_origins=("http://127.0.0.1:8080",),
            )
            service = MVPService(settings, RuntimeRepository(settings))

            item = service.append_feedback(
                "里芙",
                "session_feedback",
                "message_feedback",
                [],
                "回答的关系称呼不正确。",
                category="character_portrayal",
                mode="immersive",
                communication_channel="text",
                client_version="preview-0.2.0",
            )
            service.triage_feedback(item["feedback_id"], "planned", "纳入下一轮修复")
            result = service.feedback(
                category="character_portrayal", feedback_status="planned"
            )

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["feedback"][0]["triage_note"], "纳入下一轮修复")
            self.assertEqual(
                len((runtime_root / "mvp" / "feedback_triage.jsonl").read_text(encoding="utf-8").splitlines()),
                1,
            )


if __name__ == "__main__":
    unittest.main()
