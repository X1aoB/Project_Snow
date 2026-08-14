from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import backend.snow_app.main as main_module
from backend.snow_app.persona_gateway import PersonaPairingStore


class PersonaGatewayTests(unittest.TestCase):
    def test_pairing_tokens_are_hashed_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = PersonaPairingStore(Path(temporary_directory) / "pairings.sqlite3")
            created = store.create("Codex test", "ca0144ccd81b")
            token = created["pairing_token"]

            self.assertNotIn(token, store.database_path.read_bytes().decode("utf-8", errors="ignore"))
            authenticated = store.authenticate(token)
            self.assertEqual(authenticated["pairing_id"], created["pairing_id"])
            self.assertTrue(store.revoke(created["pairing_id"], created["pairing_id"]))
            self.assertIsNone(store.authenticate(token))

    def test_non_loopback_request_is_rejected(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/persona/status",
                "headers": [],
                "client": ("192.0.2.15", 41322),
                "server": ("127.0.0.1", 8000),
                "scheme": "http",
                "query_string": b"",
            }
        )
        with self.assertRaises(HTTPException) as raised:
            main_module._require_loopback(request)
        self.assertEqual(raised.exception.status_code, 403)

    def test_gateway_routes_require_a_pairing_and_exclude_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_store = PersonaPairingStore(
                Path(temporary_directory) / "persona_pairings.sqlite3"
            )
            with patch.object(main_module, "persona_pairing_store", temporary_store):
                client = TestClient(main_module.app)
                unauthorized = client.get("/api/v1/persona/snapshot/ca0144ccd81b")
                self.assertEqual(unauthorized.status_code, 401)

                pairing = client.post(
                    "/api/v1/persona/pairings",
                    json={"label": "Codex test", "default_character_id": "ca0144ccd81b"},
                )
                self.assertEqual(pairing.status_code, 200)
                payload = pairing.json()
                headers = {"Authorization": f"Bearer {payload['pairing_token']}"}

                snapshot_response = client.get(
                    "/api/v1/persona/snapshot/ca0144ccd81b", headers=headers
                )
                self.assertEqual(snapshot_response.status_code, 200)
                snapshot = snapshot_response.json()
                self.assertEqual(snapshot["character"]["display_name"], "里芙")
                self.assertEqual(snapshot["relationship"]["preferred_address"], "亲爱的")
                self.assertFalse(snapshot["relationship"]["write_back_allowed"])
                self.assertEqual(snapshot["rendering_rules"]["hidden_reasoning"], "never_return")

                private_keys = {
                    "messages",
                    "conversation_summary",
                    "scene_state",
                    "analyst_location",
                    "character_location",
                    "active_costume",
                    "agent_runs",
                    "tool_logs",
                    "attachments",
                }

                def walk_keys(value):
                    if isinstance(value, dict):
                        for key, nested in value.items():
                            yield key
                            yield from walk_keys(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            yield from walk_keys(nested)

                self.assertTrue(private_keys.isdisjoint(set(walk_keys(snapshot))))

                revoked = client.delete(
                    f"/api/v1/persona/pairings/{payload['pairing_id']}", headers=headers
                )
                self.assertEqual(revoked.status_code, 200)
                self.assertEqual(
                    client.get(
                        "/api/v1/relationships/ca0144ccd81b", headers=headers
                    ).status_code,
                    401,
                )


if __name__ == "__main__":
    unittest.main()
