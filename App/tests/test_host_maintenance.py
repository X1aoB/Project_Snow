from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

import pytest

OPS = Path(__file__).resolve().parents[1] / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))
import maintenance


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "snow"
    (root / "releases").mkdir(parents=True)
    (root / "runtime").mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    image = "ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64
    embedding = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
    commit = "c" * 40
    (root / "releases" / "active-colour").write_text("blue\n")
    (root / "releases" / "current").write_text(f"blue {commit} {image} {embedding}\n")
    (root / "releases" / "current-manifest.json").write_text(json.dumps({"commit_sha": commit, "application": {"image": image.split("@")[0], "digest": image.split("@")[1]}}))
    (root / "runtime" / "compose.env").write_text(f"PUBLIC_API_IMAGE={image}\n")
    (secrets / "public_database_url").write_text("never-read-host-secret")
    return maintenance.Paths(root=root, secrets=secrets, docker_storage=tmp_path / "docker"), image


def fake_docker(calls, *, wrong_image=False, internal=True):
    def execute(command):
        calls.append(command)
        if command[1:3] == ["network", "inspect"]:
            return json.dumps([{"Id": "d" * 64, "Name": "project-snow-public_data", "Driver": "bridge", "Internal": internal,
                                "Labels": {"com.docker.compose.project": "project-snow-public", "com.docker.compose.network": "data"}}])
        if command[1:3] == ["image", "inspect"]:
            return json.dumps([{"Id": "sha256:" + "e" * 64}])
        if command[1] == "ps":
            return "f" * 64
        if command[1] == "inspect":
            return json.dumps([{"Image": "sha256:" + ("a" if wrong_image else "e") * 64}])
        if command[1] == "run":
            return '{"deleted":{"public_feedback":2}}'
        raise AssertionError(command)
    return execute


def test_cleanup_uses_active_digest_existing_network_and_only_database_secret(release):
    paths, image = release
    calls = []
    with patch.object(maintenance, "require_regular"):
        result = maintenance.cleanup(paths, fake_docker(calls))
    command = calls[-1]
    assert result["deleted"] == {"public_feedback": 2}
    assert command[1] == "run" and "--pull=never" in command
    assert command[command.index("--network") + 1] == "d" * 64
    assert image in command and "--entrypoint" in command
    assert command.count("--mount") == 1
    assert "public_database_url" in command[command.index("--mount") + 1]
    assert all("compose" not in command and "create" not in command for command in calls)
    assert "never-read-host-secret" not in repr(calls)
    assert "public_admin_token" not in repr(calls) and "management" not in repr(calls)


@pytest.mark.parametrize("fault", ["image", "network", "manifest"])
def test_cleanup_fails_before_container_start_on_runtime_drift(release, fault):
    paths, _ = release
    calls = []
    if fault == "manifest":
        (paths.root / "runtime" / "compose.env").write_text("PUBLIC_API_IMAGE=drifted\n")
    with patch.object(maintenance, "require_regular"), pytest.raises(maintenance.MaintenanceError):
        maintenance.cleanup(paths, fake_docker(calls, wrong_image=fault == "image", internal=fault != "network"))
    assert not any(command[1] == "run" for command in calls)


@pytest.mark.parametrize("growth,free,passes", [(0, 10, True), (0, 9, False), (6, 11, False), (6, 12, True)])
def test_capacity_reserves_larger_of_ten_gib_or_twice_growth(tmp_path, growth, free, passes):
    paths = maintenance.Paths(root=tmp_path, docker_storage=tmp_path)
    usage = lambda _: shutil._ntuple_diskusage(50 * maintenance.GIB, 0, free * maintenance.GIB)
    if passes:
        assert maintenance.capacity(paths, growth * maintenance.GIB, usage)["status"] == "ok"
    else:
        with pytest.raises(maintenance.MaintenanceError):
            maintenance.capacity(paths, growth * maintenance.GIB, usage)


