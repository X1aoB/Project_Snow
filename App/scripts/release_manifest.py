"""Create and validate immutable Project Snow deployment manifests."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(
    r'^revision(?:\s*:\s*str)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE
)
DOWN_REVISION_PATTERN = re.compile(
    r'^down_revision(?:\s*:\s*(?:str\s*\|\s*None|Union\[[^\]]+\]))?\s*=\s*(.+)$',
    re.MULTILINE,
)

RUNTIME_CONFIGURATION_PATHS = (
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
    "infra/public-api.Dockerfile",
    "requirements-public.txt",
)

RELEASE_CONTROL_PATHS = (
    "ops/project-snow-release",
    "ops/project-snow-release.sudoers",
)

ORIGIN_TLS_SCHEMA = "project-snow-origin-tls-1"
ORIGIN_TLS_HOSTNAME = "snow.xiaob.dev"
ORIGIN_CERTIFICATE_PATH = "config/origin-edge/origin-cert.pem"
AOP_CA_PATH = "config/origin-edge/aop-ca.pem"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _public_certificate_sha256(path: Path, label: str) -> str:
    payload = path.read_bytes()
    if b"PRIVATE KEY" in payload:
        raise ValueError(f"{label} must never contain private key material")
    if (
        payload.count(b"-----BEGIN CERTIFICATE-----") != 1
        or payload.count(b"-----END CERTIFICATE-----") != 1
    ):
        raise ValueError(f"{label} must contain exactly one PEM certificate")
    return hashlib.sha256(payload).hexdigest()


def read_origin_tls_binding(app_root: Path) -> dict[str, str]:
    origin_certificate_sha256 = _public_certificate_sha256(
        app_root / ORIGIN_CERTIFICATE_PATH, "origin certificate"
    )
    aop_ca_sha256 = _public_certificate_sha256(app_root / AOP_CA_PATH, "AOP CA")
    identity_payload = (
        f"{ORIGIN_TLS_SCHEMA}\n{ORIGIN_TLS_HOSTNAME}\n"
        f"{origin_certificate_sha256}\n{aop_ca_sha256}\n"
    ).encode("ascii")
    return {
        "schema_version": ORIGIN_TLS_SCHEMA,
        "hostname": ORIGIN_TLS_HOSTNAME,
        "bundle_sha256": hashlib.sha256(identity_payload).hexdigest(),
        "origin_certificate_sha256": origin_certificate_sha256,
        "aop_ca_sha256": aop_ca_sha256,
    }


def read_public_versions(app_root: Path) -> tuple[str, str, str, str]:
    public_env = app_root / "ops" / "public.env.example"
    values: dict[str, str] = {}
    for raw_line in public_env.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    app_version = values.get("PUBLIC_APP_VERSION", "")
    media_version = values.get("PUBLIC_MEDIA_VERSION", "")
    sticker_version = values.get("PUBLIC_STICKER_VERSION", "")
    data_pointer = json.loads(
        (app_root / "config" / "public_knowledge" / "data_release.json").read_text(
            encoding="utf-8"
        )
    )
    data_version = str(data_pointer.get("data_version") or "")
    if not app_version or not data_version or not media_version or not sticker_version:
        raise ValueError(
            "public environment must define application, data, avatar media and sticker versions"
        )
    return app_version, data_version, media_version, sticker_version


def read_release_artifacts(
    app_root: Path,
    *,
    data_version: str,
    media_version: str,
    sticker_version: str,
) -> dict[str, dict[str, str]]:
    index_path = app_root / "config" / "public_release_artifacts.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(index, dict) or set(index) != {"schema_version", "artifacts"}:
        raise ValueError("public release artifact index has unexpected fields")
    if index.get("schema_version") != "project-snow-release-artifacts-1":
        raise ValueError("unsupported public release artifact index")
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"data", "avatar", "sticker"}:
        raise ValueError("public release artifact index must bind data, avatar and sticker")

    expected_versions = {
        "data": data_version,
        "avatar": media_version,
        "sticker": sticker_version,
    }
    normalized: dict[str, dict[str, str]] = {}
    for kind, expected_version in expected_versions.items():
        entry = artifacts.get(kind)
        expected_fields = {"version", "manifest_sha256"}
        if kind != "data":
            expected_fields.add("checksums_sha256")
        if not isinstance(entry, dict) or set(entry) != expected_fields:
            raise ValueError(f"{kind} release artifact binding has unexpected fields")
        version = str(entry.get("version") or "")
        manifest_sha256 = str(entry.get("manifest_sha256") or "")
        if version != expected_version or not SHA256_PATTERN.fullmatch(manifest_sha256):
            raise ValueError(f"{kind} release artifact binding is invalid")
        normalized_entry = {
            "version": version,
            "manifest_sha256": manifest_sha256,
        }
        if kind != "data":
            checksums_sha256 = str(entry.get("checksums_sha256") or "")
            if not SHA256_PATTERN.fullmatch(checksums_sha256):
                raise ValueError(f"{kind} checksum binding is invalid")
            normalized_entry["checksums_sha256"] = checksums_sha256
        normalized[kind] = normalized_entry
    return normalized


def migration_heads(versions_directory: Path) -> list[str]:
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions_directory.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        revision_match = REVISION_PATTERN.search(source)
        if not revision_match:
            continue
        revisions.add(revision_match.group(1))
        down_match = DOWN_REVISION_PATTERN.search(source)
        if not down_match:
            continue
        value = down_match.group(1).strip()
        if value == "None":
            continue
        parents.update(re.findall(r'["\']([^"\']+)["\']', value))
    heads = sorted(revisions - parents)
    if not heads:
        raise ValueError("no Alembic migration head found")
    return heads


def create_manifest(
    *,
    commit_sha: str,
    public_image: str,
    public_digest: str,
    embedding_image: str,
    embedding_digest: str,
    app_root: Path,
) -> dict[str, Any]:
    if not SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("commit SHA must contain 40 lowercase hexadecimal characters")
    for digest in (public_digest, embedding_digest):
        if not DIGEST_PATTERN.fullmatch(digest):
            raise ValueError("image digests must use sha256:<64 lowercase hexadecimal characters>")
    app_version, data_version, media_version, sticker_version = read_public_versions(app_root)
    release_artifacts = read_release_artifacts(
        app_root,
        data_version=data_version,
        media_version=media_version,
        sticker_version=sticker_version,
    )
    return {
        "schema_version": "project-snow-release-1",
        "commit_sha": commit_sha,
        "app_version": app_version,
        "data_version": data_version,
        "media_version": media_version,
        "sticker_version": sticker_version,
        "release_artifacts": release_artifacts,
        "migration_heads": migration_heads(app_root / "migrations" / "versions"),
        "application": {"image": public_image, "digest": public_digest},
        "embedding": {"image": embedding_image, "digest": embedding_digest},
        "configuration_sha256": {
            relative_path: _sha256_file(app_root / relative_path)
            for relative_path in RUNTIME_CONFIGURATION_PATHS
        },
        "release_control_sha256": {
            relative_path: _sha256_file(app_root / relative_path)
            for relative_path in RELEASE_CONTROL_PATHS
        },
        "direct_origin_tls": read_origin_tls_binding(app_root),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--public-image", required=True)
    parser.add_argument("--public-digest", required=True)
    parser.add_argument("--embedding-image", required=True)
    parser.add_argument("--embedding-digest", required=True)
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = create_manifest(
        commit_sha=args.sha,
        public_image=args.public_image,
        public_digest=args.public_digest,
        embedding_image=args.embedding_image,
        embedding_digest=args.embedding_digest,
        app_root=args.app_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
