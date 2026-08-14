from __future__ import annotations

from pathlib import Path
from unittest import TestCase


class DeploymentContractTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app_root = Path(__file__).resolve().parents[1]

    def read(self, relative: str) -> str:
        return (self.app_root / relative).read_text(encoding="utf-8")

    def test_neo4j_uses_file_backed_auth(self) -> None:
        compose = self.read("compose.prod.yml")
        self.assertIn("NEO4J_AUTH_FILE: /run/secrets/neo4j_auth", compose)
        self.assertIn("/etc/project-snow/secrets/neo4j_auth:/run/secrets/neo4j_auth:ro", compose)
        self.assertNotIn("NEO4J_AUTH:?", compose)

    def test_deploy_promotes_compose_environment_only_after_smoke(self) -> None:
        script = self.read("ops/deploy.sh")
        smoke = script.index("/app/public_smoke.py")
        promote = script.index('mv -f "$candidate_env" "$current_env"')
        self.assertLess(smoke, promote)
        self.assertIn('--env-file "$candidate_env"', script)
        self.assertIn("/etc/project-snow/images.env", script)
        self.assertIn("/srv/project-snow/runtime/compose.env", script)

    def test_maintenance_commands_use_last_promoted_environment(self) -> None:
        for relative in ("ops/backup.sh", "ops/restore-postgres.sh", "ops/rollback.sh"):
            self.assertIn("--env-file", self.read(relative), relative)
        cleanup = self.read("ops/project-snow-cleanup.service")
        self.assertIn("--env-file /srv/project-snow/runtime/compose.env", cleanup)

    def test_prepare_script_installs_host_tuning_and_deploy_key(self) -> None:
        script = self.read("ops/prepare_debian.sh")
        self.assertIn('cp "$script_dir/sysctl-project-snow.conf"', script)
        self.assertIn("/srv/project-snow/repo/App", script)
        self.assertIn("must be a symlink", script)
        self.assertIn("/home/deploy/.ssh/authorized_keys", script)
        self.assertIn("PasswordAuthentication no", script)
        self.assertIn("ufw allow 43556/tcp", script)

    def test_local_deploy_selects_a_verified_main_commit(self) -> None:
        script = self.read("scripts/deploy.ps1")
        self.assertIn("/srv/project-snow/repo", script)
        self.assertIn("git fetch --quiet origin main", script)
        self.assertIn("git merge-base --is-ancestor", script)
        self.assertIn("git checkout --quiet --detach", script)
        self.assertIn("git rev-parse HEAD | grep -Fx", script)
        self.assertIn("&& cd App &&", script)

    def test_production_examples_separate_public_settings_from_secrets(self) -> None:
        public_env = self.read("ops/public.env.example")
        images_env = self.read("ops/images.env.example")
        self.assertIn("PUBLIC_ALLOW_INSECURE_DEV=false", public_env)
        self.assertIn("PUBLIC_AUTO_CREATE_SCHEMA=false", public_env)
        self.assertNotIn("PUBLIC_DATABASE_URL=", public_env)
        self.assertNotIn("TURNSTILE_SECRET=", public_env)
        for variable in (
            "CADDY_IMAGE",
            "CLOUDFLARED_IMAGE",
            "POSTGRES_IMAGE",
            "QDRANT_IMAGE",
            "NEO4J_IMAGE",
            "EGRESS_PROXY_IMAGE",
        ):
            self.assertIn(f"{variable}=", images_env)