def test_capacity_checks_docker_mount_separately(tmp_path):
    paths = maintenance.Paths(root=tmp_path / "app", docker_storage=tmp_path / "docker")
    usage = lambda path: shutil._ntuple_diskusage(50, 0, (20 if path == paths.root else 2) * maintenance.GIB)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.capacity(paths, 0, usage)


def test_environment_rejects_duplicate_image_pin(tmp_path):
    environment = tmp_path / "compose.env"
    environment.write_text("PUBLIC_API_IMAGE=a\nPUBLIC_API_IMAGE=b\n")
    with patch.object(maintenance, "require_regular"), pytest.raises(maintenance.MaintenanceError):
        maintenance.read_environment(environment)


def test_lease_recovery_requires_stopped_source_and_only_uses_its_verified_digest(release):
    paths, image = release
    calls = []
    base = fake_docker(calls)
    def execute(command):
        if command[1] == "ps":
            calls.append(command)
            return ""
        if command[1] == "run":
            calls.append(command)
            return '{"recovered":2,"remaining_leases":0}'
        return base(command)
    with patch.object(maintenance, "require_regular"):
        result = maintenance.recover_requests(paths, execute)
    assert result["recovered"] == 2
    job = calls[-1]
    assert image in job and job.count("--mount") == 1
    assert "recover_expired_requests" in job[-1] and "time.monotonic() + 46" in job[-1]
    assert not any("compose" in command or "prune" in command for command in calls)
    with patch.object(maintenance, "require_regular"), pytest.raises(maintenance.MaintenanceError, match="Stop and drain"):
        maintenance.recover_requests(paths, fake_docker([]))


@pytest.mark.parametrize("changed", [None, "EMBEDDING_IMAGE", "POSTGRES_IMAGE", "infra/egress-squid.conf"])
def test_stage_rejects_shared_dependency_changes(release, changed):
    paths, image = release
    environment = {key: image for key in maintenance.SHARED_IMAGE_KEYS}
    (paths.root / "runtime" / "compose.env").write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
    if changed in environment:
        environment[changed] = image.replace("a" * 64, "b" * 64)
    candidate = paths.root / "runtime" / "candidate.env"
    candidate.write_text("".join(f"{key}={value}\n" for key, value in environment.items()))
    hashes = {key: "a" * 64 for key in maintenance.SHARED_CONFIG_PATHS}
    (paths.root / "releases" / "current-config.json").write_text(json.dumps({"configuration_sha256": hashes}))
    if changed in hashes:
        hashes[changed] = "b" * 64
    manifest = paths.root / "candidate.json"
    manifest.write_text(json.dumps({"configuration_sha256": hashes}))
    with patch.object(maintenance, "require_regular"):
        if changed:
            with pytest.raises(maintenance.MaintenanceError, match="independent backed-up"):
                maintenance.shared_dependency_gate(paths, candidate, manifest)
        else:
            assert maintenance.shared_dependency_gate(paths, candidate, manifest)["status"] == "ok"


def test_stage_never_reconciles_shared_services():
    source = (OPS / "deploy.sh").read_text()
    assert 'compose run --rm --no-deps "$service" alembic upgrade head' in source
    assert 'compose up -d --no-deps "$service"' in source
    assert 'compose up -d postgres qdrant neo4j embedding egress-proxy' not in source
    assert source.index("shared-dependency-gate") < source.index('compose pull "$service"')


OLD_EMBEDDING = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "b" * 64
NEW_EMBEDDING = "ghcr.io/x1aob/project_snow-embedding@sha256:" + "c" * 64


