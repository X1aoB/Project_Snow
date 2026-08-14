from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from migrations.secret_config import load_required_secret


class MigrationSecretConfigTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app_root = Path(__file__).resolve().parents[1]

    def test_reads_database_url_from_compose_secret_file(self) -> None:
        with TemporaryDirectory() as directory:
            secret_file = Path(directory) / "database-url"
            secret_file.write_text("postgresql+psycopg://file-value\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "PUBLIC_DATABASE_URL_FILE": str(secret_file),
                    "PUBLIC_DATABASE_URL": "postgresql+psycopg://environment-value",
                },
                clear=False,
            ):
                self.assertEqual(
                    load_required_secret("PUBLIC_DATABASE_URL"),
                    "postgresql+psycopg://file-value",
                )

    def test_falls_back_to_database_url_environment_value(self) -> None:
        with patch.dict(
            os.environ,
            {"PUBLIC_DATABASE_URL": "postgresql+psycopg://environment-value"},
            clear=False,
        ):
            os.environ.pop("PUBLIC_DATABASE_URL_FILE", None)
            self.assertEqual(
                load_required_secret("PUBLIC_DATABASE_URL"),
                "postgresql+psycopg://environment-value",
            )

    def test_rejects_missing_configured_secret_file(self) -> None:
        with patch.dict(
            os.environ,
            {"PUBLIC_DATABASE_URL_FILE": "missing-database-url"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Unable to read configured secret file"):
                load_required_secret("PUBLIC_DATABASE_URL")

    def test_rejects_empty_configured_secret_file(self) -> None:
        with TemporaryDirectory() as directory:
            secret_file = Path(directory) / "database-url"
            secret_file.write_text("\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"PUBLIC_DATABASE_URL_FILE": str(secret_file)},
                clear=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "is empty"):
                    load_required_secret("PUBLIC_DATABASE_URL")

    def test_rejects_missing_database_url(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PUBLIC_DATABASE_URL", None)
            os.environ.pop("PUBLIC_DATABASE_URL_FILE", None)
            with self.assertRaisesRegex(RuntimeError, "must be configured"):
                load_required_secret("PUBLIC_DATABASE_URL")

    def test_alembic_offline_migration_reads_database_url_file(self) -> None:
        with TemporaryDirectory() as directory:
            secret_file = Path(directory) / "database-url"
            secret_file.write_text(
                "postgresql+psycopg://project_snow:file-value@postgres/project_snow\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.pop("PUBLIC_DATABASE_URL", None)
            environment["PUBLIC_DATABASE_URL_FILE"] = str(secret_file)
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
                cwd=self.app_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("CREATE TABLE IF NOT EXISTS public_feedback", result.stdout)
