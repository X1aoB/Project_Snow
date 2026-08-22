from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase


class DeploymentContractTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app_root = Path(__file__).resolve().parents[1]

    def read(self, relative: str) -> str:
        return (self.app_root / relative).read_text(encoding="utf-8")

    def run_posix_shell(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        candidates = (
            "C:/Program Files/Git/bin/bash.exe",
            shutil.which("sh"),
            shutil.which("bash"),
        )
        shell = next((Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)
        self.assertIsNotNone(shell, "A POSIX shell is required for deployment contract tests.")
        with tempfile.TemporaryDirectory() as temporary_root:
            script_path = Path(temporary_root) / "contract.sh"
            script_path.write_text(script, encoding="utf-8", newline="\n")
            return subprocess.run(
                [str(shell), str(script_path), *arguments],
                check=False,
                capture_output=True,
                text=True,
            )

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
        self.assertIn('expose: ["8000"]', api)
        colour_services = compose[
            compose.index("  public-api-blue:\n"):compose.index("\n  admin:")
        ]
        self.assertNotIn("ports:", colour_services)
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

    def test_direct_origin_is_a_hardened_tcp_only_sidecar(self) -> None:
        compose = self.read("compose.prod.yml")
        origin_caddyfile = self.read("infra/OriginEdge.Caddyfile")
        caddy = compose[compose.index("  caddy:\n"):compose.index("\n  origin-edge:")]
        origin_edge = compose[
            compose.index("  origin-edge:\n"):compose.index("\n  cloudflared:")
        ]
        networks = compose[compose.index("\nnetworks:\n"):]

        self.assertIn("image: ${CADDY_IMAGE:?Set immutable CADDY_IMAGE digest}", origin_edge)
        self.assertIn('      - "443:8443/tcp"', origin_edge)
        self.assertEqual(origin_edge.count("443:8443/tcp"), 1)
        self.assertNotIn("/udp", origin_edge)
        self.assertNotIn("h3", origin_caddyfile)
        self.assertNotIn("ports:", caddy)
        self.assertIn("networks: [edge-client, origin-backend, app]", caddy)
        self.assertIn("networks: [origin-backend, origin-uplink]", origin_edge)
        self.assertNotIn("edge-client", origin_edge)
        self.assertIn('dns: ["127.0.0.1"]', origin_edge)
        self.assertIn("cap_drop: [\"ALL\"]", origin_edge)
        self.assertIn("security_opt: [\"no-new-privileges:true\"]", origin_edge)
        self.assertIn("read_only: true", origin_edge)
        for material in ("origin-cert.pem", "origin-key.pem", "aop-ca.pem"):
            self.assertIn(
                f"${{ORIGIN_TLS_ROOT:?Set verified immutable origin TLS root}}/{material}",
                origin_edge,
            )
            self.assertIn(f"/run/project-snow-origin/{material}:ro", origin_edge)

        self.assertIn("auto_https off", origin_caddyfile)
        self.assertIn("protocols h1 h2", origin_caddyfile)
        self.assertIn("https://:8443", origin_caddyfile)
        self.assertNotIn("https://snow.xiaob.dev:8443", origin_caddyfile)
        self.assertEqual(origin_caddyfile.count("client_auth"), 1)
        self.assertIn("mode require_and_verify", origin_caddyfile)
        self.assertIn("trust_pool file /run/project-snow-origin/aop-ca.pem", origin_caddyfile)
        self.assertIn("@snow host snow.xiaob.dev", origin_caddyfile)
        self.assertIn("handle @snow", origin_caddyfile)
        self.assertIn("max_size 65536", origin_caddyfile)
        self.assertIn("reverse_proxy http://caddy:8080", origin_caddyfile)
        self.assertIn("respond 421", origin_caddyfile)
        self.assertIn("  cloudflared:\n", compose)
        self.assertIn("networks: [edge-client, tunnel-uplink]", compose)
        self.assertIn("  origin-backend:\n    name: ps-origin1", networks)
        self.assertIn("internal: true", networks[networks.index("  origin-backend:"):networks.index("  origin-uplink:")])
        self.assertIn("com.docker.network.bridge.name: ps-origin1", networks)
        self.assertIn("  origin-uplink:\n    name: ps-origin0", networks)
        self.assertIn("driver: bridge", networks)
        self.assertIn("com.docker.network.bridge.name: ps-origin0", networks)

    def test_deploy_stages_only_the_inactive_colour(self) -> None:
        script = self.read("ops/deploy.sh")
        verify_data = script.index(
            'python -m backend.snow_app.data_loader --release-root "$candidate_data_root" --verify-only'
        )
        load_data = script.index(
            'python -m backend.snow_app.data_loader --release-root "$candidate_data_root"',
            verify_data + 1,
        )
        dependency_probe = script.index("PROJECT_SNOW_DATA_DEPENDENCIES")
        start_api = script.index('compose up -d "$service"')
        smoke = script.index("/app/public_smoke.py")
        acceptance_target = script.index("candidate_app_network=project-snow-public_app")
        acceptance_smoke = script.index('candidate_internal_endpoint=')
        stage_env = script.index('mv -f -- "$candidate_env" "$colour_env"')
        self.assertLess(verify_data, load_data)
        self.assertLess(dependency_probe, load_data)
        self.assertLess(load_data, start_api)
        self.assertLess(smoke, stage_env)
        self.assertLess(smoke, acceptance_target)
        self.assertLess(acceptance_target, acceptance_smoke)
        self.assertLess(acceptance_smoke, stage_env)
        self.assertIn('--env-file "$candidate_env"', script)
        self.assertIn("/etc/project-snow/images.env", script)
        self.assertIn("/srv/project-snow/runtime/compose.env", script)
        self.assertIn("active-colour", script)
        self.assertIn("Active-colour marker is required before staging", script)
        self.assertIn("Active-colour marker exists but is not readable", script)
        self.assertIn('case "$active_colour" in blue|green)', script)
        self.assertNotIn("tr -d '[:space:]' < \"$active_file\"", script)
        self.assertIn("Caddy, origin-edge and cloudflared keep", script)
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
        self.assertIn('pending = {("qdrant", 6333), ("neo4j", 7687)}', script)
        self.assertIn("time.monotonic() + 60", script)
        self.assertIn("Qdrant or Neo4j did not become reachable", script)
        self.assertNotIn("systemctl restart docker", script)
        self.assertNotIn("force-recreate", script)
        self.assertIn("Staged API network attachment is not the exact internal-only policy", script)
        self.assertIn("Expected exactly one staged API container", script)
        self.assertIn("project-snow-public_app", script)
        self.assertIn("project-snow-public_data", script)
        self.assertIn("project-snow-public_egress-client", script)
        self.assertIn('.HostConfig.PortBindings // {}) | length == 0', script)
        self.assertIn('.Internal == true', script)
        self.assertIn('.Labels["com.docker.compose.project"] == "project-snow-public"', script)
        self.assertIn('.Labels["com.docker.compose.network"] == $role', script)
        self.assertIn("ipaddress.ip_address", script)
        self.assertIn("address in subnet", script)
        self.assertIn("SSH tunnel only", script)
        self.assertIn("-H 'Host: snow.xiaob.dev'", script)
        firewall_install = script.index(
            'install_direct_origin_firewall "$candidate_config_root"'
        )
        self.assertLess(acceptance_smoke, firewall_install)
        self.assertLess(firewall_install, stage_env)

    def test_direct_origin_firewall_is_installed_and_gates_edge_recreation(self) -> None:
        deploy = self.read("ops/deploy.sh")
        promote = self.read("ops/promote.sh")
        service = self.read("ops/project-snow-origin-firewall.service")

        helper_install = deploy.index(
            'install -o root -g root -m 0755 "$firewall_source" "$firewall_binary"'
        )
        unit_install = deploy.index(
            '"$firewall_config_root/ops/project-snow-origin-firewall.service"'
        )
        daemon_reload = deploy.index("systemctl daemon-reload", unit_install)
        first_update = deploy.index('"$firewall_binary" update', daemon_reload)
        enable_units = deploy.index(
            "systemctl enable project-snow-origin-firewall.service "
            "project-snow-origin-firewall.timer",
            first_update,
        )
        systemd_exec_check = deploy.index(
            "systemctl cat --no-pager project-snow-origin-firewall.service",
            enable_units,
        )
        self.assertLess(helper_install, unit_install)
        self.assertLess(unit_install, daemon_reload)
        self.assertLess(daemon_reload, first_update)
        self.assertLess(first_update, enable_units)
        self.assertLess(enable_units, systemd_exec_check)
        self.assertIn("/usr/local/sbin/project-snow-origin-firewall", deploy)
        self.assertIn('"$firewall_binary|0:0:755:1"', deploy)
        self.assertIn("project-snow-origin-firewall.service|0:0:644:1", deploy)
        self.assertIn("project-snow-origin-firewall.timer|0:0:644:1", deploy)
        self.assertIn("stat -c %u:%g:%a:%h", deploy)
        self.assertIn(
            "[ \"$firewall_unit_exec_start\" = "
            "'/usr/local/sbin/project-snow-origin-firewall update' ]",
            deploy,
        )
        self.assertIn("systemctl is-enabled --quiet project-snow-origin-firewall.service", deploy)
        self.assertIn("systemctl is-active --quiet project-snow-origin-firewall.timer", deploy)

        update_gate = promote.index("run_origin_firewall update")
        target_switch = promote.index(
            'switch_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        restore_gate = promote.index("run_origin_firewall restore")
        previous_switch = promote.index(
            'switch_edge "$previous_env" "$previous_config_root" "$previous_colour"'
        )
        self.assertLess(update_gate, target_switch)
        self.assertLess(restore_gate, previous_switch)
        self.assertIn("public 443 fail-closed", promote)
        self.assertNotIn("ufw allow 443", deploy)
        self.assertIn("Before=docker.service", service)
        self.assertIn("RequiredBy=docker.service", service)
        self.assertIn(
            "ExecStart=/usr/local/sbin/project-snow-origin-firewall update", service
        )
        self.assertNotIn("/srv/project-snow/app/scripts/cloudflare_origin_firewall.py", service)

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
        self.assertIn('up -d --no-deps --force-recreate "$@"', script)
        self.assertNotIn("--force-recreate caddy cloudflared", script)
        self.assertIn("service_list_has", script)
        self.assertIn('set -- caddy', script)
        self.assertNotIn('set -- "$@" origin-edge', script)
        self.assertIn('set -- "$@" cloudflared', script)
        self.assertIn('set -- "$@" egress-proxy', script)
        self.assertIn("start origin-edge", script)
        self.assertIn(
            'switch_edge "$previous_env" "$previous_config_root" "$previous_colour"',
            script,
        )
        self.assertIn('"$previous_services" "$previous_allow_origin"', script)
        self.assertIn('"$target_services" "$target_allow_origin"', script)
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
        firewall_update = script.index("run_origin_firewall update")
        restore_runtime_start = script.index("restore_previous_runtime() {")
        target_origin_prepare = script.index(
            'prepare_or_retain_origin_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        previous_origin_prepare = script.index(
            'prepare_or_retain_origin_edge "$previous_env" "$previous_config_root" "$previous_colour"',
            restore_runtime_start,
        )
        firewall_restore = script.index("run_origin_firewall restore", previous_origin_prepare)
        target_origin_probe = script.index(
            'probe_prepared_origin_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        previous_origin_probe = script.index(
            'probe_prepared_origin_edge "$previous_env" "$previous_config_root" "$previous_colour"',
            firewall_restore,
        )
        self.assertLess(target_origin_prepare, firewall_update)
        self.assertLess(previous_origin_prepare, firewall_restore)
        self.assertLess(firewall_update, target_origin_probe)
        self.assertLess(firewall_restore, previous_origin_probe)
        self.assertLess(target_origin_probe, switch)
        self.assertLess(previous_origin_probe, restore_edge)
        self.assertLess(firewall_update, switch)
        self.assertLess(firewall_restore, restore_edge)
        self.assertIn("validate_origin_edge_material", script)
        self.assertIn("validate_origin_edge_container", script)
        self.assertIn("validate_origin_edge_network", script)
        self.assertIn("up --no-start --no-deps --force-recreate origin-edge", script)
        self.assertIn('docker network inspect "$origin_edge_network"', script)
        self.assertIn("validate_docker_dns_security_floor", script)
        self.assertIn("docker version --format '{{.Server.Version}}'", script)
        self.assertIn("at or above 26.0.0", script)
        self.assertIn(".Driver == \"bridge\"", script)
        self.assertIn(".[0].Internal == false", script)
        self.assertIn(".[0].EnableIPv6 == false", script)
        self.assertIn('Options["com.docker.network.bridge.name"]', script)
        self.assertIn('Labels["com.docker.compose.project"] == "project-snow-public"', script)
        self.assertIn('Labels["com.docker.compose.network"] == "origin-uplink"', script)
        self.assertIn('.Labels["com.docker.compose.network"] == "origin-backend"', script)
        self.assertIn(".[0].Internal == true", script)
        self.assertIn('/usr/sbin/ip -json -details link show dev "$origin_edge_network"', script)
        self.assertIn('/usr/sbin/ip -json -details link show dev "$origin_edge_internal_network"', script)
        self.assertIn(
            'for unmanaged_origin_bridge in "$origin_edge_network" "$origin_edge_internal_network"',
            script,
        )
        self.assertIn('/usr/sbin/ip link show dev "$unmanaged_origin_bridge"', script)
        self.assertIn("origin_uplink_exists", script)
        self.assertIn("origin_backend_exists", script)
        self.assertIn("Refusing to adopt an unmanaged existing origin bridge", script)
        self.assertIn(".[0].State.Running == $expected_running", script)
        self.assertIn(
            'validate_origin_edge_container "$prepare_env" "$prepare_config_root" "$prepare_colour" false',
            script,
        )
        self.assertIn('keys) == ["8443/tcp"]', script)
        self.assertIn('HostPort == "443"', script)
        self.assertIn('.[0].HostConfig.Dns == ["127.0.0.1"]', script)
        self.assertIn('.[0].Config.Image == $caddy_image', script)
        self.assertIn(
            '.[0].Config.Cmd == ["caddy", "run", "--config", "/etc/caddy/OriginEdge.Caddyfile", "--adapter", "caddyfile"]',
            script,
        )
        self.assertIn(
            '{Type: "bind", Source: $caddy_config, Destination: "/etc/caddy/OriginEdge.Caddyfile", RW: false}',
            script,
        )
        self.assertIn('map(select(.Type == "bind" or .Type == "volume"))', script)
        self.assertIn("NetworkSettings.Ports", script)
        self.assertIn("($containers | length) == 1", script)
        self.assertIn("($containers | has($container_id))", script)
        self.assertIn(
            "caddy validate --config /etc/caddy/OriginEdge.Caddyfile --adapter caddyfile",
            script,
        )
        self.assertIn("Expected exactly one origin-edge container", script)
        probe_start = script.index("run_origin_network_probe() {")
        probe_end = script.index("\n}\n\nvalidate_running_origin_edge_caddy()", probe_start)
        probe = script[probe_start:probe_end]
        self.assertIn('docker create --name "$probe_candidate"', probe)
        self.assertIn('--network "$origin_edge_network"', probe)
        self.assertIn("--dns 127.0.0.1", probe)
        self.assertIn('docker network connect "$origin_edge_internal_network"', probe)
        self.assertIn('--entrypoint python "$probe_image_id"', probe)
        self.assertIn("socket.getaddrinfo(external_name, 443)", probe)
        self.assertIn('require_tcp_blocked("1.1.1.1", 80, 42)', probe)
        self.assertIn('require_tcp_blocked("1.1.1.1", 53, 43)', probe)
        self.assertIn("socket.SOCK_DGRAM", probe)
        self.assertIn('udp.sendto(query, ("1.1.1.1", 53))', probe)
        self.assertIn("require_tcp_blocked(origin_gateway, 53, 45)", probe)
        self.assertIn("require_tcp_blocked(backend_gateway, 53, 46)", probe)
        self.assertIn("require_tcp_blocked(other_bridge_target, 8000, 47)", probe)
        self.assertIn('socket.getaddrinfo("caddy", 8080)', probe)
        self.assertIn('http.client.HTTPConnection("caddy", 8080, timeout=5)', probe)
        self.assertIn('headers={"Host": "snow.xiaob.dev"}', probe)
        self.assertIn("read_origin_drop_counter", script)
        self.assertIn("run_origin_firewall counters", script)
        self.assertIn("read_origin_drop_counter input_uplink", probe)
        self.assertIn("read_origin_drop_counter input_backend", probe)
        self.assertIn("read_origin_drop_counter forward", probe)
        self.assertIn(
            '"$probe_uplink_input_after" -gt "$probe_uplink_input_before"', probe
        )
        self.assertIn(
            '"$probe_backend_input_after" -gt "$probe_backend_input_before"', probe
        )
        self.assertIn('"$probe_forward_after" -gt "$probe_forward_before"', probe)
        self.assertIn("--read-only --cap-drop ALL --security-opt no-new-privileges", probe)
        self.assertNotIn("origin-key.pem", probe)
        self.assertNotIn("/etc/project-snow/origin-edge", probe)
        self.assertIn("ensure_caddy_origin_backend", script)
        self.assertIn(
            'docker network connect --alias caddy "$origin_edge_internal_network"',
            script,
        )
        self.assertIn('index("caddy")', script)
        self.assertIn("The dedicated origin backend contains an unexpected endpoint", script)
        self.assertIn(
            '([$container_id, $caddy_id] | sort)',
            script,
        )
        retained_runtime = script.index('if [ "$existing_origin_running" = true ]; then')
        retained_caddy_validation = script.index(
            'running_origin_edge_matches_snapshot "$prepare_env" "$prepare_config_root"',
            retained_runtime,
        )
        retained_return = script.index("return 0", retained_runtime)
        stopped_create = script.index(
            "up --no-start --no-deps --force-recreate origin-edge",
            retained_runtime,
        )
        self.assertLess(retained_caddy_validation, retained_return)
        self.assertLess(retained_return, stopped_create)
        stopped_validation = script.index(
            'validate_origin_edge_container "$prepare_env" "$prepare_config_root" "$prepare_colour" false',
            stopped_create,
        )
        start_mode = script.index("origin_edge_prestart_mode=start", stopped_validation)
        probe_wrapper = script.index("probe_prepared_origin_edge() {", start_mode)
        probe_call = script.index(
            'run_origin_network_probe "$prepared_env"', probe_wrapper
        )
        self.assertLess(stopped_create, stopped_validation)
        self.assertLess(stopped_validation, start_mode)
        self.assertLess(start_mode, probe_call)
        self.assertIn("origin_edge_prestart_mode=retain", script)
        self.assertIn("origin_edge_prestart_mode=start", script)
        self.assertIn('"$target_origin_mode"', script)
        self.assertIn('"$previous_origin_mode"', script)
        self.assertIn("root-owned mode-0400 single regular file", script)
        self.assertIn(
            "A direct-origin target must retain cloudflared until a separately authorized edge migration.",
            script,
        )
        self.assertNotIn("remove target-only origin-edge", script)
        self.assertNotIn("remove target-only cloudflared", script)
        self.assertNotIn(
            'elif [ "$target_has_origin_edge" -ne 1 ] && [ "$previous_has_origin_edge" -eq 1 ]',
            script,
        )
        self.assertNotIn(
            'if [ "$target_has_cloudflared" -ne 1 ] && [ "$previous_has_cloudflared" -eq 1 ]',
            script,
        )
        cloudflared_lines = "\n".join(
            line for line in script.splitlines() if "cloudflared" in line
        )
        self.assertNotIn("stop_snapshot_service", cloudflared_lines)
        for previous_fail_closed_message in (
            "the exact pre-upgrade origin-edge could not be restored",
            "previous origin-edge could not pass its retained-runtime or stopped pre-start gate",
            "failed to restore the last-known-good origin firewall",
            "previous origin-edge failed its post-firewall no-secret network isolation probe",
        ):
            failure_message = script.index(previous_fail_closed_message)
            fail_closed_assignment = script.index(
                "previous_allow_origin=0", failure_message
            )
            self.assertLess(fail_closed_assignment - failure_message, 600)
        for target_fail_closed_message in (
            "Target origin-edge could not pass its retained-runtime or stopped pre-start gate",
            "Rollback could not restore the last-known-good origin firewall",
            "Target origin-edge failed its post-firewall no-secret network isolation probe",
        ):
            failure_message = script.index(target_fail_closed_message)
            fail_closed_assignment = script.index("target_allow_origin=0", failure_message)
            self.assertLess(fail_closed_assignment - failure_message, 600)
        self.assertIn(
            "previous origin-edge could not pass its retained-runtime or stopped pre-start gate; keeping public 443 fail-closed",
            script,
        )
        self.assertIn(
            "Target origin-edge could not pass its retained-runtime or stopped pre-start gate; public 443 was not started",
            script,
        )
        self.assertIn(
            'if ! run_origin_firewall restore; then\n'
            "      echo 'CRITICAL: failed to restore the last-known-good origin firewall; keeping public 443 fail-closed.' >&2\n"
            "      previous_allow_origin=0",
            script,
        )
        self.assertIn(
            'if ! run_origin_firewall restore; then\n'
            "    echo 'Rollback could not restore the last-known-good origin firewall; public 443 will remain fail-closed.' >&2\n"
            "    target_allow_origin=0",
            script,
        )
        self.assertIn("previous public API did not become ready for restoration", script)
        self.assertIn(
            "Cloudflare Access and MyWebsite settings were not changed", script
        )
        self.assertNotIn("cloudflared tunnel route dns", script)

    def test_origin_edge_bundle_replacement_requires_and_retains_tunnel_fallback(self) -> None:
        script = self.read("ops/promote.sh")
        begin_start = script.index("begin_origin_edge_replacement() {")
        begin_end = script.index("\n}\n\nprepare_or_retain_origin_edge()", begin_start) + 2
        begin = script[begin_start:begin_end]
        tunnel_gate = begin.index("require_running_cloudflared_fallback")
        persistent_recovery = begin.index("persist_live_origin_edge_binding")
        recovery_binding = begin.index("origin_edge_replacement_restore_env=")
        mutation_phase = begin.index("promote_signal_phase=switching")
        remove_old = begin.index("remove_snapshot_origin_edge_if_present")
        self.assertLess(tunnel_gate, recovery_binding)
        self.assertLess(tunnel_gate, persistent_recovery)
        self.assertLess(persistent_recovery, recovery_binding)
        self.assertLess(recovery_binding, mutation_phase)
        self.assertLess(mutation_phase, remove_old)
        self.assertIn("origin_edge_prestart_failure_preserve=1", begin)
        self.assertIn("origin_edge_tunnel_retain=1", begin)

        prepare_start = script.index("prepare_or_retain_origin_edge() {")
        prepare_end = script.index("\n}\n\nprobe_prepared_origin_edge()", prepare_start)
        prepare = script[prepare_start:prepare_end]
        overlay_mode = prepare.index("origin_edge_prestart_mode=overlay")
        replacement = prepare.index("begin_origin_edge_replacement")
        self.assertLess(overlay_mode, replacement)
        self.assertIn('[ "$rollback_mode" = 1 ]', prepare)
        self.assertIn('[ "$prepare_tls_root" = /etc/project-snow/origin-edge ]', prepare)

        restore_start = script.index("restore_replaced_origin_edge() {")
        restore_end = script.index("\n}\n\nswitch_edge()", restore_start) + 2
        restore = script[restore_start:restore_end]
        remove_target = restore.index("remove_snapshot_origin_edge_if_present")
        recreate_old = restore.index("up --no-start --no-deps --force-recreate origin-edge")
        firewall = restore.index("run_origin_firewall restore")
        probe = restore.index("probe_prepared_origin_edge")
        start_old = restore.index("start origin-edge")
        final_tunnel = restore.rindex("require_running_cloudflared_fallback")
        self.assertLess(remove_target, recreate_old)
        self.assertLess(recreate_old, firewall)
        self.assertLess(firewall, probe)
        self.assertLess(probe, start_old)
        self.assertLess(start_old, final_tunnel)
        self.assertIn("origin_edge_replacement_active=0", restore)

        switch_start = script.index("switch_edge() {")
        switch_end = script.index("\n}\n\nstop_snapshot_service()", switch_start)
        switch = script[switch_start:switch_end]
        self.assertIn('if [ "$origin_edge_tunnel_retain" -eq 1 ]; then', switch)
        self.assertIn("overlay)", switch)
        self.assertIn('edge_runtime_env="$origin_edge_overlay_env"', switch)
        self.assertIn("edge_retain_tunnel=1", switch)
        self.assertIn('set -- "$@" cloudflared', switch)
        self.assertIn("require_running_cloudflared_fallback", switch)
        self.assertIn("project-snow-public_edge-client", script)
        self.assertIn("project-snow-public_tunnel-uplink", script)
        for legacy_mount in (
            "/etc/project-snow/origin-edge/origin-cert.pem:/run/project-snow-origin/origin-cert.pem:ro",
            "/etc/project-snow/origin-edge/origin-key.pem:/run/project-snow-origin/origin-key.pem:ro",
            "/etc/project-snow/origin-edge/aop-ca.pem:/run/project-snow-origin/aop-ca.pem:ro",
        ):
            self.assertIn(legacy_mount, script)

        harness = f"""\
set -u
gate_mode=$1
log_file=$2
test_root=${{log_file%/*}}
previous_has_origin_edge=1
previous_env=previous.env
previous_config_root=previous-config
previous_colour=blue
origin_edge_prestart_failure_preserve=0
origin_edge_tunnel_retain=0
origin_edge_replacement_active=0
origin_edge_replacement_restore_env=
origin_edge_replacement_restore_config_root=
origin_edge_replacement_restore_colour=
origin_edge_replacement_restore_binding=
origin_edge_replacement_target_env=
origin_edge_replacement_target_config_root=
origin_edge_replacement_target_colour=
promote_signal_phase=pre-switch
live_origin_edge_binding="$test_root/live-origin-edge-config.json"
running_origin_edge_matches_snapshot() {{ printf '%s\n' match >> "$log_file"; }}
require_running_cloudflared_fallback() {{
  printf '%s\n' tunnel >> "$log_file"
  [ "$gate_mode" = pass ]
}}
persist_live_origin_edge_binding() {{
  printf '%s\n' persist >> "$log_file"
  printf '%s\n' live > "$live_origin_edge_binding"
  origin_edge_retained_env="$1"
  origin_edge_retained_config_root="$2"
  origin_edge_retained_colour="$3"
}}
chown() {{ :; }}
chmod() {{ :; }}
fsync_promote_path() {{ :; }}
validate_live_origin_edge_binding() {{ :; }}
remove_snapshot_origin_edge_if_present() {{ printf '%s\n' remove >> "$log_file"; }}
{begin}
begin_status=0
begin_origin_edge_replacement target.env target-config green \
  previous.env previous-config blue || begin_status=$?
printf '%s:%s:%s:%s\n' "$origin_edge_replacement_active" \
  "$origin_edge_prestart_failure_preserve" "$origin_edge_tunnel_retain" \
  "$promote_signal_phase"
exit "$begin_status"
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            log = Path(temporary_root) / "success.log"
            success = self.run_posix_shell(harness, "pass", log.as_posix())
            success_log = log.read_text().splitlines()
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(success.stdout.strip(), "1:0:1:switching")
        self.assertEqual(success_log, ["match", "tunnel", "persist", "remove", "tunnel"])

        with tempfile.TemporaryDirectory() as temporary_root:
            log = Path(temporary_root) / "failure.log"
            failure = self.run_posix_shell(harness, "fail", log.as_posix())
            failure_log = log.read_text().splitlines()
        self.assertNotEqual(failure.returncode, 0)
        self.assertEqual(failure_log, ["match", "tunnel"])

    def test_live_origin_edge_binding_survives_colour_restaging_and_rotation(self) -> None:
        script = self.read("ops/promote.sh")
        binding_start = script.index("origin_context_config_binding() {")
        binding_end = script.index("\n}\n\nvalidate_mailer_env()", binding_start) + 2
        binding_function = script[binding_start:binding_end]
        discover_call = script.index("if ! discover_running_origin_edge_binding; then")
        target_prepare = script.index(
            'prepare_or_retain_origin_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        self.assertLess(discover_call, target_prepare)
        self.assertIn("/srv/project-snow/releases/live-origin-edge-config.json", script)
        self.assertIn("/srv/project-snow/runtime/origin-edge", script)
        self.assertIn("project-snow-live-origin-edge-1", script)
        self.assertIn('fsync_promote_path "$live_origin_edge_env_root"', script)
        self.assertIn('fsync_promote_path "$persist_immutable_env"', script)
        self.assertIn("origin_edge_replacement_restore_binding", script)
        self.assertIn(
            'persist_live_origin_edge_binding "$origin_edge_replacement_restore_env"',
            script,
        )
        persist_start = script.index("persist_live_origin_edge_binding() {")
        persist_end = script.index("\n}\n\ndiscover_running_origin_edge_binding()", persist_start)
        persist = script[persist_start:persist_end]
        loaded_assignment = persist.rindex("live_origin_edge_binding_loaded=1")
        signal_restore = persist.rindex("restore_promote_signal_traps")
        self.assertLess(loaded_assignment, signal_restore)

        harness = f"""\
set -u
colour_env=/runtime/colours/blue.compose.env
colour_config_root=/config/new
colour=blue
colour_config_binding=/releases/blue-config.json
previous_env=/runtime/colours/green.compose.env
previous_config_root=/config/legacy
previous_colour=green
previous_config_binding=/releases/green-config.json
live_origin_edge_binding_loaded=1
live_origin_edge_binding=/releases/live-origin-edge-config.json
origin_edge_retained_env=/runtime/origin-edge/old.compose.env
origin_edge_retained_config_root=/config/old
origin_edge_retained_colour=green
origin_edge_replacement_restore_binding=
origin_edge_replacement_restore_config_root=
origin_edge_replacement_restore_colour=
{binding_function}
origin_context_config_binding /runtime/origin-edge/old.compose.env /config/old green
origin_edge_retained_env=/runtime/origin-edge/new.compose.env
origin_edge_retained_config_root=/config/new
origin_edge_retained_colour=blue
origin_edge_replacement_restore_binding=/releases/old.restore.json
origin_edge_replacement_restore_config_root=/config/old
origin_edge_replacement_restore_colour=green
origin_context_config_binding /runtime/origin-edge/old.compose.env /config/old green
"""
        result = self.run_posix_shell(harness)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "/releases/live-origin-edge-config.json",
                "/releases/old.restore.json",
            ],
        )

    def test_promoted_metadata_commit_restores_exact_previous_set_on_fault(self) -> None:
        script = self.read("ops/promote.sh")
        restore_start = script.index("restore_promoted_file() {")
        commit_start = script.index("commit_promoted_state() {", restore_start)
        commit_end = script.index("\n}\n\nif ! commit_promoted_state", commit_start) + 2
        transaction = script[restore_start:commit_end]
        target_switch = script.index(
            'if ! switch_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        switching = script.rindex("promote_signal_phase=switching", 0, target_switch)
        for backup_assignment in (
            "previous_state_backup=",
            "previous_active_backup=",
            "previous_manifest_backup=",
            "previous_config_backup=",
            "previous_current_backup=",
        ):
            self.assertLess(script.index(backup_assignment, script.index('origin_probe_container=""')), switching)
        self.assertIn("restore_previous_promoted_state || true", transaction)
        self.assertIn('fsync_promote_path "$runtime_root" || promoted_restore_failed=1', transaction)
        self.assertIn("promoted_state_recovery_failed=1", transaction)

        harness = f"""\
set -u
test_root=$1
failure_mode=$2
runtime_root="$test_root/runtime"
release_root="$test_root/releases"
mkdir -p "$runtime_root" "$release_root"
current_env="$runtime_root/compose.env"
active_file="$release_root/active-colour"
current_manifest="$release_root/current-manifest.json"
current_config_binding="$release_root/current-config.json"
release_current="$release_root/current"
state_tmp="$runtime_root/state.target"
active_tmp="$release_root/active.target"
manifest_tmp="$release_root/manifest.target"
config_tmp="$release_root/config.target"
current_tmp="$release_root/current.target"
previous_state_backup="$runtime_root/state.previous"
previous_active_backup="$release_root/active.previous"
previous_manifest_backup="$release_root/manifest.previous"
previous_config_backup="$release_root/config.previous"
previous_current_backup="$release_root/current.previous"
for coordinate in \
  "$current_env|$previous_state_backup|state" \
  "$active_file|$previous_active_backup|active" \
  "$current_manifest|$previous_manifest_backup|manifest" \
  "$current_config_binding|$previous_config_backup|config" \
  "$release_current|$previous_current_backup|current"; do
  destination=${{coordinate%%|*}}
  remainder=${{coordinate#*|}}
  backup=${{remainder%%|*}}
  value=${{remainder#*|}}
  printf 'old-%s\n' "$value" > "$destination"
  printf 'old-%s\n' "$value" > "$backup"
done
printf 'new-state\n' > "$state_tmp"
printf 'new-active\n' > "$active_tmp"
printf 'new-manifest\n' > "$manifest_tmp"
printf 'new-config\n' > "$config_tmp"
printf 'new-current\n' > "$current_tmp"
promoted_restore_tmp=
promoted_state_recovery_failed=0
promote_signal_phase=switching
origin_edge_replacement_active=1
origin_edge_tunnel_retain=1
restore_promote_signal_traps() {{ :; }}
chown() {{ :; }}
stat() {{ printf '%s\n' 0:0:600:1; }}
mv_call_count=0
mv() {{
  mv_call_count=$((mv_call_count + 1))
  if [ "$failure_mode" = move ] && [ "$mv_call_count" -eq 2 ]; then
    return 1
  fi
  if [ "$failure_mode" = restore ] &&
     {{ [ "$mv_call_count" -eq 2 ] || [ "$mv_call_count" -eq 3 ]; }}; then
    return 1
  fi
  command mv "$@"
}}
fsync_failed=0
fsync_promote_path() {{
  if [ "$failure_mode" = fsync ] && [ "$1" = "$runtime_root" ] &&
     [ "$fsync_failed" -eq 0 ]; then
    fsync_failed=1
    return 1
  fi
  return 0
}}
{transaction}
commit_status=0
commit_promoted_state || commit_status=$?
printf '%s:%s\n' "$commit_status" "$promoted_state_recovery_failed"
for destination in "$current_env" "$active_file" "$current_manifest" \
  "$current_config_binding" "$release_current"; do
  cat "$destination"
done
"""
        for failure_mode in ("move", "fsync"):
            with self.subTest(failure_mode=failure_mode), tempfile.TemporaryDirectory() as temporary_root:
                result = self.run_posix_shell(harness, Path(temporary_root).as_posix(), failure_mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], "1:0")
            self.assertEqual(
                result.stdout.splitlines()[1:],
                ["old-state", "old-active", "old-manifest", "old-config", "old-current"],
            )

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            failed_restore = self.run_posix_shell(harness, root.as_posix(), "restore")
            backups_remain = all(
                (root / relative).is_file()
                for relative in (
                    "runtime/state.previous",
                    "releases/active.previous",
                    "releases/manifest.previous",
                    "releases/config.previous",
                    "releases/current.previous",
                )
            )
        self.assertEqual(failed_restore.returncode, 0, failed_restore.stderr)
        self.assertEqual(failed_restore.stdout.splitlines()[0], "1:1")
        self.assertTrue(backups_remain)

    def test_promote_signal_restores_after_switch_and_acknowledges_only_commit(self) -> None:
        script = self.read("ops/promote.sh")
        cleanup_start = script.index("cleanup() {", script.index('origin_probe_container=""'))
        control_end_marker = "restore_promote_signal_traps || exit 78"
        cleanup_end = script.index(control_end_marker, cleanup_start) + len(control_end_marker)
        signal_control = script[cleanup_start:cleanup_end]
        self.assertIn("trap cleanup EXIT", signal_control)
        self.assertIn("terminate_promote_signal 143", signal_control)
        self.assertIn("restore_previous_runtime", signal_control)
        self.assertIn("committed)", signal_control)
        self.assertNotIn("trap cleanup EXIT HUP", script)

        switch_call = script.index(
            'switch_edge "$colour_env" "$colour_config_root" "$colour"'
        )
        switching_phase = script.rindex("promote_signal_phase=switching", 0, switch_call)
        commit_start = script.index("commit_promoted_state() {")
        commit_phase = script.index("promote_signal_phase=committed", commit_start)
        self.assertLess(switching_phase, switch_call)
        self.assertLess(switch_call, commit_phase)
        self.assertIn("trap '' HUP INT QUIT TERM PIPE || return 1", script[commit_start:commit_phase])
        access_start = script.index("verify_cloudflare_access_restored() (")
        access_end = script.index("\n)", access_start)
        access_probe = script[access_start:access_end]
        self.assertIn("trap cleanup_access_probe EXIT", access_probe)
        self.assertIn("trap 'exit 143' TERM", access_probe)

        harness = f"""\
set -u
promote_signal_phase=$1
test_root=$2
restore_log="$test_root/restored"
origin_probe_container=
state_tmp="$test_root/state.tmp"
active_tmp="$test_root/active.tmp"
manifest_tmp="$test_root/manifest.tmp"
config_tmp="$test_root/config.tmp"
printf '%s\n' tmp > "$state_tmp"
restore_previous_runtime() {{ printf '%s\n' restored > "$restore_log"; }}
{signal_control}
kill -TERM "$$"
exit 99
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            before = self.run_posix_shell(harness, "pre-switch", root.as_posix())
            before_restored = (root / "restored").exists()
        self.assertEqual(before.returncode, 143, before.stderr)
        self.assertFalse(before_restored)

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            switching = self.run_posix_shell(harness, "switching", root.as_posix())
            switching_restored = (root / "restored").is_file()
        self.assertEqual(switching.returncode, 143, switching.stderr)
        self.assertTrue(switching_restored)

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            committed = self.run_posix_shell(harness, "committed", root.as_posix())
            committed_restored = (root / "restored").exists()
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertFalse(committed_restored)

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
        self.assertIn("jq nftables openssl", script)
        self.assertIn("/etc/project-snow/origin-edge", script)
        self.assertIn("Refusing to disable root SSH", script)
        self.assertIn("ufw allow 43556/tcp", script)
        self.assertLess(script.index("ufw --force enable"), script.index("systemctl restart docker", script.index("ufw --force enable")))
        self.assertIn("/run/project-snow-docker-after-firewall", script)
        self.assertIn("20-project-snow-after-ufw.conf", script)
        self.assertIn("Wants=ufw.service", script)
        self.assertIn("After=ufw.service", script)
        self.assertIn("stat -c %u:%g:%a:%h", script)
        self.assertIn('current_effective_ports="$(sshd -T', script)
        self.assertIn("grep -vx '43556'", script)
        self.assertIn('current_effective_ports" = 22', script)
        self.assertIn("install -o root -g root -m 0755 -d /srv/project-snow", script)
        self.assertIn("-m 0755 -d /srv/project-snow/data", script)
        self.assertIn("/srv/project-snow/media/stickers/releases", script)
        self.assertIn("/srv/project-snow/media/stickers/staging", script)
        self.assertIn("install -o root -g root -m 0750 -d /etc/project-snow", script)
        self.assertIn("install -o deploy -g deploy -m 0700 -d /srv/project-snow/inbox", script)
        self.assertIn(
            '/bin/sh "$script_dir/bootstrap-release-runner.sh" --controller-sha',
            script,
        )
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
        self.assertIn("OriginPrivateKeyPath", script)
        self.assertIn("origin-key-$Sha-$originPrivateKeySha256.pem", script)
        self.assertIn("Get-FileHash -Algorithm SHA256", script)
        self.assertIn("chmod 0600", script)
        self.assertIn("rm -f -- '$originPrivateKeyRemotePath'", script)

    def test_origin_tls_delivery_is_exact_private_and_additive(self) -> None:
        installer = self.read("scripts/install_origin_tls.py")
        deploy = self.read("ops/deploy.sh")
        runner = self.read("ops/project-snow-release")
        promote = self.read("ops/promote.sh")
        manifest = self.read("scripts/release_manifest.py")
        compose = self.read("compose.prod.yml")
        local_deploy = self.read("scripts/deploy.ps1")

        for public_path in (
            "config/origin-edge/origin-cert.pem",
            "config/origin-edge/aop-ca.pem",
        ):
            self.assertIn(public_path, manifest)
            self.assertIn(public_path, deploy)
            self.assertIn(public_path, local_deploy)
            self.assertNotIn(public_path.replace(".pem", "-key.pem"), manifest)
        self.assertIn('b"PRIVATE KEY" in payload', manifest)
        self.assertIn("direct_origin_tls", manifest)
        self.assertIn("project-snow-origin-tls-1", runner)
        self.assertIn("install_origin_tls.py", deploy)
        self.assertIn("/srv/project-snow/inbox", installer)
        self.assertIn("/etc/project-snow/origin-edge", installer)
        self.assertNotIn('parser.add_argument("--inbox"', installer)
        self.assertNotIn('parser.add_argument("--destination-root"', installer)

        for security_gate in (
            "os.O_NOFOLLOW",
            "os.O_NONBLOCK",
            "os.fstat(descriptor)",
            "stat.S_ISREG",
            "metadata.st_uid",
            "metadata.st_gid",
            "stat.S_IMODE(metadata.st_mode)",
            "metadata.st_nlink != 1",
            "metadata.st_size",
            "hashlib.sha256(payload).hexdigest()",
            "path.unlink()",
            "os.rename(candidate, final_bundle)",
            "os.fsync(descriptor)",
        ):
            self.assertIn(security_gate, installer)
        self.assertIn("origin private key does not match the Git-bound certificate", installer)
        self.assertIn('"-passin", "pass:"', installer)
        self.assertIn("one exact origin private key is required", installer)
        self.assertIn("uploaded origin key differs from the immutable installed key", installer)
        self.assertIn('mode=0o600', installer)
        self.assertIn('mode=0o400', installer)
        self.assertIn("ORIGIN_TLS_ROOT", compose)
        self.assertIn(
            "^/etc/project-snow/origin-edge/releases/[0-9a-f]{64}$", promote
        )
        self.assertIn("map({Type, Source, Destination, RW})", promote)

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

    def test_release_runner_preflights_host_before_any_mutation(self) -> None:
        runner = self.read("ops/project-snow-release")
        bootstrap = self.read("ops/bootstrap-release-runner.sh")
        deployment_guide = self.read("docs/public_deployment.md")
        preflight_start = runner.index("host_safety_preflight_active() {")
        preflight_end = runner.index("\n}\n\nrequire_host_safety_preflight()", preflight_start)
        preflight = runner[preflight_start:preflight_end]
        for probe in (
            "sshd_policy_active",
            "ufw_policy_active",
            "fail2ban_sshd_active",
            "deploy_lacks_docker_group",
            "deploy_cannot_access_docker",
            "deploy_processes_lack_docker_socket_group",
        ):
            self.assertIn(probe, preflight)
        require_start = runner.index("require_host_safety_preflight() {")
        require_end = runner.index("\n}\n\nacquire_release_lock()", require_start)
        require = runner[require_start:require_end]
        self.assertIn("host_safety_preflight_active && return 0", require)
        self.assertIn("Host safety preflight failed.", require)
        self.assertIn("deploy_process_has_docker_socket_group", runner)
        self.assertIn("host_safety_preflight", runner)

        mutating_start = runner.index("run_mutating_operation() {")
        dispatch_start = runner.index('\ncase "$operation" in', mutating_start)
        mutating = runner[mutating_start:dispatch_start]
        safety_gate = mutating.index("require_host_safety_preflight")
        acquire_lock = mutating.index("acquire_release_lock")
        stage = mutating.index("run_stage", acquire_lock)
        promote = mutating.index("run_switch promote", acquire_lock)
        rollback = mutating.index("run_switch rollback", acquire_lock)
        self.assertLess(safety_gate, acquire_lock)
        self.assertLess(acquire_lock, stage)
        self.assertLess(acquire_lock, promote)
        self.assertLess(acquire_lock, rollback)

        dispatch = runner[dispatch_start:]
        self.assertIn("status) run_status ;;", dispatch)
        self.assertIn("stage|promote|rollback) run_mutating_operation ;;", dispatch)
        self.assertEqual(runner.count('exec 9>"$release_lock"'), 1)
        self.assertNotIn("acquire_release_lock", dispatch.split("status) run_status ;;", 1)[0])
        self.assertIn(
            'install -o root -g root -m 0755 "$fresh_repo/App/ops/project-snow-release"',
            bootstrap,
        )
        self.assertIn("one bounded self-upgrade", deployment_guide)
        self.assertIn("no root console, SSH configuration change or broader sudo rule", deployment_guide)

    def test_stage_self_upgrade_is_exact_atomic_and_keeps_the_sudo_contract(self) -> None:
        deploy = self.read("ops/deploy.sh")
        runner = self.read("ops/project-snow-release")
        local_deploy = self.read("scripts/deploy.ps1")
        manifest_generator = self.read("scripts/release_manifest.py")
        sudoers = self.read("ops/project-snow-release.sudoers")

        for control_path in (
            "ops/project-snow-release",
            "ops/project-snow-release.sudoers",
        ):
            self.assertIn(control_path, deploy)
            self.assertIn(control_path, runner)
            self.assertIn(control_path, local_deploy)
            self.assertIn(control_path, manifest_generator)
        self.assertIn("release_control_sha256", deploy)
        self.assertIn("release_control_sha256", runner)
        self.assertIn("release_control_sha256", local_deploy)
        self.assertIn("RELEASE_CONTROL_PATHS", manifest_generator)

        control_gate = deploy.index("validate_release_control_contract || exit 78")
        first_staging_write = deploy.index('install -d -m 0700 "$colour_env_root"')
        self.assertLess(control_gate, first_staging_write)
        self.assertIn("git -C \"$release_repository\" ls-tree", deploy)
        self.assertIn("git -C \"$release_repository\" cat-file blob", deploy)
        self.assertIn("git -C \"$release_repository\" hash-object", deploy)
        self.assertIn("mktemp /usr/local/sbin/.project-snow-release.new.", deploy)
        self.assertIn('/bin/sh -n "$runner_upgrade_new"', deploy)
        self.assertIn('stat -c %u:%g:%a:%h "$release_runner_path"', deploy)
        self.assertIn('mv -f -- "$runner_upgrade_new" "$release_runner_path"', deploy)
        install_start = deploy.index("install_verified_release_runner() {")
        install_end = deploy.index(
            "\n}\n\nrestore_release_runner_if_pending()", install_start
        )
        install_body = deploy[install_start:install_end]
        for explicit_failure in (
            'expected_runner_sha="$(jq -r',
            'runner_upgrade_new="$(mktemp /usr/local/sbin/.project-snow-release.new.',
            '> "$runner_upgrade_new" || return 1',
            'chown root:root "$runner_upgrade_new" || return 1',
            'chmod 0755 "$runner_upgrade_new" || return 1',
            'prepared_runner_sha="$(sha256sum',
            'prepared_runner_blob="$(git -C "$release_repository" hash-object',
            'fsync_release_path "$runner_upgrade_new" || return 1',
            'runner_upgrade_backup="$(mktemp /usr/local/sbin/.project-snow-release.rollback.',
            'install -o root -g root -m 0755 "$release_runner_path" "$runner_upgrade_backup" || return 1',
            'fsync_release_path "$runner_upgrade_backup" || return 1',
            'mv -f -- "$runner_upgrade_new" "$release_runner_path" || return 1',
            'fsync_release_path /usr/local/sbin || return 1',
            'verify_new_release_runner_status "$expected_runner_sha" || return 1',
        ):
            self.assertIn(explicit_failure, install_body)
        state_set = deploy.index("runner_upgrade_installed=1", deploy.index("install_verified_release_runner()"))
        atomic_replace = deploy.index('mv -f -- "$runner_upgrade_new" "$release_runner_path"')
        self.assertLess(state_set, atomic_replace)
        self.assertIn("fsync_release_path /usr/local/sbin", deploy)
        self.assertIn("restore_release_runner_if_pending", deploy)
        self.assertIn('mv -f -- "$runner_upgrade_backup" "$release_runner_path"', deploy)
        self.assertIn("release_runner_commit_is_durable", deploy)
        self.assertIn('stat -c %u:%g:%a:%h "$colour_marker"', deploy)
        self.assertIn('installed_runner_sha" = "$target_runner_sha', deploy)
        durable_start = deploy.index("release_runner_commit_is_durable() {")
        durable_end = deploy.index("\n}\n\nrestore_stage_signal_traps()", durable_start)
        durable_body = deploy[durable_start:durable_end]
        self.assertIn('[ "$runner_upgrade_marker_synced" -eq 1 ]', durable_body)
        self.assertIn('= "$runner_upgrade_marker_identity"', durable_body)

        firewall_gate = deploy.rindex('install_direct_origin_firewall "$candidate_config_root"')
        upgrade = deploy.rindex("install_verified_release_runner || {")
        marker = deploy.rindex("commit_colour_marker || exit 78")
        self.assertLess(firewall_gate, upgrade)
        self.assertLess(upgrade, marker)
        marker_start = deploy.index("commit_colour_marker() {")
        marker_end = deploy.index("\n}\n\nconfiguration_paths_for_root()", marker_start)
        marker_body = deploy[marker_start:marker_end]
        marker_rename = marker_body.index('mv -f -- "$candidate_marker" "$colour_marker"')
        marker_fsync = marker_body.index('fsync_release_path "$colour_release_root"')
        receipt_rename = marker_body.index('mv -f -- "$candidate_receipt" "$colour_receipt"')
        receipt_fsync = marker_body.index(
            'fsync_release_path "$colour_release_root"', receipt_rename
        )
        marker_synced = marker_body.index("runner_upgrade_marker_synced=1")
        marker_committed = marker_body.index("runner_upgrade_committed=1")
        self.assertLess(marker_rename, marker_fsync)
        self.assertLess(marker_fsync, receipt_rename)
        self.assertLess(receipt_rename, receipt_fsync)
        self.assertLess(receipt_fsync, marker_synced)
        self.assertLess(marker_synced, marker_committed)
        self.assertIn("trap '' HUP INT QUIT TERM PIPE || return 1", marker_body)
        self.assertIn("trap 'exit 131' QUIT || return 1", deploy)
        self.assertIn("trap 'exit 141' PIPE || return 1", deploy)
        self.assertIn('mv -f -- "$previous_colour_marker_backup" "$colour_marker"', deploy)
        self.assertIn('mv -f -- "$previous_colour_receipt_backup" "$colour_receipt"', deploy)
        self.assertIn("failed to durably roll back an unsynced colour marker", marker_body)
        for candidate_sync in (
            'fsync_release_path "$candidate_public_env" || exit 78',
            'fsync_release_path "$candidate_marker" || exit 78',
            'fsync_release_path "$candidate_receipt" || exit 78',
            'fsync_release_path "$candidate_manifest" || exit 78',
            'fsync_release_path "$candidate_env" || exit 78',
            'fsync_release_path "$candidate_config_binding_tmp" || exit 78',
            'fsync_release_path "$colour_env_root" || exit 78',
            'fsync_release_path "$colour_release_root" || exit 78',
        ):
            self.assertIn(candidate_sync, deploy)
        self.assertIn("verify_new_release_runner_status", deploy)
        self.assertIn(".release_runner_sha256 == $runner_sha", deploy)
        self.assertIn(".runner_controller_binding == true", deploy)
        self.assertIn(".host_safety_preflight == true", deploy)

        self.assertIn("PROJECT_SNOW_RELEASE_PREVIOUS_CONTROLLER_SHA", runner)
        self.assertIn("PROJECT_SNOW_RELEASE_PREVIOUS_RUNNER_SHA256", runner)
        self.assertIn("PROJECT_SNOW_RELEASE_PREVIOUS_CONTROLLER_SHA", deploy)
        self.assertIn("c4796bbdda5557666afaaeb708ed34864456acc2", deploy)
        self.assertIn("/proc/[0-9]*/status", deploy)
        self.assertIn("/var/run/docker.sock", deploy)
        self.assertIn('visudo -cf "$release_sudoers_path"', deploy)
        self.assertNotIn('mv -f -- "$release_sudoers_path"', deploy)
        self.assertNotIn('install "$release_sudoers_path"', deploy)

        rules = [
            line
            for line in sudoers.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(rules), 3)
        self.assertEqual(
            rules[-1],
            "deploy ALL=(root) NOPASSWD: /usr/local/sbin/project-snow-release",
        )
        self.assertNotIn("docker", "\n".join(rules).casefold())
        self.assertNotIn("/bin/sh", "\n".join(rules))

    def test_runner_upgrade_acknowledges_only_the_exact_durable_attempt(self) -> None:
        deploy = self.read("ops/deploy.sh")
        cleanup_start = deploy.index("cleanup() {", deploy.index('candidate_env="$(mktemp'))
        cleanup_end = deploy.index("\n}\ntrap cleanup EXIT", cleanup_start) + 2
        cleanup_function = deploy[cleanup_start:cleanup_end]
        harness = f"""\
set -u
durable_case=$1
failure_case=$2
release_runner_commit_is_durable() {{ [ "$durable_case" = yes ]; }}
restore_release_runner_if_pending() {{ return 0; }}
runner_upgrade_committed=0
runner_upgrade_new=
runner_upgrade_backup=
candidate_env=
candidate_manifest=
candidate_marker=
candidate_receipt=
candidate_config_binding_tmp=
candidate_config_tmp=
candidate_public_env=
previous_colour_marker_backup=
previous_colour_receipt_backup=
stage_publication_recovery_failed=0
{cleanup_function}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 131' QUIT
trap 'exit 143' TERM
trap 'exit 141' PIPE
case "$failure_case" in
  hup) kill -HUP "$$"; exit 99 ;;
  quit) kill -QUIT "$$"; exit 99 ;;
  term) kill -TERM "$$"; exit 99 ;;
  pipe) kill -PIPE "$$"; exit 99 ;;
  *) exit 98 ;;
esac
"""
        durable = self.run_posix_shell(harness, "yes", "term")
        self.assertEqual(durable.returncode, 0, durable.stderr)
        not_durable = self.run_posix_shell(harness, "no", "term")
        self.assertEqual(not_durable.returncode, 143, not_durable.stderr)
        durable_hup = self.run_posix_shell(harness, "yes", "hup")
        self.assertEqual(durable_hup.returncode, 0, durable_hup.stderr)
        not_durable_hup = self.run_posix_shell(harness, "no", "hup")
        self.assertEqual(not_durable_hup.returncode, 129, not_durable_hup.stderr)
        durable_quit = self.run_posix_shell(harness, "yes", "quit")
        self.assertEqual(durable_quit.returncode, 0, durable_quit.stderr)
        not_durable_quit = self.run_posix_shell(harness, "no", "quit")
        self.assertEqual(not_durable_quit.returncode, 131, not_durable_quit.stderr)
        durable_pipe = self.run_posix_shell(harness, "yes", "pipe")
        self.assertEqual(durable_pipe.returncode, 0, durable_pipe.stderr)
        not_durable_pipe = self.run_posix_shell(harness, "no", "pipe")
        self.assertEqual(not_durable_pipe.returncode, 141, not_durable_pipe.stderr)

    def test_unsynced_colour_marker_is_rolled_back_before_runner_failure(self) -> None:
        deploy = self.read("ops/deploy.sh")
        functions_start = deploy.index("restore_stage_signal_traps() {")
        functions_end = deploy.index("\n}\n\nconfiguration_paths_for_root()", functions_start) + 2
        marker_functions = deploy[functions_start:functions_end]
        harness = f"""\
set -u
test_root=$1
mkdir -p "$test_root"
colour_release_root="$test_root"
colour_marker="$test_root/blue"
candidate_marker="$test_root/blue.candidate"
previous_colour_marker_backup="$test_root/blue.rollback"
previous_colour_receipt_backup=
release_attempt_nonce=
candidate_receipt=
colour_receipt="$test_root/blue-stage-receipt"
receipt_published=0
stage_publication_recovery_failed=0
printf '%s\n' old > "$colour_marker"
printf '%s\n' old > "$previous_colour_marker_backup"
printf '%s\n' new > "$candidate_marker"
runner_upgrade_marker_synced=0
runner_upgrade_committed=0
sync_calls=0
fsync_release_path() {{
  sync_calls=$((sync_calls + 1))
  [ "$sync_calls" -gt 1 ]
}}
{marker_functions}
if commit_colour_marker; then
  exit 90
fi
[ "$(cat "$colour_marker")" = old ]
[ "$runner_upgrade_marker_synced" -eq 0 ]
[ "$runner_upgrade_committed" -eq 0 ]
[ -z "$previous_colour_marker_backup" ]
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            result = self.run_posix_shell(harness, Path(temporary_root).as_posix())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsynced_attempt_receipt_restores_both_prior_records(self) -> None:
        deploy = self.read("ops/deploy.sh")
        functions_start = deploy.index("restore_stage_signal_traps() {")
        functions_end = deploy.index("\n}\n\nconfiguration_paths_for_root()", functions_start) + 2
        marker_functions = deploy[functions_start:functions_end]
        nonce = "7" * 64
        harness = f"""\
set -u
test_root=$1
mkdir -p "$test_root"
colour_release_root="$test_root"
colour_marker="$test_root/blue"
colour_receipt="$test_root/blue-stage-receipt"
candidate_marker="$test_root/blue.candidate"
candidate_receipt="$test_root/blue-stage-receipt.candidate"
previous_colour_marker_backup="$test_root/blue.rollback"
previous_colour_receipt_backup="$test_root/blue-stage-receipt.rollback"
printf '%s\n' old-marker > "$colour_marker"
printf '%s\n' old-receipt > "$colour_receipt"
printf '%s\n' old-marker > "$previous_colour_marker_backup"
printf '%s\n' old-receipt > "$previous_colour_receipt_backup"
printf '%s\n' new-marker > "$candidate_marker"
printf '%s\n' new-receipt > "$candidate_receipt"
release_attempt_nonce={nonce}
runner_upgrade_marker_synced=0
runner_upgrade_receipt_synced=0
runner_upgrade_committed=0
stage_publication_recovery_failed=0
receipt_published=0
sync_calls=0
fsync_release_path() {{
  sync_calls=$((sync_calls + 1))
  [ "$sync_calls" -ne 2 ]
}}
{marker_functions}
if commit_colour_marker; then
  exit 90
fi
[ "$(cat "$colour_marker")" = old-marker ]
[ "$(cat "$colour_receipt")" = old-receipt ]
[ "$runner_upgrade_marker_synced" -eq 0 ]
[ "$runner_upgrade_receipt_synced" -eq 0 ]
[ "$runner_upgrade_committed" -eq 0 ]
[ -z "$previous_colour_marker_backup" ]
[ -z "$previous_colour_receipt_backup" ]
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            result = self.run_posix_shell(harness, Path(temporary_root).as_posix())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_marker_recovery_preserves_the_only_root_backup(self) -> None:
        deploy = self.read("ops/deploy.sh")
        functions_start = deploy.index("restore_stage_signal_traps() {")
        functions_end = deploy.index("\n}\n\nconfiguration_paths_for_root()", functions_start) + 2
        marker_functions = deploy[functions_start:functions_end]
        cleanup_start = deploy.index("cleanup() {", deploy.index('candidate_env="$(mktemp'))
        cleanup_end = deploy.index("\n}\ntrap cleanup EXIT", cleanup_start) + 2
        cleanup_function = deploy[cleanup_start:cleanup_end]
        harness = f"""\
set -u
test_root=$1
mkdir -p "$test_root"
colour_release_root="$test_root"
colour_marker="$test_root/blue"
colour_receipt="$test_root/blue-stage-receipt"
candidate_marker="$test_root/blue.candidate"
candidate_receipt=
previous_colour_marker_backup="$test_root/blue.rollback"
previous_colour_receipt_backup=
printf '%s\n' old > "$previous_colour_marker_backup"
printf '%s\n' new > "$candidate_marker"
release_attempt_nonce=
runner_upgrade_marker_synced=0
runner_upgrade_receipt_synced=0
runner_upgrade_committed=0
stage_publication_recovery_failed=0
receipt_published=0
mv_calls=0
mv() {{
  mv_calls=$((mv_calls + 1))
  if [ "$mv_calls" -eq 1 ]; then
    command mv "$@"
  else
    return 1
  fi
}}
fsync_release_path() {{ return 1; }}
{marker_functions}
if commit_colour_marker; then
  exit 90
fi
[ "$stage_publication_recovery_failed" -eq 1 ]
[ -f "$previous_colour_marker_backup" ]
release_runner_commit_is_durable() {{ return 1; }}
restore_release_runner_if_pending() {{ return 0; }}
runner_upgrade_new=
runner_upgrade_backup=
candidate_env=
candidate_manifest=
candidate_config_binding_tmp=
candidate_config_tmp=
candidate_public_env=
{cleanup_function}
trap cleanup EXIT
exit 78
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            backup = Path(temporary_root) / "blue.rollback"
            result = self.run_posix_shell(harness, Path(temporary_root).as_posix())
            backup_preserved = backup.is_file()
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertTrue(backup_preserved)

    def test_parent_nonzero_fallback_requires_this_attempts_durable_receipt(self) -> None:
        runner = self.read("ops/project-snow-release")
        durable_start = runner.index("stage_commit_is_durable() {")
        durable_end = runner.index("\n}\n\ncopy_inbox_manifest()", durable_start) + 2
        durable_function = runner[durable_start:durable_end]
        old_nonce = "1" * 64
        attempt_nonce = "2" * 64
        target_runner_sha = "3" * 64
        target_sha = "4" * 40
        public_image = f"registry.invalid/public@sha256:{'5' * 64}"
        embedding_image = f"registry.invalid/embedding@sha256:{'6' * 64}"
        harness = f"""\
set -u
test_root=$1
mkdir -p "$test_root"
colour_root="$test_root"
repo="$test_root/repo"
runner_path="$test_root/runner"
manifest="$test_root/manifest.json"
printf '%s\n' runner > "$runner_path"
printf '%s\n' manifest > "$manifest"
printf '%s\n' "blue {target_sha} {public_image} {embedding_image}" > "$colour_root/blue"
printf '%s\n' "project-snow-stage-receipt-1 {old_nonce} blue {target_sha} {public_image} {embedding_image}" > "$colour_root/blue-stage-receipt"
fsync_release_path() {{ return 0; }}
jq() {{ printf '%s\n' {target_runner_sha}; }}
git() {{ printf '%s\n' {target_sha}; }}
stat() {{
  if [ "$3" = "$runner_path" ]; then
    printf '%s\n' 0:0:755:1
  else
    printf '%s\n' 0:0:600:1
  fi
}}
sha256sum() {{ printf '%s  %s\n' {target_runner_sha} "$1"; }}
stage_configuration_binding_is_durable() {{ return 0; }}
{durable_function}
if stage_commit_is_durable "$manifest" blue {target_sha} \
  {public_image} {embedding_image} {attempt_nonce}; then
  exit 90
fi
printf '%s\n' "project-snow-stage-receipt-1 {attempt_nonce} blue {target_sha} {public_image} {embedding_image}" > "$colour_root/blue-stage-receipt"
stage_commit_is_durable "$manifest" blue {target_sha} \
  {public_image} {embedding_image} {attempt_nonce}
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            result = self.run_posix_shell(harness, Path(temporary_root).as_posix())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_runner_term_waits_for_child_and_restores_only_non_durable_stage(self) -> None:
        runner = self.read("ops/project-snow-release")
        control_start = runner.index('cleanup_paths=""')
        control_end = runner.index("\nrequire_root_controlled_repo()", control_start)
        signal_control = runner[control_start:control_end]
        run_stage_start = runner.index("run_stage() {")
        run_stage_end = runner.index("\n}\n\nrun_switch()", run_stage_start)
        run_stage = runner[run_stage_start:run_stage_end]
        self.assertIn("stage_signal_child_expected=1", run_stage)
        self.assertIn("stage_signal_child_pid=$!", run_stage)
        self.assertIn('wait "$stage_signal_child_pid" || stage_child_status=$?', run_stage)
        self.assertIn('exec /usr/bin/env -i', run_stage)
        self.assertIn('stage_signal_restore_controller="$previous_runner_controller"', run_stage)
        self.assertIn("trap 'terminate_release_signal 143 TERM' TERM", signal_control)
        self.assertIn("trap 'terminate_release_signal 131 QUIT' QUIT", signal_control)
        self.assertIn("trap 'terminate_release_signal 141 PIPE' PIPE", signal_control)

        harness = f"""\
set -u
durable_case=$1
test_root=$2
mkdir -p "$test_root"
manifest="$test_root/verified-manifest.json"
checked="$test_root/checked-before-cleanup"
restored="$test_root/restored-controller"
printf '%s\n' verified > "$manifest"
stage_commit_is_durable() {{
  [ -f "$1" ] || {{ printf '%s\n' deleted > "$checked"; return 1; }}
  printf '%s\n' present > "$checked"
  [ "$durable_case" = yes ]
}}
checkout_controller() {{ printf '%s\n' "$1" > "$restored"; }}
{signal_control}
cleanup_paths="$manifest"
stage_signal_manifest="$manifest"
stage_signal_colour=blue
stage_signal_sha={'a' * 40}
stage_signal_public=public
stage_signal_embedding=embedding
stage_signal_nonce={'b' * 64}
stage_signal_can_ack=1
stage_signal_restore_controller={'c' * 40}
stage_signal_child_expected=1
parent_pid=$$
(
  trap 'exit 143' TERM
  while :; do sleep 1; done
) &
stage_signal_child_pid=$!
( sleep 0.2; kill -TERM "$parent_pid" ) &
wait "$stage_signal_child_pid" || true
exit 99
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            non_durable = self.run_posix_shell(harness, "no", root.as_posix())
            non_durable_checked = (root / "checked-before-cleanup").read_text().strip()
            restored_controller = (root / "restored-controller").read_text().strip()
            manifest_removed = not (root / "verified-manifest.json").exists()
        self.assertEqual(non_durable.returncode, 143, non_durable.stderr)
        self.assertEqual(non_durable_checked, "present")
        self.assertEqual(restored_controller, "c" * 40)
        self.assertTrue(manifest_removed)

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            durable = self.run_posix_shell(harness, "yes", root.as_posix())
            durable_checked = (root / "checked-before-cleanup").read_text().strip()
            restored_exists = (root / "restored-controller").exists()
            manifest_removed = not (root / "verified-manifest.json").exists()
        self.assertEqual(durable.returncode, 0, durable.stderr)
        self.assertEqual(durable_checked, "present")
        self.assertFalse(restored_exists)
        self.assertTrue(manifest_removed)

    def test_release_runner_term_forwards_to_switch_and_requires_durable_ack(self) -> None:
        runner = self.read("ops/project-snow-release")
        control_start = runner.index('cleanup_paths=""')
        control_end = runner.index("\nrequire_root_controlled_repo()", control_start)
        signal_control = runner[control_start:control_end]
        run_switch_start = runner.index("run_switch() {")
        run_switch_end = runner.index("\n}\n\nverified_version_array()", run_switch_start)
        run_switch = runner[run_switch_start:run_switch_end]
        durable_start = runner.index("switch_commit_matches_expected() {")
        durable_end = runner.index("\n}\n\nstage_commit_is_durable()", durable_start)
        durable = runner[durable_start:durable_end]
        self.assertIn("release_signal_child_operation=switch", run_switch)
        self.assertIn("stage_signal_child_expected=1", run_switch)
        self.assertIn("stage_signal_child_pid=$!", run_switch)
        self.assertIn('wait "$stage_signal_child_pid" || switch_child_status=$?', run_switch)
        self.assertIn("switch_commit_is_durable || return 74", run_switch)
        self.assertIn("fsync_release_path /srv/project-snow/releases || return 1", durable)
        self.assertIn("/srv/project-snow/runtime/compose.env", durable)
        self.assertIn("/srv/project-snow/releases/current-manifest.json", durable)
        self.assertIn("/srv/project-snow/releases/current-config.json", durable)
        self.assertIn('runner_matches_controller "$switch_signal_controller_sha"', durable)
        self.assertIn("live_origin_edge_binding_matches_expected", durable)
        self.assertIn('fsync_release_path "$durable_live_binding" || return 1', durable)
        self.assertIn('fsync_release_path "$durable_live_env" || return 1', durable)
        self.assertIn("fsync_release_path /srv/project-snow/runtime/origin-edge || return 1", durable)

        harness = f"""\
set -u
durable_case=$1
test_root=$2
ack_log="$test_root/ack-checked"
stage_commit_is_durable() {{ return 1; }}
checkout_controller() {{ return 1; }}
switch_commit_is_durable() {{
  printf '%s\n' checked > "$ack_log"
  [ "$durable_case" = yes ]
}}
{signal_control}
release_signal_child_operation=switch
switch_signal_can_ack=1
stage_signal_child_expected=1
parent_pid=$$
(
  if [ "$durable_case" = yes ]; then
    trap 'exit 0' TERM
  else
    trap 'exit 143' TERM
  fi
  while :; do sleep 1; done
) &
stage_signal_child_pid=$!
( sleep 0.2; kill -TERM "$parent_pid" ) &
wait "$stage_signal_child_pid" || true
exit 99
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            non_durable = self.run_posix_shell(harness, "no", root.as_posix())
            non_durable_checked = (root / "ack-checked").is_file()
        self.assertEqual(non_durable.returncode, 143, non_durable.stderr)
        self.assertTrue(non_durable_checked)

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            durable_result = self.run_posix_shell(harness, "yes", root.as_posix())
            durable_checked = (root / "ack-checked").is_file()
        self.assertEqual(durable_result.returncode, 0, durable_result.stderr)
        self.assertTrue(durable_checked)

    def test_future_runner_retry_preserves_current_or_resolves_exact_predecessor(self) -> None:
        runner = self.read("ops/project-snow-release")
        resolver_start = runner.index("resolve_installed_runner_controller() {")
        resolver_end = runner.index("\n}\n\nstage_commit_is_durable()", resolver_start) + 2
        resolver = runner[resolver_start:resolver_end]
        self.assertIn('rev-list --first-parent "$runner_target_sha"', resolver)
        self.assertNotIn("-- App/ops/project-snow-release", resolver)

        git_candidates = (
            shutil.which("git"),
            "C:/Program Files/Git/cmd/git.exe",
        )
        git = next((Path(candidate) for candidate in git_candidates if candidate and Path(candidate).is_file()), None)
        self.assertIsNotNone(git, "Git is required for runner ancestry contract tests.")

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            repository = root / "repo"
            installed_runner = root / "installed-project-snow-release"
            repository.mkdir()

            def git_run(*arguments: str) -> str:
                completed = subprocess.run(
                    [str(git), "-C", str(repository), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout.strip()

            subprocess.run([str(git), "init", str(repository)], check=True, capture_output=True)
            git_run("config", "user.email", "contracts@example.invalid")
            git_run("config", "user.name", "Deployment Contracts")
            git_run("config", "core.autocrlf", "false")
            runner_path = repository / "App" / "ops" / "project-snow-release"
            runner_path.parent.mkdir(parents=True)
            predecessor_payload = b"#!/bin/sh\necho runner-n\n"
            runner_path.write_bytes(predecessor_payload)
            git_run("add", "App/ops/project-snow-release")
            git_run("update-index", "--chmod=+x", "App/ops/project-snow-release")
            git_run("commit", "-m", "runner n")
            original_controller = git_run("rev-parse", "HEAD")
            (repository / "README.md").write_text("intermediate\n", encoding="utf-8")
            git_run("add", "README.md")
            git_run("commit", "-m", "intermediate controller")
            nearest_predecessor = git_run("rev-parse", "HEAD")
            target_payload = b"#!/bin/sh\necho runner-n-plus-one\n"
            runner_path.write_bytes(target_payload)
            git_run("add", "App/ops/project-snow-release")
            git_run("commit", "-m", "runner n plus one")
            target = git_run("rev-parse", "HEAD")
            installed_runner.write_bytes(predecessor_payload)
            installed_sha = hashlib.sha256(predecessor_payload).hexdigest()

            harness = f"""\
set -eu
repo=$1
runner_path=$2
stat() {{
  if [ "$#" -eq 3 ] && [ "$1" = -c ] && [ "$2" = %u:%g:%a:%h ] && [ "$3" = "$runner_path" ]; then
    printf '%s\n' 0:0:755:1
  else
    command stat "$@"
  fi
}}
{resolver}
resolve_installed_runner_controller "$3" "$4" "$5"
"""
            normal = self.run_posix_shell(
                harness,
                repository.as_posix(),
                installed_runner.as_posix(),
                target,
                installed_sha,
                original_controller,
            )
            interrupted_retry = self.run_posix_shell(
                harness,
                repository.as_posix(),
                installed_runner.as_posix(),
                target,
                installed_sha,
                target,
            )
            installed_runner.write_bytes(target_payload)
            installed_target_sha = hashlib.sha256(target_payload).hexdigest()
            runner_advanced_before_controller_restore = self.run_posix_shell(
                harness,
                repository.as_posix(),
                installed_runner.as_posix(),
                target,
                installed_target_sha,
                original_controller,
            )
        self.assertEqual(normal.returncode, 0, normal.stderr)
        self.assertEqual(normal.stdout.strip(), original_controller)
        self.assertEqual(interrupted_retry.returncode, 0, interrupted_retry.stderr)
        self.assertEqual(interrupted_retry.stdout.strip(), nearest_predecessor)
        self.assertEqual(
            runner_advanced_before_controller_restore.returncode,
            0,
            runner_advanced_before_controller_restore.stderr,
        )
        self.assertEqual(runner_advanced_before_controller_restore.stdout.strip(), target)

        run_stage_start = runner.index("run_stage() {")
        run_stage_end = runner.index("\n}\n\nrun_switch()", run_stage_start)
        run_stage = runner[run_stage_start:run_stage_end]
        resolver_call = run_stage.index("resolve_installed_runner_controller")
        target_checkout = run_stage.index('checkout_controller "$requested_sha"')
        self.assertLess(resolver_call, target_checkout)
        self.assertIn(
            'PROJECT_SNOW_RELEASE_PREVIOUS_CONTROLLER_SHA="$previous_runner_controller"',
            run_stage,
        )
        self.assertIn('PROJECT_SNOW_RELEASE_ATTEMPT_NONCE="$stage_attempt_nonce"', run_stage)
        self.assertNotIn('checkout_controller "$previous_controller"', run_stage)
        self.assertIn('checkout_controller "$previous_runner_controller"', run_stage)
        durable_gate = run_stage.index("if stage_commit_is_durable")
        failure_restore = run_stage.index(
            'checkout_controller "$previous_runner_controller"', durable_gate
        )
        self.assertLess(durable_gate, failure_restore)
        durable_start = runner.index("stage_commit_is_durable() {")
        durable_end = runner.index("\n}\n\ncopy_inbox_manifest()", durable_start)
        durable_body = runner[durable_start:durable_end]
        self.assertIn('fsync_release_path "$colour_root" || return 1', durable_body)

        binding_start = runner.index("runner_matches_controller() {")
        binding_end = runner.index("\n}\n\nstage_commit_is_durable()", binding_start)
        binding_body = runner[binding_start:binding_end]
        self.assertIn('hash-object --no-filters "$runner_path"', binding_body)
        self.assertIn('$bound_controller_sha:App/ops/project-snow-release', binding_body)
        run_switch_start = runner.index("run_switch() {")
        run_switch_end = runner.index("\n}\n\nverified_version_array()", run_switch_start)
        run_switch = runner[run_switch_start:run_switch_end]
        binding_gate = run_switch.index('runner_matches_controller "$controller_sha"')
        traffic_switch = run_switch.index("./ops/promote.sh")
        self.assertLess(binding_gate, traffic_switch)
        self.assertIn("repeat exact stage before switching traffic", run_switch)
        self.assertIn("runner_controller_binding", runner)

    def test_promote_fails_closed_when_runner_and_checkout_are_not_bound(self) -> None:
        promote = self.read("ops/promote.sh")
        gate_start = promote.index("require_runner_controller_binding() {")
        gate_end = promote.index("\n}\n\nrequire_runner_controller_binding ||", gate_start) + 2
        gate = promote[gate_start:gate_end]
        gate_call = promote.index("require_runner_controller_binding ||", gate_end)
        first_policy_read = promote.index("access_policy_file=", gate_call)
        first_docker_call = promote.index("docker compose", gate_call)

        self.assertLess(gate_call, first_policy_read)
        self.assertLess(gate_call, first_docker_call)
        self.assertIn('stat -c %u:%g:%a:%h "$release_runner_path"', gate)
        self.assertIn('[ "$release_runner_metadata" = 0:0:755:1 ] || return 1', gate)
        self.assertIn('git -C "$release_repository" rev-parse --verify HEAD', gate)
        self.assertIn('git -C "$release_repository" ls-tree', gate)
        self.assertIn('[ "$1" = 100755 ]', gate)
        self.assertIn('[ "$2" = blob ]', gate)
        self.assertIn('hash-object --no-filters', gate)
        self.assertIn(
            '[ "$release_installed_runner_blob" = "$release_controller_runner_blob" ] || return 1',
            gate,
        )
        self.assertIn("repeat exact stage before switching traffic", promote[gate_call:first_policy_read])

        git_candidates = (
            shutil.which("git"),
            "C:/Program Files/Git/cmd/git.exe",
        )
        git = next(
            (Path(candidate) for candidate in git_candidates if candidate and Path(candidate).is_file()),
            None,
        )
        self.assertIsNotNone(git, "Git is required for promote binding contract tests.")

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            repository = root / "repo"
            installed_runner = root / "installed-project-snow-release"
            repository.mkdir()

            def git_run(*arguments: str) -> str:
                completed = subprocess.run(
                    [str(git), "-C", str(repository), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout.strip()

            subprocess.run([str(git), "init", str(repository)], check=True, capture_output=True)
            git_run("config", "user.email", "contracts@example.invalid")
            git_run("config", "user.name", "Deployment Contracts")
            git_run("config", "core.autocrlf", "false")
            tracked_runner = repository / "App" / "ops" / "project-snow-release"
            tracked_runner.parent.mkdir(parents=True)
            runner_payload = b"#!/bin/sh\necho bound-runner\n"
            tracked_runner.write_bytes(runner_payload)
            git_run("add", "App/ops/project-snow-release")
            git_run("update-index", "--chmod=+x", "App/ops/project-snow-release")
            git_run("commit", "-m", "bound runner")
            installed_runner.write_bytes(runner_payload)

            harness = f"""\
set -u
release_repository=$1
release_runner_path=$2
stat() {{
  if [ "$#" -ge 3 ] && [ "$1" = -c ] && [ "$2" = %u:%g:%a:%h ] && [ "$3" = "$release_runner_path" ]; then
    printf '%s\n' 0:0:755:1
  else
    command stat "$@"
  fi
}}
{gate}
require_runner_controller_binding
"""
            matching = self.run_posix_shell(
                harness,
                repository.as_posix(),
                installed_runner.as_posix(),
            )
            installed_runner.write_bytes(b"#!/bin/sh\necho stale-runner\n")
            mismatched = self.run_posix_shell(
                harness,
                repository.as_posix(),
                installed_runner.as_posix(),
            )

        self.assertEqual(matching.returncode, 0, matching.stderr)
        self.assertNotEqual(mismatched.returncode, 0)

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
        self.assertIn("'core.autocrlf=false'", script)
        self.assertIn("'core.eol=lf'", script)
        self.assertIn("$hardenedSshBase", script)
        self.assertIn("'-p', '43556'", script)
        self.assertIn("& ssh @hardenedSshBase $verifyCommand", script)
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
        self.assertIn('"App/ops/project-snow-release"', host_bootstrap)
        self.assertIn("'App/ops/project-snow-release'", script)
        self.assertIn("host preparation bundle does not match the exact Git archive", host_bootstrap)
        self.assertIn("host preparation executable must use LF line endings", host_bootstrap)
        self.assertIn("project-snow-prepare-", host_bootstrap)
        self.assertIn('"--no-block", "start"', host_bootstrap)

    def test_deploy_verifies_release_configuration_hashes_before_staging(self) -> None:
        local_script = self.read("scripts/deploy.ps1")
        remote_script = self.read("ops/deploy.sh")
        for relative_path in (
            "compose.prod.yml",
            "infra/Caddyfile",
            "infra/OriginEdge.Caddyfile",
            "scripts/cloudflare_origin_firewall.py",
            "ops/project-snow-origin-firewall.service",
            "ops/project-snow-origin-firewall.timer",
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
        self.assertIn('mv -f -- "$candidate_public_env" "$public_env_path"', script)

    def test_candidate_public_env_migrates_legacy_settings_fail_closed(self) -> None:
        script = self.read("ops/deploy.sh")
        self.assertIn("build_candidate_public_env", script)
        self.assertIn(
            "Public environment must be root-owned, mode 0600 and have one link.",
            script,
        )
        self.assertIn("Public environment contains a duplicate key.", script)
        self.assertIn("Public environment contains a disallowed key.", script)
        self.assertIn("PUBLIC_EXPERIENCE_NOTICE_VERSION=0.9.2", script)
        self.assertIn("PUBLIC_PRIVACY_POLICY_VERSION=0.9.2", script)
        self.assertIn("PUBLIC_PRIVACY_EFFECTIVE_AT=2026-08-20", script)
        self.assertIn("PUBLIC_TURNSTILE_HOSTNAME=snow.xiaob.dev", script)
        self.assertIn("PUBLIC_TURNSTILE_MAX_AGE_SECONDS=300", script)
        self.assertIn("PUBLIC_MAX_PROVIDER_CALLS_PER_ACTION=2", script)
        self.assertIn("PUBLIC_BYOK_LIFETIME_HOURS=12", script)
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

    def test_ci_and_local_validation_include_direct_origin_assets(self) -> None:
        workflow = (self.app_root.parent / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        validate_all = self.read("scripts/validate_all.ps1")
        self.assertIn("ORIGIN_TLS_ROOT: /etc/project-snow/origin-edge/releases/", workflow)
        self.assertIn("App/scripts/install_origin_tls.py", workflow)
        self.assertIn("App/scripts/cloudflare_origin_firewall.py", workflow)
        self.assertIn("App/tests/test_install_origin_tls.py", workflow)
        self.assertIn("App/tests/test_origin_firewall.py", workflow)
        self.assertIn("$originTlsRootWasSet = Test-Path Env:ORIGIN_TLS_ROOT", validate_all)
        self.assertIn("Remove-Item Env:ORIGIN_TLS_ROOT", validate_all)

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
        runner = self.read("ops/project-snow-release")
        promote = self.read("ops/promote.sh")
        for script in (deploy, promote):
            self.assertIn("project-snow-config-snapshot-1", script)
            self.assertIn("/srv/project-snow/releases/configurations", script)
            self.assertIn("configuration_sha256", script)
            self.assertIn("configuration_paths_for_root()", script)
            self.assertIn('case "$direct_origin_path_count" in', script)
            self.assertIn("    0) ;;", script)
            self.assertIn("legacy_direct_origin_configuration_paths=", script)
            self.assertIn(
                'printf \'%s\\n\' "$legacy_direct_origin_configuration_paths"',
                script,
            )
            self.assertIn(
                "    7) printf '%s\\n' \"$direct_origin_configuration_paths\" ;;",
                script,
            )
            self.assertIn('"$binding_expected_count"', script)
            self.assertNotIn('"$binding_count" -eq 7', script)
            self.assertNotIn('"$binding_count" -eq 11', script)
            for relative_path in (
                "compose.prod.yml",
                "infra/Caddyfile",
                "infra/OriginEdge.Caddyfile",
                "config/origin-edge/origin-cert.pem",
                "config/origin-edge/aop-ca.pem",
                "scripts/install_origin_tls.py",
                "scripts/cloudflare_origin_firewall.py",
                "ops/project-snow-origin-firewall.service",
                "ops/project-snow-origin-firewall.timer",
                "infra/egress-squid.conf",
                "infra/neo4j-entrypoint.sh",
                "infra/postgres/postgresql.conf",
            ):
                self.assertIn(relative_path, script)
        self.assertIn('case "$bootstrap_direct_count" in', deploy)
        self.assertIn("        0|7) ;;", deploy)
        self.assertIn("        4)", deploy)
        self.assertIn("mixed legacy direct-origin configuration bundle", deploy)
        self.assertIn('existing_bootstrap_paths="$(configuration_paths_for_root', deploy)
        self.assertIn('[ "$bootstrap_paths" = "$existing_bootstrap_paths" ]', deploy)
        self.assertIn('-f "$candidate_config_root/compose.prod.yml"', deploy)
        self.assertIn(
            'verify_snapshot_against_manifest "$candidate_config_root" "$release_manifest"',
            deploy,
        )
        self.assertIn(
            'mv -f -- "$candidate_config_binding_tmp" "$colour_config_binding"', deploy
        )
        self.assertIn("seal_configuration_snapshot()", deploy)
        self.assertIn('fsync_release_path "$seal_root/$seal_path"', deploy)
        self.assertIn('find "$seal_root" -depth -type d -print', deploy)
        self.assertIn('fsync_release_path "$configuration_release_root"', deploy)
        self.assertIn(
            '[ "$seal_after_file_state" = "$seal_file_state" ]', deploy
        )
        self.assertIn(
            '[ "$seal_after_dir_state" = "$seal_dir_state" ]', deploy
        )
        candidate_seal = deploy.index(
            'seal_configuration_snapshot "$candidate_config_root"'
        )
        candidate_binding = deploy.index(
            'create_config_binding "$colour" "$sha" "$candidate_config_root"',
            candidate_seal,
        )
        self.assertLess(candidate_seal, candidate_binding)
        bootstrap_seal = deploy.index(
            'seal_configuration_snapshot "$bootstrap_config_root"'
        )
        bootstrap_binding = deploy.index(
            'create_config_binding "$active_colour" "$bootstrap_marker_sha"',
            bootstrap_seal,
        )
        self.assertLess(bootstrap_seal, bootstrap_binding)
        self.assertIn("stage_configuration_binding_is_durable()", runner)
        self.assertIn('fsync_release_path "$durable_config_binding"', runner)
        self.assertIn(
            'fsync_release_path "$stage_config_root/$durable_config_path"', runner
        )
        self.assertIn('find "$stage_config_root" -depth -type d -print', runner)
        self.assertIn(
            "fsync_release_path /srv/project-snow/releases/configurations", runner
        )
        self.assertIn(
            '[ "$stage_config_file_state" = "$durable_config_file_state" ]',
            runner,
        )
        self.assertIn(
            '[ "$stage_config_dir_state" = "$durable_config_dir_state" ]',
            runner,
        )
        self.assertIn('-f "$colour_config_root/compose.prod.yml"', promote)
        self.assertIn(
            "The active colour $previous_colour has no complete rollback snapshot",
            promote,
        )
        self.assertIn('mv -f "$config_tmp" "$current_config_binding"', promote)

    def test_configuration_snapshot_seal_fails_closed_on_fsync_or_mutation(self) -> None:
        deploy = self.read("ops/deploy.sh")
        seal_start = deploy.index("seal_configuration_snapshot() {")
        seal_end = deploy.index(
            "\n}\n\ninstall_direct_origin_firewall()", seal_start
        ) + 2
        seal_function = deploy[seal_start:seal_end]
        snapshot_sha = "a" * 40
        harness = f"""\
set -u
mode=$1
test_root=$2
configuration_release_root="$test_root/configurations"
seal_root="$configuration_release_root/{snapshot_sha}"
log="$test_root/fsync.log"
mkdir -p "$seal_root/infra"
printf '%s\n' compose > "$seal_root/compose.prod.yml"
printf '%s\n' caddy > "$seal_root/infra/Caddyfile"
configuration_paths_for_root() {{
  printf '%s\n' compose.prod.yml infra/Caddyfile
}}
stat() {{
  case "$2" in
    *%h*) printf '%s\n' 1:2:0:0:444:1 ;;
    *) printf '%s\n' 1:3:0:0:555 ;;
  esac
}}
tampered=0
fsync_release_path() {{
  printf '%s\n' "$1" >> "$log"
  if [ "$mode" = parent ] && [ "$1" = "$configuration_release_root" ]; then
    return 1
  fi
  if [ "$mode" = tamper ] && [ "$tampered" -eq 0 ] &&
     [ "$1" = "$seal_root/infra/Caddyfile" ]; then
    printf '%s\n' changed >> "$seal_root/infra/Caddyfile"
    tampered=1
  fi
  return 0
}}
{seal_function}
seal_configuration_snapshot "$seal_root"
"""
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            success = self.run_posix_shell(harness, "success", root.as_posix())
            sync_log = (root / "fsync.log").read_text(encoding="utf-8")
        self.assertEqual(success.returncode, 0, success.stderr)
        compose_sync = sync_log.index(f"/{snapshot_sha}/compose.prod.yml")
        caddy_sync = sync_log.index(f"/{snapshot_sha}/infra/Caddyfile")
        nested_dir_sync = sync_log.index(f"/{snapshot_sha}/infra\n")
        root_dir_sync = sync_log.index(f"/{snapshot_sha}\n", nested_dir_sync)
        parent_sync = sync_log.rindex("/configurations\n")
        self.assertLess(compose_sync, nested_dir_sync)
        self.assertLess(caddy_sync, nested_dir_sync)
        self.assertLess(nested_dir_sync, root_dir_sync)
        self.assertLess(root_dir_sync, parent_sync)

        with tempfile.TemporaryDirectory() as temporary_root:
            parent_failure = self.run_posix_shell(
                harness, "parent", Path(temporary_root).as_posix()
            )
        self.assertNotEqual(parent_failure.returncode, 0)

        with tempfile.TemporaryDirectory() as temporary_root:
            mutation = self.run_posix_shell(
                harness, "tamper", Path(temporary_root).as_posix()
            )
        self.assertNotEqual(mutation.returncode, 0)

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
        public_javascript = self.read("public_frontend/app.js")
        privacy_html = self.read("public_frontend/privacy/index.html")
        public_env = self.read("ops/public.env.example")
        self.assertIn("@frontend_assets path / /index.html /app.js /app.css", caddyfile)
        self.assertIn('header @frontend_assets Cache-Control "no-store, max-age=0"', caddyfile)
        self.assertIn("@content_hashed path_regexp immutable", caddyfile)
        self.assertIn('header @content_hashed Cache-Control "public, max-age=31536000, immutable"', caddyfile)
        self.assertIn("COPY scripts/fingerprint_public_frontend.py", dockerfile)
        self.assertIn("python ./scripts/fingerprint_public_frontend.py --app-root /app", dockerfile)
        self.assertIn("pip install --no-cache-dir --require-hashes", dockerfile)
        self.assertIn('href="/app.css?v=0.9.2"', public_html)
        self.assertIn('src="/app.js?v=0.9.2"', public_html)
        self.assertIn('href="/shared/immersive.css?v=0.9.2"', public_html)
        self.assertIn('src="/privacy/privacy.js?v=0.9.2"', privacy_html)
        self.assertIn('experience_notice_version || "0.9.2"', public_javascript)
        self.assertIn("PUBLIC_APP_VERSION=0.9.2", public_env)
        self.assertIn("PUBLIC_EXPERIENCE_NOTICE_VERSION=0.9.2", public_env)
        self.assertIn("PUBLIC_PRIVACY_POLICY_VERSION=0.9.2", public_env)
        self.assertIn("PUBLIC_BYOK_LIFETIME_HOURS=12", public_env)
        self.assertIn("PUBLIC_MEDIA_VERSION=2026.08.19.avatar.1", public_env)
        self.assertIn("PUBLIC_STICKER_VERSION=2026.08.19.sticker.1", public_env)
        self.assertIn("@versioned_media path /media/*", caddyfile)

    def test_browser_ci_uses_preinstalled_allowlisted_chrome(self) -> None:
        workflow = (self.app_root.parent / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        frontend_tests = self.read("tests/test_public_frontend_e2e.py")
        self.assertNotIn("python -m playwright install", workflow)
        self.assertEqual(
            workflow.count("PROJECT_SNOW_PLAYWRIGHT_CHANNEL: chrome"),
            2,
        )
        self.assertEqual(workflow.count("google-chrome --version"), 2)
        self.assertNotIn("playwright.chromium.launch()", frontend_tests)
        self.assertEqual(frontend_tests.count("_launch_browser(playwright)"), 17)
