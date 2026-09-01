from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.snow_app.config import Settings
from backend.snow_app.persona_export import PersonaExportError, export_bundle


CHARACTER_ID = "ca0144ccd81b"


class PersonaExportTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        runtime = root / "runtime"
        (runtime / "mvp").mkdir(parents=True)
        (runtime / "personas").mkdir(parents=True)
        (runtime / "lakehouse").mkdir(parents=True)
        (runtime / "mvp" / "character_views.jsonl").write_text(
            json.dumps(
                {
                    "character_id": CHARACTER_ID,
                    "character_name": "里芙",
                    "coverage": {"reviewed": 2, "local_path": "C:/private/view.json"},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (runtime / "personas" / "dialogue_style_profiles.jsonl").write_text(
            json.dumps(
                {
                    "character_id": CHARACTER_ID,
                    "sentence_style": {"summary": "简洁、沉着"},
                    "supported_values": [{"claim": "重视证据"}],
                    "user_facts": {"private": True},
                    "local_path": "C:/private/profile.json",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        documents = [
            {
                "document_id": "doc_public",
                "title": "审核后的公开片段",
                "text": "里芙重视清晰证据。",
                "source_type": "character_story",
                "canonical_url": "https://example.invalid/public",
                "metadata": {
                    "related_character_ids": [CHARACTER_ID],
                    "local_path": "C:/private/source.json",
                },
            },
            {
                "document_id": "doc_unscoped",
                "title": "全局内容",
                "text": "不会作为整库转储导出。",
                "metadata": {},
            },
            {
                "document_id": "doc_private_path",
                "title": "本机路径",
                "text": "来源在 C:/Users/example/private.txt",
                "metadata": {"character_id": CHARACTER_ID},
            },
        ]
        (runtime / "lakehouse" / "documents.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in documents),
            encoding="utf-8",
        )
        return Settings(
            data_root=root / "Data",
            runtime_root=runtime,
            chat_enabled=False,
            embedding_model="unused",
            allowed_origins=[],
        )

    def test_zip_export_is_checksummed_and_excludes_private_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "lif.persona-bundle.zip"
            result = export_bundle(
                target,
                self._settings(root),
                character_values=[CHARACTER_ID],
                source_commit="0123456789abcdef",
            )
            self.assertEqual(result["character_count"], 1)
            self.assertEqual(result["document_count"], 1)
            with zipfile.ZipFile(target) as bundle_zip:
                manifest = json.loads(bundle_zip.read("manifest.json"))
                for name, entry in manifest["files"].items():
                    self.assertEqual(
                        hashlib.sha256(bundle_zip.read(name)).hexdigest(), entry["sha256"]
                    )
                payload = b"\n".join(bundle_zip.read(name) for name in bundle_zip.namelist())
            lowered = payload.decode("utf-8").casefold()
            self.assertNotIn("user_facts", lowered)
            self.assertNotIn("local_path", lowered)
            self.assertNotIn("c:/private", lowered)
            self.assertNotIn("doc_unscoped", lowered)
            self.assertNotIn("doc_private_path", lowered)
            self.assertIn("doc_public", lowered)
            self.assertEqual(manifest["source"]["commit"], "0123456789abcdef")

    def test_existing_target_and_unknown_character_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = self._settings(root)
            target = root / "bundle"
            target.mkdir()
            with self.assertRaisesRegex(PersonaExportError, "overwrite"):
                export_bundle(target, settings, source_commit="01234567")
            with self.assertRaisesRegex(PersonaExportError, "Unknown"):
                export_bundle(
                    root / "other.zip",
                    settings,
                    character_values=["not-a-character"],
                    source_commit="01234567",
                )
            with self.assertRaisesRegex(PersonaExportError, "hexadecimal"):
                export_bundle(
                    root / "invalid-commit.zip",
                    settings,
                    character_values=[CHARACTER_ID],
                    source_commit="not-a-commit",
                )


if __name__ == "__main__":
    unittest.main()
