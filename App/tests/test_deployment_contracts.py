from __future__ import annotations

import subprocess
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
        entrypoint = self.read("infra/neo4j-entrypoint.sh")
        self.assertIn("/etc/project-snow/secrets/neo4j_auth:/run/host-secrets/neo4j_auth:ro", compose)
        self.assertIn('entrypoint: ["/bin/bash", "/opt/project-snow/neo4j-entrypoint.sh"]', compose)
        self.assertIn('export NEO4J_AUTH_FILE="$runtime_dir/neo4j_auth"', entrypoint)
        self.assertIn('install -o neo4j -g neo4j -m 0700 -d "$runtime_dir"', entrypoint)
        self.assertIn(
            'install -o neo4j -g neo4j -m 0400 "$source_file" "$runtime_dir/neo4j_auth"',
            entrypoint,
        )
        self.assertNotIn("NEO4J_AUTH:?", compose)

    def test_database_and_egress_services_can_bind_inside_the_compose_network(self) -> None:
        postgres = self.read("infra/postgres/postgresql.conf")
        compose = self.read("compose.prod.yml")
        squid = self.read("infra/egress-squid.conf")
        self.assertIn("listen_addresses = '*'", postgres)
        self.assertIn("command: [\"neo4j\"]", compose)
        self.assertIn("uid=13,gid=13,mode=0750", compose)
        self.assertIn("cache_log /var/log/squid/cache.log", squid)
        self.assertIn("access_log stdio:/var/log/squid/access.log", squid)
        self.assertIn("cache_store_log none", squid)
        self.assertIn("pinger_enable off", squid)

    def test_admin_is_published_only_on_loopback_through_a_host_reachable_network(self) -> None:
        compose = self.read("compose.prod.yml")
        admin_start = compose.index("  admin:\n")
        admin_end = compose.index("\n  caddy:\n", admin_start)
        admin = compose[admin_start:admin_end]
        networks_start = compose.index("\nnetworks:\n")
        networks = compose[networks_start:]
        self.assertIn('"127.0.0.1:19090:19090"', admin)
        self.assertIn("networks: [data, management]", admin)
        self.assertIn("  management:\n", networks)
        self.assertNotIn("  management:\n    internal: true", networks)

    def test_public_api_bootstraps_root_only_secrets_then_drops_privileges(self) -> None:
        compose = self.read("compose.prod.yml")
        dockerfile = self.read("infra/public-api.Dockerfile")
        entrypoint = self.read("infra/public-entrypoint.sh")
        self.assertIn("/etc/project-snow/secrets:/run/host-secrets:ro", compose)
        self.assertIn("/run/project-snow-secrets:rw,noexec,nosuid,size=1m,mode=0700", compose)
        self.assertIn("PUBLIC_DATABASE_URL_FILE: /run/project-snow-secrets/public_database_url", compose)
        self.assertIn("apt-get install -y --no-install-recommends gosu", dockerfile)
        self.assertIn('ENTRYPOINT ["/app/infra/public-entrypoint.sh"]', dockerfile)
        self.assertIn('install -o snow -g snow -m 0400', entrypoint)
        self.assertIn('exec gosu snow "$@"', entrypoint)
        self.assertNotIn("/etc/project-snow/secrets:/run/secrets:ro", compose)

    def test_deploy_promotes_compose_environment_only_after_smoke(self) -> None:
        script = self.read("ops/deploy.sh")
        verify_data = script.index("backend.snow_app.data_loader --verify-only")
        load_data = script.index("python -m backend.snow_app.data_loader", verify_data + 1)
        start_api = script.index('compose up -d "public-api-$colour" caddy cloudflared')
        smoke = script.index("/app/public_smoke.py")
        promote = script.index('mv -f "$candidate_env" "$current_env"')
        self.assertLess(verify_data, load_data)
        self.assertLess(load_data, start_api)
        self.assertLess(smoke, promote)
        self.assertIn('--env-file "$candidate_env"', script)
        self.assertIn("/etc/project-snow/images.env", script)
        self.assertIn("/srv/project-snow/runtime/compose.env", script)
        self.assertIn("Public API did not become ready within 60 seconds.", script)

    def test_maintenance_commands_use_last_promoted_environment(self) -> None:
        for relative in ("ops/backup.sh", "ops/restore-postgres.sh", "ops/rollback.sh"):
            self.assertIn("--env-file", self.read(relative), relative)
        cleanup = self.read("ops/project-snow-cleanup.service")
        self.assertIn("--env-file /srv/project-snow/runtime/compose.env", cleanup)

    def test_prepare_script_installs_host_tuning_and_deploy_key(self) -> None:
        script = self.read("ops/prepare_debian.sh")
        daemon = self.read("ops/docker-daemon.json")
        self.assertIn('cp "$script_dir/sysctl-project-snow.conf"', script)
        self.assertIn("/srv/project-snow/repo/App", script)
        self.assertIn("must be a symlink", script)
        self.assertIn("/home/deploy/.ssh/authorized_keys", script)
        self.assertIn("PasswordAuthentication no", script)
        self.assertIn("ufw allow 43556/tcp", script)
        self.assertIn("-m 0755 -d /srv/project-snow/data", script)
        self.assertIn("-g deploy -m 0750 -d /etc/project-snow", script)
        self.assertIn('"userland-proxy": true', daemon)

    def test_local_deploy_selects_a_verified_main_commit(self) -> None:
        script = self.read("scripts/deploy.ps1")
        self.assertIn("/srv/project-snow/repo", script)
        self.assertIn("git fetch --quiet origin main", script)
        self.assertIn("git merge-base --is-ancestor", script)
        self.assertIn("git checkout --quiet --detach", script)
        self.assertIn("git rev-parse HEAD | grep -Fx", script)
        self.assertIn("&& cd App &&", script)

    def test_directly_invoked_operations_are_executable_in_git(self) -> None:
        repo_root = self.app_root.parent
        paths = (
            "App/ops/backup.sh",
            "App/ops/deploy.sh",
            "App/ops/prepare_debian.sh",
            "App/ops/restore-postgres.sh",
            "App/ops/rollback.sh",
            "App/ops/promote-data.sh",
            "App/infra/public-entrypoint.sh",
            "App/infra/neo4j-entrypoint.sh",
        )
        result = subprocess.run(
            ["git", "ls-files", "-s", "--", *paths],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        indexed_modes = {
            fields[3].replace("\\", "/"): fields[0]
            for line in result.stdout.splitlines()
            if line.strip() and len(fields := line.split(maxsplit=3)) == 4
        }
        self.assertEqual(indexed_modes, {path: "100755" for path in paths}, result.stdout)

    def test_production_runtime_uses_promoted_data_release(self) -> None:
        compose = self.read("compose.prod.yml")
        self.assertIn("APP_RUNTIME: /srv/project-snow/data/current", compose)
        self.assertNotIn("APP_RUNTIME: /srv/project-snow/runtime", compose)
        promote = self.read("ops/promote-data.sh")
        self.assertIn("verify_data_release.py", promote)
        self.assertIn('mv -Tf "$temporary_current" "$current_link"', promote)

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