@pytest.fixture
def swap(release, tmp_path):
    paths, image = release
    swap_dir = tmp_path / "embedding-swap"
    swap_dir.mkdir()
    paths = maintenance.Paths(root=paths.root, secrets=paths.secrets,
                              docker_storage=paths.docker_storage, embedding_swap=swap_dir)
    environment = {key: (OLD_EMBEDDING if key == "EMBEDDING_IMAGE" else image) for key in maintenance.SHARED_IMAGE_KEYS}
    (paths.root / "runtime" / "compose.env").write_text("".join(f"{k}={v}\n" for k, v in environment.items()))
    hashes = {key: "a" * 64 for key in maintenance.SHARED_CONFIG_PATHS}
    (paths.root / "releases" / "current-config.json").write_text(json.dumps({"configuration_sha256": hashes}))
    candidate_env = dict(environment)
    candidate_env["EMBEDDING_IMAGE"] = NEW_EMBEDDING
    candidate = paths.root / "runtime" / "candidate.env"
    candidate.write_text("".join(f"{k}={v}\n" for k, v in candidate_env.items()))
    manifest = paths.root / "candidate.json"
    manifest.write_text(json.dumps({"configuration_sha256": hashes}))
    return paths, candidate, manifest, swap_dir


def write_authorization(swap_dir, *, to=NEW_EMBEDDING, from_=OLD_EMBEDDING, schema=maintenance.EMBEDDING_SWAP_SCHEMA):
    digest = to.rsplit(":", 1)[1]
    (swap_dir / f"{digest}.env").write_text(
        f"schema={schema}\nfrom={from_}\nto={to}\napproved_at=2026-10-26T00:00:00+00:00\nprobe_sha256={'d' * 64}\n")


def test_gate_refuses_embedding_change_without_authorization(swap):
    paths, candidate, manifest, _swap_dir = swap
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="independent backed-up"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_gate_accepts_embedding_change_with_matching_authorization(swap):
    paths, candidate, manifest, swap_dir = swap
    write_authorization(swap_dir)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        result = maintenance.shared_dependency_gate(paths, candidate, manifest)
    assert result == {"status": "ok", "shared_dependencies": "embedding-swap-authorized",
                      "from": OLD_EMBEDDING, "to": NEW_EMBEDDING}


