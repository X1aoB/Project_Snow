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
        self.assertIn("log_statement = 'none'", postgres)
        self.assertIn("log_min_duration_statement = -1", postgres)
        self.assertIn("log_min_error_statement = 'panic'", postgres)
        self.assertIn("log_parameter_max_length = 0", postgres)
        self.assertIn("log_parameter_max_length_on_error = 0", postgres)

    def test_admin_is_published_only_on_loopback_through_an_internal_network(self) -> None:
        compose = self.read("compose.prod.yml")
        admin_start = compose.index("  admin:\n")
        admin_end = compose.index("\n  caddy:\n", admin_start)
        admin = compose[admin_start:admin_end]
        networks_start = compose.index("\nnetworks:\n")
        networks = compose[networks_start:]
        self.assertIn('"127.0.0.1:19090:19090"', admin)
        self.assertIn("networks: [data, management]", admin)
        self.assertIn('"--no-access-log"', admin)
        self.assertIn("  management:\n", networks)
        self.assertIn("  management:\n    internal: true", networks)

    def test_public_api_bootstraps_root_only_secrets_then_drops_privileges(self) -> None:
        compose = self.read("compose.prod.yml")
        dockerfile = self.read("infra/public-api.Dockerfile")
        entrypoint = self.read("infra/public-entrypoint.sh")
        self.assertNotIn("/etc/project-snow/secrets:/run/host-secrets:ro", compose)
        self.assertIn(
            "/etc/project-snow/secrets/public_database_url:/run/host-secrets/public_database_url:ro",
            compose,
        )
        self.assertIn("/run/project-snow-secrets:rw,noexec,nosuid,size=1m,mode=0700", compose)
        self.assertIn("PUBLIC_DATABASE_URL_FILE: /run/project-snow-secrets/public_database_url", compose)
        self.assertIn("apt-get upgrade -y --no-install-recommends", dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends gosu", dockerfile)
        self.assertIn('ENTRYPOINT ["/app/infra/public-entrypoint.sh"]', dockerfile)
        self.assertIn('"--no-access-log"', dockerfile)
        self.assertIn('install -o snow -g snow -m 0400', entrypoint)
        self.assertIn('exec gosu snow "$@"', entrypoint)
        self.assertNotIn("/etc/project-snow/secrets:/run/secrets:ro", compose)

    def test_public_networks_and_secrets_are_least_privilege(self) -> None:
        compose = self.read("compose.prod.yml")
        api = compose[compose.index("x-api: &api"):compose.index("\nservices:")]
        admin = compose[compose.index("  admin:\n"):compose.index("\n  feedback-mailer:")]
        mailer = compose[compose.index("  feedback-mailer:\n"):compose.index("\n  caddy:")]
        egress = compose[compose.index("  egress-proxy:\n"):compose.index("\nvolumes:")]
        self.assertIn("networks: [app, data, egress-client]", api)
        self.assertNotIn("outbound", api)
        self.assertNotIn("public_admin_token", api)
        self.assertNotIn("feedback_smtp_password", api)
        self.assertIn(
            "networks: [egress-client, egress-uplink, outbound]", egress
        )
        self.assertIn("x-legacy-outbound-retirement", compose)
        self.assertIn("both-colour-manifests-at-least-0.9", compose)
        self.assertIn("networks: [edge-client, tunnel-uplink]", compose)
        self.assertIn("  egress-client:\n    internal: true", compose)
        self.assertIn("  edge-client:\n    internal: true", compose)
        self.assertIn("public_admin_token", admin)
        self.assertNotIn("turnstile_secret", admin)
        self.assertNotIn("public_credential_key", admin)
        self.assertIn("feedback_smtp_password", mailer)
        self.assertNotIn("public_qq_key", mailer)
        self.assertNotIn("public_database_url", mailer)
        self.assertNotIn("PUBLIC_ENV_FILE", mailer)
        self.assertIn("PUBLIC_MAILER_ENV_FILE", mailer)
        self.assertIn("feedback_mailer_database_password", mailer)
        self.assertIn("PUBLIC_DATABASE_USER: project_snow_feedback_mailer", mailer)
        self.assertIn(
            "/etc/project-snow/secrets/feedback_mailer_database_password:"
            "/run/secrets/feedback_mailer_database_password:ro",
            compose,
        )
        entrypoint = self.read("infra/public-entrypoint.sh")
        self.assertIn("feedback_mailer_database_password", entrypoint)
        prepare = self.read("ops/prepare_debian.sh")
        self.assertIn("/etc/project-snow/feedback-mailer.env", prepare)
        self.assertIn("chmod 0600 /etc/project-snow/feedback-mailer.env", prepare)
        self.assertIn("openssl rand -base64 48", prepare)
        self.assertIn("feedback_mailer_database_password", prepare)
        self.assertIn("chmod 0400 /etc/project-snow/secrets/feedback_smtp_password", prepare)

    def test_feedback_mailer_role_cannot_read_feedback_content(self) -> None:
        migration = self.read(
            "migrations/versions/20260819_0004_feedback_mailer_role.py"
        )
        self.assertIn("CREATE ROLE project_snow_feedback_mailer NOLOGIN", migration)
        self.assertIn(
            "feedback_id, public_code, created_at, expires_at", migration
        )
        self.assertIn(
            "outbox_id, feedback_id, status, attempt_count, next_attempt_at, locked_until",
            migration,
        )
        self.assertNotIn("body_text", migration)
        self.assertNotIn("context_json", migration)
        self.assertNotIn("qq_cipher", migration)

    def test_public_edge_blocks_private_health_and_caps_request_bodies(self) -> None:
        caddyfile = self.read("infra/Caddyfile")
        self.assertIn("max_size 65536", caddyfile)
        self.assertIn("read_body 10s", caddyfile)
        self.assertIn("not host snow.xiaob.dev", caddyfile)
        self.assertIn("not header Origin https://snow.xiaob.dev", caddyfile)
        self.assertIn(
            r"not header_regexp Content-Type `(?i)^application/json([\t ]*;.*)?$`",
            caddyfile,
        )
        self.assertIn("/public/v1/health/ready /public/v1/health/full", caddyfile)
        self.assertIn("respond @private_health 404", caddyfile)
        self.assertIn("Strict-Transport-Security", caddyfile)
        self.assertIn("Cross-Origin-Opener-Policy same-origin", caddyfile)
        self.assertIn("style-src-elem 'self'", caddyfile)
        self.assertIn("style-src-attr 'unsafe-inline'", caddyfile)
        self.assertIn("@privacy_document path /privacy/ /privacy/index.html", caddyfile)
        self.assertIn("path_regexp unversioned_scene", caddyfile)

    def test_deploy_stages_only_the_inactive_colour(self) -> None:
        script = self.read("ops/deploy.sh")
        verify_data = script.index(
            'python -m backend.snow_app.data_loader --release-root "$candidate_data_root" --verify-only'
        )
        load_data = script.index(
            'python -m backend.snow_app.data_loader --release-root "$candidate_data_root"',
            verify_data + 1,
        )
        start_api = script.index('compose up -d "$service"')
        smoke = script.index("/app/public_smoke.py")
        stage_env = script.index('mv -f "$candidate_env" "$colour_env"')
        self.assertLess(verify_data, load_data)
        self.assertLess(load_data, start_api)
        self.assertLess(smoke, stage_env)
        self.assertIn('--env-file "$candidate_env"', script)
        self.assertIn("/etc/project-snow/images.env", script)
        self.assertIn("/srv/project-snow/runtime/compose.env", script)
        self.assertIn("active-colour", script)
        self.assertIn("Active-colour marker is required before staging", script)
        self.assertIn("Active-colour marker exists but is not readable", script)
        self.assertIn('case "$active_colour" in blue|green)', script)
        self.assertNotIn("tr -d '[:space:]' < \"$active_file\"", script)
        self.assertIn("Caddy and cloudflared keep serving", script)
        self.assertNotIn("force-recreate caddy", script)
        self.assertIn(
            'candidate_media_root="/srv/project-snow/media/releases/$candidate_media_version"',
            script,
        )
        self.assertIn("PUBLIC_MEDIA_ROOT=%s", script)
        self.assertIn("must be downloaded, verified and staged", script)
        self.assertIn(
            "public_smoke.py http://127.0.0.1:8000 --mode internal", script
        )
        self.assertIn(
            'candidate_data_root="/srv/project-snow/data/releases/$candidate_data_version"',
            script,
        )
        self.assertIn("PUBLIC_DATA_ROOT=%s", script)
        self.assertIn('--release-root "$candidate_data_root" --verify-only', script)
        self.assertNotIn("data_loader --activate", script)

    def test_compose_allows_a_colour_to_pin_verified_media(self) -> None:
        compose = self.read("compose.prod.yml")
        self.assertIn(
            "PUBLIC_MEDIA_ROOT: ${PUBLIC_MEDIA_ROOT:-/srv/project-snow/media/current}",
            compose,
        )

    def test_media_stage_only_does_not_switch_current(self) -> None:
        script = self.read("ops/fetch-promote-media.sh")
        stage = script.index('if [ "$mode" = "stage-only" ]; then')
        promote = script.index('ln -s "$target" "$media_root/current.next"')
        self.assertLess(stage, promote)
        self.assertIn('mode="${2:-promote}"', script)
        self.assertIn("promote|stage-only", script)

    def test_deploy_bootstraps_complete_active_release_for_rollback(self) -> None:
        script = self.read("ops/deploy.sh")
        bootstrap_env = script.index('cp "$current_env" "$bootstrap_colour_env"')
        bootstrap_marker = script.index('cp "$current_marker" "$bootstrap_colour_marker"')
        bootstrap_manifest = script.index(
            'cp "$current_manifest" "$bootstrap_colour_manifest"'
        )
        pull_candidate = script.index('compose pull "$service"')

        self.assertLess(bootstrap_env, pull_candidate)
        self.assertLess(bootstrap_marker, pull_candidate)
        self.assertLess(bootstrap_manifest, pull_candidate)
        self.assertIn('current_marker="/srv/project-snow/releases/current"', script)
        self.assertIn("Cannot preserve rollback environment", script)
        self.assertIn("Cannot preserve rollback marker", script)
        self.assertIn("Cannot preserve rollback manifest", script)
        self.assertIn("Bootstrap rollback marker colour mismatch.", script)
        self.assertIn('bootstrap_manifest_sha="$(jq -r', script)
        self.assertIn(
            '"$bootstrap_manifest_sha" != "$bootstrap_marker_sha"', script
        )
        self.assertIn("Bootstrap rollback manifest is invalid.", script)
        self.assertIn("Cannot pin rollback data release", script)
        self.assertIn("PUBLIC_DATA_ROOT=%s", script)
        self.assertIn('git -C "$repository_root" archive "$bootstrap_marker_sha"', script)
        self.assertIn("project-snow-config-snapshot-1", script)
        self.assertIn('validate_config_binding "$bootstrap_config_binding"', script)

    def test_promote_switches_only_after_candidate_smoke_and_can_restore(self) -> None:
        script = self.read("ops/promote.sh")
        candidate_smoke = script.index("python - http://127.0.0.1:8000 --mode internal")
        switch = script.index(
            'switch_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        post_switch_smoke = script.index("python - http://caddy:8080 --mode public")
        marker = script.index('mv -f "$state_tmp" "$current_env"')
        self.assertLess(candidate_smoke, switch)
        self.assertLess(switch, post_switch_smoke)
        self.assertLess(post_switch_smoke, marker)
        self.assertIn("restoring the previous edge configuration snapshot", script)
        self.assertIn("--force-recreate caddy cloudflared", script)
        self.assertIn("--force-recreate caddy cloudflared egress-proxy", script)
        self.assertIn(
            'switch_edge "$previous_env" "$previous_config_root" "$previous_colour"',
            script,
        )
        self.assertNotIn(
            "switch_edge \"$previous_env\" \"$previous_colour\" || true", script
        )
        self.assertIn("active-colour", script)
        self.assertIn("Active-colour marker is required before promotion", script)
        self.assertIn("Refusing to promote already-active colour", script)
        self.assertNotIn("tr -d '[:space:]' < \"$active_file\"", script)
        self.assertIn(
            'python - http://127.0.0.1:8000 --mode internal < "$smoke_script"',
            script,
        )
        self.assertIn(
            "python - http://caddy:8080 --mode public", script
        )
        self.assertIn("--allow-private-health", script)
        self.assertIn("PROJECT_SNOW_ROLLBACK_MODE", script)
        self.assertIn("/etc/project-snow/access-denied-status", script)
        self.assertIn("root-owned mode-0600 regular file", script)
        self.assertIn("verify_cloudflare_access_restored", script)
        self.assertIn("--header 'Cookie:'", script)
        self.assertIn("https://snow.xiaob.dev/cdn-cgi/access/", script)
        self.assertIn("cloudflareaccess.com/cdn-cgi/access/", script)
        self.assertIn("public site is not behind Access", script)
        self.assertIn("config --services", script)
        self.assertIn("target_has_mailer", script)
        self.assertIn("previous_has_mailer", script)
        self.assertIn("previous_compose stop feedback-mailer", script)
        self.assertIn("previous_compose rm -f feedback-mailer", script)
        self.assertIn("restore_previous_runtime", script)
        target_start = script.index('compose up -d --no-deps "$service"')
        access_gate = script.index("verify_cloudflare_access_restored || exit 70")
        ready_check = script.index("while [ \"$attempt\" -lt 15 ]")
        stop_previous = script.index('previous_compose stop "$previous_service"')
        self.assertLess(access_gate, target_start)
        self.assertLess(target_start, ready_check)
        self.assertLess(post_switch_smoke, stop_previous)
        self.assertLess(stop_previous, marker)
        restore = script.index(
            'previous_compose up -d --no-deps "$previous_service"'
        )
        restore_edge = script.index(
            'switch_edge "$previous_env" "$previous_config_root" "$previous_colour"',
            restore,
        )
        self.assertLess(restore, restore_edge)
        self.assertIn("previous public API did not become ready for restoration", script)

    def test_smoke_separates_internal_full_from_public_minimal(self) -> None:
        smoke = self.read("infra/public_smoke.py")
        self.assertIn('PUBLIC_HOST = "snow.xiaob.dev"', smoke)
        self.assertIn('headers = {"Host": host} if host else {}', smoke)
        self.assertIn('host = public_host if mode == "public" else None', smoke)
        self.assertIn("INTERNAL_ONLY_JSON_CHECKS if mode == \"internal\" else ()", smoke)
        self.assertIn('("/public/v1/health/ready", "status", "ok")', smoke)
        self.assertIn('("/public/v1/health/full", "status", "ok")', smoke)
        self.assertIn('if mode == "public"', smoke)
        self.assertIn('mode == "public" and not allow_private_health', smoke)
        self.assertIn("--allow-private-health", smoke)
        self.assertIn("public smoke exposed private health endpoint", smoke)
        self.assertNotIn("assert get(", smoke)

    def test_maintenance_commands_use_last_promoted_environment(self) -> None:
        for relative in ("ops/backup.sh", "ops/restore-postgres.sh"):
            self.assertIn("--env-file", self.read(relative), relative)
        self.assertIn("promote.sh", self.read("ops/rollback.sh"))
        cleanup = self.read("ops/project-snow-cleanup.service")
        self.assertIn("--env-file /srv/project-snow/runtime/compose.env", cleanup)

    def test_prepare_script_installs_host_tuning_and_deploy_key(self) -> None:
        script = self.read("ops/prepare_debian.sh")
        daemon = self.read("ops/docker-daemon.json")
        self.assertIn('cp "$script_dir/sysctl-project-snow.conf"', script)
        self.assertIn("/srv/project-snow/repo/App", script)
        self.assertIn("must be a symlink", script)
        self.assertIn(
            'deploy_authorized_keys="$deploy_ssh_directory/authorized_keys"', script
        )
        self.assertIn("PasswordAuthentication no", script)
        self.assertIn("PermitRootLogin no", script)
        self.assertIn("AllowUsers deploy", script)
        self.assertIn("systemctl enable --now fail2ban", script)
        self.assertIn("Refusing to disable root SSH", script)
        self.assertIn("ufw allow 43556/tcp", script)
        self.assertIn("-m 0755 -d /srv/project-snow/data", script)
        self.assertIn("/srv/project-snow/media/stickers/releases", script)
        self.assertIn("/srv/project-snow/media/stickers/staging", script)
        self.assertIn("install -o root -g root -m 0750 -d /etc/project-snow", script)
        self.assertIn("install -o deploy -g deploy -m 0700 -d /srv/project-snow/inbox", script)
        self.assertIn('"$script_dir/bootstrap-release-runner.sh"', script)
        self.assertIn("--controller-sha", script)
        self.assertIn("ufw status | grep -Fx 'Status: active'", script)
        self.assertNotIn("usermod -aG docker deploy", script)
        self.assertIn('"userland-proxy": true', daemon)

    def test_local_deploy_routes_exact_release_through_root_runner(self) -> None:
        script = self.read("scripts/deploy.ps1")
        self.assertNotIn("/srv/project-snow/repo", script)
        self.assertNotIn("git fetch", script)
        self.assertNotIn("git checkout", script)
        self.assertIn("project-snow-release-1", script)
        self.assertIn('/srv/project-snow/inbox/release-$Sha.json', script)
        self.assertIn("project-snow-release status", script)
        self.assertIn("project-snow-release stage '$Colour' '$Sha'", script)
        self.assertIn("deploy_has_docker_group", script)
        self.assertIn("deploy_can_access_docker", script)
        self.assertIn("ufw_active", script)
        self.assertIn("fail2ban_active", script)
        self.assertIn("sshd_hardened", script)
        self.assertIn("media_version", script)
        self.assertIn("Release manifest has no media version.", script)
        self.assertIn("sticker_version", script)
        self.assertIn("data_version", script)
        self.assertIn("installed_data_versions", script)
        self.assertIn("installed_avatar_versions", script)
        self.assertIn("installed_sticker_versions", script)
        self.assertIn("& tar -cf $archivePath", script)
        self.assertIn('/srv/project-snow/inbox/$($releaseSpec.Kind)-$($releaseSpec.Version).tar', script)
        self.assertIn("configuration_sha256", script)
        self.assertIn("release_artifacts", script)
        self.assertIn("manifest_sha256", script)
        self.assertIn("checksums_sha256", script)
        self.assertIn("does not match the trusted CI release binding", script)
        self.assertIn("'-F', $configPath", script)
        self.assertIn("project-snow-ssh-config", script)

    def test_deploy_account_has_only_a_root_owned_release_runner(self) -> None:
        runner = self.read("ops/project-snow-release")
        bootstrap = self.read("ops/bootstrap-release-runner.sh")
        sudoers = self.read("ops/project-snow-release.sudoers")

        self.assertIn("Allowed operations: stage, promote, rollback, status.", runner)
        self.assertIn('exec 9>"$release_lock"', runner)
        self.assertIn("flock -n 9", runner)
        self.assertIn("/usr/bin/env -i", runner)
        self.assertIn("https://github.com/X1aoB/Project_Snow.git", runner)
        self.assertIn("+refs/heads/main:refs/remotes/origin/main", runner)
        self.assertIn("merge-base --is-ancestor", runner)
        self.assertIn("core.hooksPath=/dev/null", runner)
        self.assertIn("release_manifest.py", runner)
        self.assertIn("docker buildx imagetools inspect", runner)
        self.assertIn("runuser -u deploy -- docker info", runner)
        self.assertIn("ufw status", runner)
        self.assertIn("systemctl is-active --quiet fail2ban", runner)
        self.assertIn("fail2ban-client status sshd", runner)
        self.assertIn("sshd -T -C user=deploy", runner)
        self.assertIn("sshd_hardened", runner)
        self.assertIn("PROJECT_SNOW_ROLLBACK_MODE=1", runner)
        self.assertEqual(runner.count("PROJECT_SNOW_ROLLBACK_MODE=1"), 1)
        self.assertNotIn("tr -d '[:space:]' < \"$active_file\"", runner)
        self.assertNotIn("eval ", runner)

        self.assertIn("clone --quiet --no-checkout", bootstrap)
        self.assertIn("--controller-sha", bootstrap)
        self.assertIn("chown -R root:root \"$repo\"", bootstrap)
        self.assertIn("gpasswd -d deploy docker", bootstrap)
        self.assertIn("runuser -u deploy -- docker info", bootstrap)
        self.assertIn("feedback_mailer_database_password", bootstrap)
        self.assertIn("feedback_smtp_password", bootstrap)
        self.assertIn("project-snow-release status", bootstrap)
        self.assertIn("ca-certificates curl git jq openssl python3 sudo util-linux", bootstrap)

        self.assertIn("deploy ALL=(root) NOPASSWD: /usr/local/sbin/project-snow-release", sudoers)
        sudoers_rules = "\n".join(
            line for line in sudoers.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("/bin/sh", sudoers_rules)
        self.assertNotIn("docker", sudoers_rules.casefold())

    def test_release_archives_are_safely_verified_before_atomic_install(self) -> None:
        runner = self.read("ops/project-snow-release")
        installer = self.read("scripts/install_release_archive.py")
        for kind in ("data", "avatar", "sticker"):
            self.assertIn(f'    {kind} "${kind}_version"', runner)
        self.assertIn("install_release_archive.py", runner)
        self.assertIn("/srv/project-snow/inbox", runner)
        self.assertIn("release_artifacts", runner)
        self.assertIn("--manifest-sha256", runner)
        self.assertIn("--checksums-sha256", runner)
        self.assertIn("os.O_NOFOLLOW", installer)
        self.assertIn("metadata.st_uid != expected_owner_uid", installer)
        self.assertIn("metadata.st_nlink != 1", installer)
        self.assertIn("MAX_ARCHIVE_BYTES", installer)
        self.assertIn("MAX_EXPANDED_BYTES", installer)
        self.assertIn("MAX_MEMBERS", installer)
        self.assertIn("member.isdir() or member.isreg()", installer)
        self.assertNotIn("extractall", installer)
        self.assertIn("PublicMediaCatalog", installer)
        self.assertIn("PublicStickerCatalog", installer)
        self.assertIn("verify_data_release", installer)
        self.assertIn("verify_trusted_binding", installer)
        self.assertIn("trusted Git/CI binding", installer)
        self.assertIn("--manifest-sha256", installer)
        self.assertIn("--checksums-sha256", installer)
        self.assertIn("staging.rename(target)", installer)

    def test_legacy_bootstrap_schedules_exact_main_host_prepare_outside_docker(self) -> None:
        script = self.read("scripts/bootstrap-release-runner.ps1")
        host_bootstrap = self.read("scripts/bootstrap_release_host.py")
        self.assertIn("https://github.com/X1aoB/Project_Snow.git", script)
        self.assertIn("refs/remotes/origin/main", script)
        self.assertIn("show \"$Sha`:App/scripts/bootstrap_release_host.py\"", script)
        self.assertIn("git @archiveArguments", script)
        self.assertIn("$hostBootstrapSource | & ssh", script)
        self.assertIn("--pid host", script)
        self.assertIn("docker run --rm -i", script)
        self.assertIn("--entrypoint python", script)
        self.assertIn("prepare-$Sha.status", script)
        self.assertIn("deploy_can_access_docker", script)
        self.assertIn("ufw_active", script)
        self.assertIn("fail2ban_active", script)
        self.assertIn("sshd_hardened", script)
        self.assertNotIn("cd /srv/project-snow/repo", script)
        self.assertIn("os.O_NOFOLLOW", host_bootstrap)
        self.assertIn("EXPECTED_FILES", host_bootstrap)
        self.assertIn("host preparation bundle does not match the exact Git archive", host_bootstrap)
        self.assertIn("project-snow-prepare-", host_bootstrap)
        self.assertIn('"--no-block", "start"', host_bootstrap)

    def test_deploy_verifies_release_configuration_hashes_before_staging(self) -> None:
        local_script = self.read("scripts/deploy.ps1")
        remote_script = self.read("ops/deploy.sh")
        for relative_path in (
            "compose.prod.yml",
            "infra/Caddyfile",
            "infra/egress-squid.conf",
            "infra/neo4j-entrypoint.sh",
            "infra/postgres/postgresql.conf",
            "infra/public-api.Dockerfile",
            "requirements-public.txt",
        ):
            self.assertIn(relative_path, local_script)
            self.assertIn(relative_path, remote_script)
        self.assertIn("^[0-9a-f]{64}$", local_script)
        self.assertIn(".configuration_sha256[$path]", remote_script)
        self.assertIn('sha256sum "$configuration_path"', remote_script)
        self.assertIn("Release configuration hash mismatch", remote_script)

    def test_deploy_requires_manifest_identity_to_match_every_release_input(self) -> None:
        script = self.read("ops/deploy.sh")
        identity_check = script.index('manifest_commit_sha="$(jq -r')
        first_staging_write = script.index('install -d -m 0700 "$colour_env_root"')
        self.assertLess(identity_check, first_staging_write)
        for manifest_field in (
            ".commit_sha",
            ".application.image",
            ".application.digest",
            ".embedding.image",
            ".embedding.digest",
        ):
            self.assertIn(manifest_field, script)
        self.assertIn("A readable verified release manifest is required.", script)
        self.assertIn(
            '"$PUBLIC_API_IMAGE" = "$manifest_application_image@$manifest_application_digest"',
            script,
        )
        self.assertIn(
            '"$EMBEDDING_IMAGE" = "$manifest_embedding_image@$manifest_embedding_digest"',
            script,
        )
        self.assertIn(
            "Release manifest commit SHA does not match the requested SHA.", script
        )

    def test_staged_colour_pins_manifest_versions_in_a_private_env(self) -> None:
        script = self.read("ops/deploy.sh")
        self.assertIn(
            'public_env_source="${PROJECT_SNOW_PUBLIC_ENV:-/etc/project-snow/public.env}"',
            script,
        )
        self.assertIn('public_env_path="$public_env_root/public-$sha.env"', script)
        self.assertIn("PUBLIC_APP_VERSION=%s", script)
        self.assertIn("PUBLIC_DATA_VERSION=%s", script)
        self.assertIn("PUBLIC_DATA_ROOT=%s", script)
        self.assertIn("PUBLIC_MEDIA_VERSION=%s", script)
        self.assertIn("PUBLIC_STICKER_VERSION=%s", script)
        self.assertIn('candidate_sticker_root="/srv/project-snow/media/stickers/releases/', script)
        self.assertIn("PUBLIC_STICKER_ROOT=%s", script)
        self.assertNotIn("active_sticker_version", script)
        self.assertIn("PUBLIC_ENV_FILE=$public_env_path", script)
        self.assertIn('mv -f "$candidate_public_env" "$public_env_path"', script)

    def test_candidate_public_env_migrates_legacy_settings_fail_closed(self) -> None:
        script = self.read("ops/deploy.sh")
        self.assertIn("build_candidate_public_env", script)
        self.assertIn(
            "Public environment must be root-owned, mode 0600 and have one link.",
            script,
        )
        self.assertIn("Public environment contains a duplicate key.", script)
        self.assertIn("Public environment contains a disallowed key.", script)
        self.assertIn("PUBLIC_EXPERIENCE_NOTICE_VERSION=0.9", script)
        self.assertIn("PUBLIC_PRIVACY_POLICY_VERSION=0.9", script)
        self.assertIn("PUBLIC_PRIVACY_EFFECTIVE_AT=2026-08-19", script)
        self.assertIn("PUBLIC_TURNSTILE_HOSTNAME=snow.xiaob.dev", script)
        self.assertIn("PUBLIC_TURNSTILE_MAX_AGE_SECONDS=300", script)
        self.assertIn("PUBLIC_MAX_PROVIDER_CALLS_PER_ACTION=2", script)
        self.assertIn("PUBLIC_STATE_KEY_ID=%s", script)
        self.assertIn("PUBLIC_STATE_PREVIOUS_KEY_ID=%s", script)
        self.assertIn('public_enabled_providers="$public_value"', script)
        self.assertIn('public_turnstile_site_key="$public_value"', script)
        self.assertNotIn('cp "$public_env_source" "$candidate_public_env"', script)
        self.assertLess(
            script.index('build_candidate_public_env "$public_env_source"'),
            script.index('compose up -d "$service"'),
        )

    def test_sticker_promotion_requires_public_license_review_and_metadata(self) -> None:
        script = self.read("ops/fetch-promote-sticker-media.sh")
        for required in (
            'private_candidate == false',
            'license_review_status == "verified_public_release"',
            'source_page_url',
            'source_image_url',
            'license_status == "verified"',
            'content_hash == .sha256',
        ):
            self.assertIn(required, script)
        self.assertIn('mode="${2:-promote}"', script)
        self.assertIn('if [ "$mode" = "stage-only" ]', script)
        self.assertIn("without changing the current symlink", script)

    def test_promotion_and_rollback_use_the_workspace_ssh_config(self) -> None:
        for relative in ("scripts/promote.ps1", "scripts/rollback.ps1"):
            script = self.read(relative)
            self.assertIn("'-F', $configPath", script, relative)
            self.assertIn("project-snow-ssh-config", script, relative)
            self.assertIn("sudo -n /usr/local/sbin/project-snow-release", script, relative)
            self.assertIn("40-character main SHA", script, relative)

    def test_deploy_reuses_embedding_but_always_verifies_versioned_data(self) -> None:
        script = self.read("ops/deploy.sh")
        self.assertIn("Embedding digest unchanged; skipping image pull.", script)
        self.assertNotIn("skipping Qdrant and Neo4j load.", script)
        self.assertIn("Default loading is stage-only", script)
        self.assertIn('--release-root "$candidate_data_root"', script)
        self.assertNotIn("--activate\n", script)

    def test_embedding_image_is_offline_and_compose_waits_for_real_readiness(self) -> None:
        compose = self.read("compose.prod.yml")
        dockerfile = self.read("infra/embedding.Dockerfile")
        service = self.read("infra/embedding_service.py")
        workflow = (self.app_root.parent / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("EMBEDDING_MODEL_REVISION=7999e1d3359715c523056ef9478215996d62a620", dockerfile)
        self.assertIn("model.save_pretrained('/models/bge-small-zh-v1.5')", dockerfile)
        self.assertIn("COPY --from=builder --chown=10001:10001 /models /models", dockerfile)
        self.assertIn("HF_HUB_OFFLINE=1", dockerfile)
        self.assertIn("TRANSFORMERS_OFFLINE=1", dockerfile)
        self.assertNotIn('volumes: ["embedding_models:/models"]', compose)
        self.assertIn("embedding:\n      condition: service_healthy", compose)
        self.assertIn("payload.get('dimension') == 512", compose)
        self.assertIn("model()\n    yield", service)
        self.assertIn('status_code=503', service)
        self.assertIn("len(vectors[0]) == 512", workflow)

    def test_ci_uses_risk_tiers_and_a_stable_summary_gate(self) -> None:
        workflow = (self.app_root.parent / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        release = (
            self.app_root.parent / ".github" / "workflows" / "publish-images.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("classify_changes.py", workflow)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", workflow)
        self.assertIn("gate:\n", workflow)
        self.assertIn("All selected risk-tier jobs passed", workflow)
        self.assertIn("github.head_ref == 'codex/ci-risk-tiering'", workflow)
        self.assertIn("Reuse previous verified embedding digest", release)
        self.assertIn("fetch-depth: 0", release)
        self.assertIn('git rev-list --first-parent "$PREVIOUS_SHA"', release)
        self.assertIn("Tag reused embedding digest for the current main SHA", release)
        self.assertIn("release_manifest.py", release)

    def test_directly_invoked_operations_are_executable_in_git(self) -> None:
        repo_root = self.app_root.parent
        paths = (
            "App/ops/backup.sh",
            "App/ops/deploy.sh",
            "App/ops/promote.sh",
            "App/ops/prepare_debian.sh",
            "App/ops/restore-postgres.sh",
            "App/ops/rollback.sh",
            "App/ops/promote-data.sh",
            "App/ops/project-snow-release",
            "App/ops/bootstrap-release-runner.sh",
            "App/infra/public-entrypoint.sh",
            "App/infra/neo4j-entrypoint.sh",
            "App/ops/fetch-promote-media.sh",
            "App/ops/fetch-promote-sticker-media.sh",
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

    def test_production_runtime_pins_each_colour_to_an_immutable_data_release(self) -> None:
        compose = self.read("compose.prod.yml")
        repository = self.read("backend/snow_app/public_repository.py")
        loader = self.read("backend/snow_app/data_loader.py")
        self.assertIn("PUBLIC_DATA_ROOT: ${PUBLIC_DATA_ROOT:?", compose)
        self.assertIn("DATA_ROOT: ${PUBLIC_DATA_ROOT:?", compose)
        self.assertIn("APP_RUNTIME: ${PUBLIC_DATA_ROOT:?", compose)
        self.assertNotIn("APP_RUNTIME: /srv/project-snow/data/current", compose)
        self.assertIn("verify_data_release", repository)
        self.assertIn("self.qdrant_collection = versioned_collection_name", repository)
        self.assertIn("MATCH (start:SnowEntity {dataset_version: $data_version})", repository)
        self.assertNotIn("MATCH (pointer:SnowDatasetPointer {name: 'active'})", repository)
        self.assertIn("activate: bool = False", loader)
        self.assertIn('parser.add_argument(\n        "--activate"', loader)
        promote = self.read("ops/promote-data.sh")
        self.assertIn("verify_data_release.py", promote)
        self.assertIn("''|--activate", promote)
        self.assertIn("--release-root \"$resolved_release\" --activate", promote)
        self.assertNotIn("tr -d '[:space:]' < \"$active_file\"", promote)
        self.assertIn('mv -Tf "$temporary_current" "$current_link"', promote)

    def test_deploy_reloads_and_verifies_no_body_postgres_logging(self) -> None:
        script = self.read("ops/deploy.sh")
        self.assertIn("pg_ctl reload", script)
        for setting in (
            "log_statement",
            "log_min_duration_statement",
            "log_min_error_statement",
            "log_parameter_max_length",
            "log_parameter_max_length_on_error",
        ):
            self.assertIn(setting, script)
        self.assertIn("none|-1|panic|0|0", script)
        self.assertIn("PostgreSQL logging policy is unsafe", script)

    def test_colour_runtime_configuration_is_hashed_snapshotted_and_fail_closed(self) -> None:
        deploy = self.read("ops/deploy.sh")
        promote = self.read("ops/promote.sh")
        for script in (deploy, promote):
            self.assertIn("project-snow-config-snapshot-1", script)
            self.assertIn("/srv/project-snow/releases/configurations", script)
            self.assertIn("configuration_sha256", script)
            for relative_path in (
                "compose.prod.yml",
                "infra/Caddyfile",
                "infra/egress-squid.conf",
                "infra/neo4j-entrypoint.sh",
                "infra/postgres/postgresql.conf",
            ):
                self.assertIn(relative_path, script)
        self.assertIn('-f "$candidate_config_root/compose.prod.yml"', deploy)
        self.assertIn(
            'verify_snapshot_against_manifest "$candidate_config_root" "$release_manifest"',
            deploy,
        )
        self.assertIn(
            'mv -f "$candidate_config_binding_tmp" "$colour_config_binding"', deploy
        )
        self.assertIn('-f "$colour_config_root/compose.prod.yml"', promote)
        self.assertIn(
            "The active colour $previous_colour has no complete rollback snapshot",
            promote,
        )
        self.assertIn('mv -f "$config_tmp" "$current_config_binding"', promote)

    def test_feedback_mailer_secrets_are_preflighted_and_role_password_stays_off_argv(self) -> None:
        deploy = self.read("ops/deploy.sh")
        promote = self.read("ops/promote.sh")
        for script in (deploy, promote):
            self.assertIn("/etc/project-snow/feedback-mailer.env", script)
            self.assertIn(
                "Feedback mailer environment must be owned by root with mode 0600",
                script,
            )
            for key in (
                "PUBLIC_FEEDBACK_EMAIL_TO",
                "PUBLIC_FEEDBACK_EMAIL_FROM",
                "PUBLIC_FEEDBACK_SMTP_HOST",
                "PUBLIC_FEEDBACK_SMTP_PORT",
                "PUBLIC_FEEDBACK_SMTP_USERNAME",
            ):
                self.assertIn(key, script)
            self.assertIn(
                "Feedback SMTP sender, host and username must be all configured or all empty.",
                script,
            )
        logging_gate = deploy.index("PostgreSQL logging policy is unsafe")
        role_rotation = deploy.index("PROJECT_SNOW_MAILER_ROLE")
        data_load = deploy.index("# Default loading is stage-only")
        self.assertLess(logging_gate, role_rotation)
        self.assertLess(role_rotation, data_load)
        self.assertIn("/run/secrets/feedback_mailer_database_password", deploy)
        self.assertIn(
            "/etc/project-snow/secrets/feedback_mailer_database_password", deploy
        )
        self.assertIn("root-owned mode-0600 regular file", deploy)
        self.assertIn("project_snow_feedback_mailer", deploy)
        self.assertIn("convert_from(decode(", deploy)
        self.assertIn("\\gexec", deploy)
        self.assertIn("role_can_login", deploy)
        self.assertNotIn("--set=mailer_password", deploy)

    def test_promoted_media_is_readable_by_the_unprivileged_api(self) -> None:
        script = self.read("ops/fetch-promote-media.sh")
        verify = script.index("sha256sum -c SHA256SUMS")
        directories = script.index('find "$staging" -type d -exec chmod 0755')
        files = script.index('find "$staging" -type f -exec chmod 0644')
        promote = script.index('mv -- "$staging" "$target"')
        self.assertLess(verify, directories)
        self.assertLess(directories, files)
        self.assertLess(files, promote)

    def test_avatar_publication_fails_closed_on_provenance_or_hash_gaps(self) -> None:
        script = self.read("ops/publish-media.ps1")
        for required in (
            "project-snow-avatar-media-3",
            "verified_public_release",
            "exactly 22 character avatars",
            "source_revision_id",
            "source_uploader",
            "original_sha256",
            "transformations",
            "Get-FileHash",
            "SHA256SUMS does not cover every packaged avatar file exactly once",
        ):
            self.assertIn(required, script)

    def test_production_examples_separate_public_settings_from_secrets(self) -> None:
        public_env = self.read("ops/public.env.example")
        images_env = self.read("ops/images.env.example")
        mailer_env = self.read("ops/feedback-mailer.env.example")
        self.assertIn("PUBLIC_ALLOW_INSECURE_DEV=false", public_env)
        self.assertIn("PUBLIC_AUTO_CREATE_SCHEMA=false", public_env)
        self.assertNotIn("PUBLIC_DATABASE_URL=", public_env)
        self.assertNotIn("TURNSTILE_SECRET=", public_env)
        self.assertNotIn("PUBLIC_FEEDBACK_SMTP_", public_env)
        self.assertNotIn("PUBLIC_FEEDBACK_EMAIL_", public_env)
        self.assertIn("PUBLIC_ENABLED_PROVIDERS=openai,deepseek,dashscope,zhipu,moonshot", public_env)
        self.assertEqual(
            {
                line.split("=", 1)[0]
                for line in mailer_env.splitlines()
                if line and not line.startswith("#")
            },
            {
                "PUBLIC_FEEDBACK_EMAIL_TO",
                "PUBLIC_FEEDBACK_EMAIL_FROM",
                "PUBLIC_FEEDBACK_SMTP_HOST",
                "PUBLIC_FEEDBACK_SMTP_PORT",
                "PUBLIC_FEEDBACK_SMTP_USERNAME",
            },
        )
        for variable in (
            "CADDY_IMAGE",
            "CLOUDFLARED_IMAGE",
            "POSTGRES_IMAGE",
            "QDRANT_IMAGE",
            "NEO4J_IMAGE",
            "EGRESS_PROXY_IMAGE",
        ):
            self.assertIn(f"{variable}=", images_env)

    def test_public_frontend_has_immersive_byok_boundaries(self) -> None:
        css = self.read("public_frontend/app.css")
        javascript = self.read("public_frontend/app.js")
        html = self.read("public_frontend/index.html")
        self.assertIn("[hidden] { display:none !important; }", css)
        self.assertIn('invalid_request: "请求内容不完整', javascript)
        self.assertIn('provider_not_enabled: "该模型厂商尚未启用', javascript)
        self.assertIn("/^[a-z][a-z0-9_]*$/.test(code) ? errorMessages.request_failed : code", javascript)
        self.assertIn("async function waitForTurnstile()", javascript)
        self.assertIn('throw new Error("turnstile_unavailable")', javascript)
        self.assertIn("function saveCredential()", javascript)
        self.assertIn("const dbVersion = 4", javascript)
        self.assertIn('api("/presence/resolve"', javascript)
        self.assertIn('api("/presence/transition"', javascript)
        self.assertIn('api("/presence/arrival"', javascript)
        self.assertIn("content_blocks", javascript)
        self.assertIn("面对面场景", html)
        self.assertNotIn("/api/v1", javascript)
        self.assertNotIn("/workspace/", html)
        self.assertNotIn("attachment-input", html)
        self.assertNotIn("record-audio", html)
        self.assertNotIn("agent-mode", html)

    def test_public_frontend_build_fingerprints_immutable_assets(self) -> None:
        caddyfile = self.read("infra/Caddyfile")
        dockerfile = self.read("infra/public-api.Dockerfile")
        public_html = self.read("public_frontend/index.html")
        privacy_html = self.read("public_frontend/privacy/index.html")
        public_env = self.read("ops/public.env.example")
        self.assertIn("@frontend_assets path / /index.html /app.js /app.css", caddyfile)
        self.assertIn('header @frontend_assets Cache-Control "no-store, max-age=0"', caddyfile)
        self.assertIn("@content_hashed path_regexp immutable", caddyfile)
        self.assertIn('header @content_hashed Cache-Control "public, max-age=31536000, immutable"', caddyfile)
        self.assertIn("COPY scripts/fingerprint_public_frontend.py", dockerfile)
        self.assertIn("python ./scripts/fingerprint_public_frontend.py --app-root /app", dockerfile)
        self.assertIn("pip install --no-cache-dir --require-hashes", dockerfile)
        self.assertIn('href="/app.css?v=0.9.0"', public_html)
        self.assertIn('src="/app.js?v=0.9.0"', public_html)
        self.assertIn('href="/shared/immersive.css?v=0.9.0"', public_html)
        self.assertIn('src="/privacy/privacy.js?v=0.9.0"', privacy_html)
        self.assertIn("PUBLIC_APP_VERSION=0.9.0", public_env)
        self.assertIn("PUBLIC_MEDIA_VERSION=2026.08.19.avatar.1", public_env)
        self.assertIn("PUBLIC_STICKER_VERSION=2026.08.19.sticker.1", public_env)
        self.assertIn("@versioned_media path /media/*", caddyfile)