def test_gate_refuses_authorization_with_mismatched_refs(swap):
    paths, candidate, manifest, swap_dir = swap
    # Authorization names a different running digest than compose.env records.
    write_authorization(swap_dir, from_="ghcr.io/x1aob/project_snow-embedding@sha256:" + "9" * 64)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="does not match the running"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_gate_refuses_authorization_with_unknown_schema(swap):
    paths, candidate, manifest, swap_dir = swap
    write_authorization(swap_dir, schema="project-snow-embedding-swap-9")
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="schema is unknown"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_gate_still_refuses_when_a_second_shared_key_also_changes(swap):
    paths, candidate, manifest, swap_dir = swap
    write_authorization(swap_dir)
    # Flip POSTGRES_IMAGE too: an authorized embedding swap must be the ONLY change.
    text = candidate.read_text().replace(
        "POSTGRES_IMAGE=ghcr.io/x1aob/project_snow-public@sha256:" + "a" * 64,
        "POSTGRES_IMAGE=ghcr.io/x1aob/project_snow-public@sha256:" + "e" * 64)
    candidate.write_text(text)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="independent backed-up"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_authorization_reader_rejects_unsafe_file_mode(swap):
    paths, _candidate, _manifest, swap_dir = swap
    write_authorization(swap_dir)
    def unsafe(path, *, private=False):
        if private:
            raise maintenance.MaintenanceError("Expected a root-only file: " + path.name)
    with patch.object(maintenance, "require_regular", unsafe), patch.object(maintenance, "_require_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="root-only file"):
            maintenance.embedding_swap_authorization(paths, OLD_EMBEDDING, NEW_EMBEDDING)


def test_authorization_absent_returns_none(swap):
    paths, _candidate, _manifest, _swap_dir = swap
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        assert maintenance.embedding_swap_authorization(paths, OLD_EMBEDDING, NEW_EMBEDDING) is None


def write_probe_receipt(path, *, from_=OLD_EMBEDDING, to=NEW_EMBEDDING, status="pass", passed=True):
    path.write_text(json.dumps({"schema": "project-snow-embedding-probe-1", "status": status,
                                "passed": passed, "from": from_, "to": to, "cosine_min": 0.999999}))


def test_authorize_writes_pinned_file_from_passing_receipt(swap, tmp_path):
    paths, _candidate, _manifest, swap_dir = swap
    receipt = tmp_path / "receipt.json"
    write_probe_receipt(receipt)
    import hashlib as _h
    expected_sha = _h.sha256(receipt.read_bytes()).hexdigest()
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"), \
            patch.object(maintenance, "active_release", return_value={"colour": "blue"}):
        result = maintenance.embedding_swap_authorize(paths, OLD_EMBEDDING, NEW_EMBEDDING, receipt)
        written = maintenance.embedding_swap_authorization(paths, OLD_EMBEDDING, NEW_EMBEDDING)
    assert result == {"status": "authorized", "from": OLD_EMBEDDING, "to": NEW_EMBEDDING, "probe_sha256": expected_sha}
    assert written["from"] == OLD_EMBEDDING and written["to"] == NEW_EMBEDDING and written["probe_sha256"] == expected_sha
    assert written["schema"] == maintenance.EMBEDDING_SWAP_SCHEMA


def test_authorize_refuses_when_running_digest_differs(swap, tmp_path):
    paths, _candidate, _manifest, _swap_dir = swap
    receipt = tmp_path / "receipt.json"
    write_probe_receipt(receipt, from_="ghcr.io/x1aob/project_snow-embedding@sha256:" + "7" * 64)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"), \
            patch.object(maintenance, "active_release", return_value={"colour": "blue"}):
        with pytest.raises(maintenance.MaintenanceError, match="running embedding digest"):
            maintenance.embedding_swap_authorize(paths, "ghcr.io/x1aob/project_snow-embedding@sha256:" + "7" * 64,
                                                 NEW_EMBEDDING, receipt)


@pytest.mark.parametrize("fault,match", [
    ("status", "passing comparison"),
    ("refs", "different embedding digests"),
])
def test_authorize_refuses_non_passing_or_mismatched_receipt(swap, tmp_path, fault, match):
    paths, _candidate, _manifest, _swap_dir = swap
    receipt = tmp_path / "receipt.json"
    if fault == "status":
        write_probe_receipt(receipt, status="fail", passed=False)
    else:
        write_probe_receipt(receipt, to="ghcr.io/x1aob/project_snow-embedding@sha256:" + "8" * 64)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"), \
            patch.object(maintenance, "active_release", return_value={"colour": "blue"}):
        with pytest.raises(maintenance.MaintenanceError, match=match):
            maintenance.embedding_swap_authorize(paths, OLD_EMBEDDING, NEW_EMBEDDING, receipt)


def test_clear_removes_only_well_formed_authorizations(swap):
    paths, _candidate, _manifest, swap_dir = swap
    write_authorization(swap_dir)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_private_directory"):
        result = maintenance.embedding_swap_clear(paths)
    assert result["status"] == "cleared"
    assert result["removed"] == [NEW_EMBEDDING.rsplit(":", 1)[1] + ".env"]
    assert list(swap_dir.iterdir()) == []
    with patch.object(maintenance, "_require_private_directory"):
        assert maintenance.embedding_swap_clear(paths)["status"] == "cleared"


def test_clear_refuses_unexpected_directory_entry(swap):
    paths, _candidate, _manifest, swap_dir = swap
    (swap_dir / "notes.txt").write_text("stray")
    with patch.object(maintenance, "_require_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="Unexpected entry"):
            maintenance.embedding_swap_clear(paths)


def test_promote_recreates_embedding_only_under_authorization_and_reverts():
    source = (OPS / "promote.sh").read_text()
    # The swap runs only when the running embedding differs from the marker.
    assert 'if ! embedding_running_before="$(running_embedding_image)"; then' in source
    assert '[ "$embedding_running_before" != "$marker_embedding_image" ]; then' in source
    # Forward promotion requires the authorized gate result.
    assert '"shared_dependencies": "embedding-swap-authorized"' in source
    # The target image must be present locally before any recreate.
    assert 'embedding_image_present "$marker_embedding_image"' in source
    # Failure recreates the previous digest and restores traffic without committing.
    assert 'recreate_embedding_from_env "$previous_env" "$previous_config_root" "$previous_colour"' in source
    # The authorization is consumed only after a committed forward swap.
    assert source.index("embedding_swapped=1") < source.index("embedding-swap-clear")
    assert '--force-recreate embedding' in source
    # A normal release keeps the identical pre-existing switch path: the swap is
    # gated on a running-vs-marker digest difference, so an unchanged embedding
    # skips the whole block.
    guard = source.index('if ! embedding_running_before="$(running_embedding_image)"; then')
    assert '[ "$embedding_running_before" != "$marker_embedding_image" ]; then' in source[guard:guard + 1200]


def test_promote_embedding_health_checks_dimension_and_embed_sanity():
    source = (OPS / "promote.sh").read_text()
    assert "/health" in source and "'dimension'" in source and "= 512" in source
    assert "/embed" in source and "len(vector)==512" in source


# --- egress-config swap (mirrors the embedding swap) ------------------------

OLD_EGRESS_SHA = "a" * 64
NEW_EGRESS_SHA = "e" * 64


@pytest.fixture
def egress_swap(release, tmp_path):
    paths, image = release
    swap_dir = tmp_path / "egress-swap"
    swap_dir.mkdir()
    paths = maintenance.Paths(root=paths.root, secrets=paths.secrets,
                              docker_storage=paths.docker_storage, egress_swap=swap_dir)
    environment = {key: image for key in maintenance.SHARED_IMAGE_KEYS}
    (paths.root / "runtime" / "compose.env").write_text("".join(f"{k}={v}\n" for k, v in environment.items()))
    hashes = {key: OLD_EGRESS_SHA for key in maintenance.SHARED_CONFIG_PATHS}
    (paths.root / "releases" / "current-config.json").write_text(json.dumps({"configuration_sha256": hashes}))
    candidate = paths.root / "runtime" / "candidate.env"
    candidate.write_text("".join(f"{k}={v}\n" for k, v in environment.items()))
    candidate_hashes = dict(hashes)
    candidate_hashes[maintenance.EGRESS_SWAP_PATH] = NEW_EGRESS_SHA
    manifest = paths.root / "candidate.json"
    manifest.write_text(json.dumps({"configuration_sha256": candidate_hashes}))
    return paths, candidate, manifest, swap_dir


def write_egress_authorization(swap_dir, *, to=NEW_EGRESS_SHA, from_=OLD_EGRESS_SHA,
                               schema=maintenance.EGRESS_SWAP_SCHEMA, parse_ok="true"):
    (swap_dir / f"{to}.env").write_text(
        f"schema={schema}\nfrom_sha256={from_}\nto_sha256={to}\n"
        f"approved_at=2026-10-26T00:00:00+00:00\nparse_ok={parse_ok}\n")


def test_egress_gate_refuses_without_authorization(egress_swap):
    paths, candidate, manifest, _swap_dir = egress_swap
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="independent backed-up"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_egress_gate_accepts_with_matching_authorization(egress_swap):
    paths, candidate, manifest, swap_dir = egress_swap
    write_egress_authorization(swap_dir)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        result = maintenance.shared_dependency_gate(paths, candidate, manifest)
    assert result == {"status": "ok", "shared_dependencies": "egress-config-swap-authorized",
                      "from": OLD_EGRESS_SHA, "to": NEW_EGRESS_SHA}


def test_egress_gate_refuses_mismatched_hashes(egress_swap):
    paths, candidate, manifest, swap_dir = egress_swap
    write_egress_authorization(swap_dir, from_="9" * 64)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="does not match the running"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_egress_gate_refuses_unknown_schema(egress_swap):
    paths, candidate, manifest, swap_dir = egress_swap
    write_egress_authorization(swap_dir, schema="project-snow-egress-swap-9")
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="schema is unknown"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_egress_gate_refuses_when_parse_not_ok(egress_swap):
    paths, candidate, manifest, swap_dir = egress_swap
    write_egress_authorization(swap_dir, parse_ok="false")
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="passing squid parse"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_egress_gate_still_refuses_when_a_second_shared_item_also_changes(egress_swap):
    paths, candidate, manifest, swap_dir = egress_swap
    write_egress_authorization(swap_dir)
    # A second shared config change alongside the egress conf must stay refused.
    document = json.loads(manifest.read_text())
    document["configuration_sha256"]["infra/postgres/postgresql.conf"] = "f" * 64
    manifest.write_text(json.dumps(document))
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        with pytest.raises(maintenance.MaintenanceError, match="independent backed-up"):
            maintenance.shared_dependency_gate(paths, candidate, manifest)


def test_egress_authorize_writes_pinned_file_and_reads_back(egress_swap):
    paths, _candidate, manifest, swap_dir = egress_swap
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        result = maintenance.egress_swap_authorize(paths, manifest, parse_ok=True)
        written = maintenance.egress_swap_authorization(paths, OLD_EGRESS_SHA, NEW_EGRESS_SHA)
    assert result == {"status": "authorized", "from_sha256": OLD_EGRESS_SHA, "to_sha256": NEW_EGRESS_SHA}
    assert written is not None and written["to_sha256"] == NEW_EGRESS_SHA


def test_egress_authorize_refuses_without_parse_ok(egress_swap):
    paths, _candidate, manifest, _swap_dir = egress_swap
    with pytest.raises(maintenance.MaintenanceError, match="squid -k parse"):
        maintenance.egress_swap_authorize(paths, manifest, parse_ok=False)


def test_egress_clear_removes_only_well_formed(egress_swap):
    paths, _candidate, _manifest, swap_dir = egress_swap
    write_egress_authorization(swap_dir)
    with patch.object(maintenance, "require_regular"), patch.object(maintenance, "_require_egress_private_directory"):
        result = maintenance.egress_swap_clear(paths)
    assert result["status"] == "cleared"
    assert result["removed"] == [NEW_EGRESS_SHA + ".env"]


def test_promote_admits_a_changed_egress_conf_before_any_traffic_moves():
    source = (OPS / "promote.sh").read_text()
    guard = source.index('egress_conf_before="$(egress_conf_sha_from_binding "$previous_config_binding")"')
    assert '[ "$egress_conf_before" != "$egress_conf_target" ]' in source[guard:guard + 400]
    preflight = source[guard:source.index("\nfi\n", guard)]
    # Authorization and squid -k parse are refused before the edge switch, so a
    # refused conf never reaches the live proxy and needs no runtime restore.
    authorization = preflight.index('"shared_dependencies": "egress-config-swap-authorized"')
    assert authorization < preflight.index('egress_conf_parses "$colour_env" "$colour_config_root"')
    assert "exit 73" in preflight and "restore_previous_runtime" not in preflight
    assert source.index(preflight) < source.index('if ! switch_edge "$colour_env" "$colour_config_root" "$colour"')
    # The edge switch recreates egress-proxy itself; promote never does it twice.
    assert "recreate_egress_from_env" not in source
    assert "--force-recreate egress-proxy" not in source


def test_promote_verifies_egress_policy_after_the_switch_and_clears_after_commit():
    source = (OPS / "promote.sh").read_text()
    edge_switch = source.index('if ! switch_edge "$colour_env" "$colour_config_root" "$colour"')
    policy = source.index('egress_policy_holds "$colour_env" "$colour_config_root" "$colour" $egress_new_hosts')
    assert edge_switch < policy < source.index("commit_promoted_state() {")
    # Only a forward swap probes the newly allowed vendor hosts: a rollback to a
    # conf that predates them must still pass.
    forward = source.rindex('if [ "$rollback_mode" = 0 ]; then', 0, policy)
    assert 'egress_new_hosts="api.anthropic.com api.x.ai generativelanguage.googleapis.com"' in source[forward:policy]
    # Authorization consumed only after a committed forward swap.
    assert source.index("egress_swapped=1") < source.index("egress-config-swap-clear")


def _promote_functions(*names: str) -> str:
    source = (OPS / "promote.sh").read_text(encoding="utf-8")
    chunks = []
    for name in names:
        start = source.index(f"\n{name}() {{\n") + 1
        chunks.append(source[start:source.index("\n}\n", start) + 3])
    return "\n".join(chunks)


def _run_posix(script: str, tmp_path, **environment: str):
    import os
    import subprocess

    candidates = ("C:/Program Files/Git/bin/bash.exe", shutil.which("sh"), shutil.which("bash"))
    shell = next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)
    assert shell, "A POSIX shell is required for promote.sh behaviour tests."
    path = tmp_path / "harness.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    return subprocess.run([shell, str(path)], capture_output=True, text=True, check=False,
                          env={**os.environ, **environment})


EGRESS_POLICY_HARNESS = r'''set -u
sleep() { :; }
egress_connect_status() {
  printf '%s\n' "$4" >> "$PROBE_LOG"
  case "$4" in
    challenges.cloudflare.com) printf '%s\n' "$TURNSTILE_STATUS" ;;
    169.254.169.254) printf '%s\n' "$METADATA_STATUS" ;;
    *) printf '%s\n' "$VENDOR_STATUS" ;;
  esac
}
'''


@pytest.mark.parametrize(("turnstile", "metadata", "vendor", "hosts", "expected"), [
    ("200", "403", "200", "api.anthropic.com api.x.ai", 0),
    # A rollback to the 1.0.2 conf, which refuses the new vendors, names none.
    ("200", "403", "403", "", 0),
    ("200", "403", "403", "api.anthropic.com", 1),
    ("200", "403", "000", "api.x.ai", 1),
    ("200", "403", "503", "generativelanguage.googleapis.com", 0),
    ("200", "200", "200", "", 1),
    ("503", "403", "200", "", 1),
])
def test_egress_policy_probe_outcomes(tmp_path, turnstile, metadata, vendor, hosts, expected):
    log = tmp_path / "probes.log"
    script = (EGRESS_POLICY_HARNESS + _promote_functions("egress_policy_holds")
              + f"\negress_policy_holds env root green {hosts}\n")
    result = _run_posix(script, tmp_path, PROBE_LOG=str(log), TURNSTILE_STATUS=turnstile,
                        METADATA_STATUS=metadata, VENDOR_STATUS=vendor)
    assert result.returncode == expected, result.stderr
    probed = log.read_text().split()
    if turnstile != "200":
        # Waits for the recreated proxy, then gives up without other probes.
        assert probed == ["challenges.cloudflare.com"] * 15
    if expected == 0:
        assert probed == ["challenges.cloudflare.com", "169.254.169.254", *hosts.split()]
    if vendor == "503" and hosts:
        assert "its upstream answered 503" in result.stderr


@pytest.mark.parametrize(("reply", "status", "expected"), [
    ("200", "0", "200"),
    ("403", "0", "403"),
    ("HTTP/1.1 200", "0", "000"),
    ("", "1", "000"),
])
def test_egress_connect_status_normalises_the_probe_reply(tmp_path, reply, status, expected):
    script = ("set -u\ndocker() { printf '%s\\n' \"$DOCKER_REPLY\"; return \"$DOCKER_STATUS\"; }\n"
              + _promote_functions("egress_connect_status")
              + "\negress_connect_status env root blue api.x.ai\n")
    result = _run_posix(script, tmp_path, DOCKER_REPLY=reply, DOCKER_STATUS=status)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
