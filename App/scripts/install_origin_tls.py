"""Install one immutable direct-origin TLS bundle from the deploy inbox.

The only private input is a single origin key uploaded by the unprivileged
``deploy`` account.  Public certificates come exclusively from the exact Git
configuration snapshot selected by the release controller.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


TLS_SCHEMA = "project-snow-origin-tls-1"
INSTALL_SCHEMA = "project-snow-origin-tls-install-1"
ORIGIN_HOSTNAME = "snow.xiaob.dev"
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
MIN_KEY_BYTES = 80
MAX_KEY_BYTES = 64 * 1024
MIN_CERT_BYTES = 128
MAX_CERT_BYTES = 512 * 1024


class OriginTlsError(RuntimeError):
    """Raised when an origin TLS bundle cannot be installed safely."""


@dataclass(frozen=True)
class RuntimePaths:
    inbox: Path = Path("/srv/project-snow/inbox")
    configuration_root: Path = Path("/srv/project-snow/releases/configurations")
    destination_root: Path = Path("/etc/project-snow/origin-edge")
    openssl: Path = Path("/usr/bin/openssl")


@dataclass(frozen=True)
class ValidatedRegularFile:
    path: Path
    payload: bytes
    device: int
    inode: int
    owner_uid: int
    owner_gid: int
    mode: int
    link_count: int
    size: int


def _validate_hex(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise OriginTlsError(f"{label} has an invalid lowercase hexadecimal identity")
    return value


def origin_tls_bundle_sha256(origin_cert_sha256: str, aop_ca_sha256: str) -> str:
    """Return the public, deterministic identity of an origin TLS bundle."""

    certificate_hash = _validate_hex(
        origin_cert_sha256, HEX_64, "origin certificate SHA256"
    )
    aop_hash = _validate_hex(aop_ca_sha256, HEX_64, "AOP CA SHA256")
    payload = (
        f"{TLS_SCHEMA}\n{ORIGIN_HOSTNAME}\n{certificate_hash}\n{aop_hash}\n"
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _require_directory(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    label: str,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OriginTlsError(f"{label} is missing") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise OriginTlsError(f"{label} ownership, mode or type is invalid")
    return metadata


def _require_root_controlled_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise OriginTlsError(f"{label} is missing") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise OriginTlsError(f"{label} ownership, mode or type is invalid")
    return metadata


def _ensure_root_directory(path: Path, *, label: str) -> None:
    try:
        os.mkdir(path, 0o700)
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)
        _fsync_directory(path.parent)
    except FileExistsError:
        pass
    except OSError as exc:
        raise OriginTlsError(f"could not create {label}") from exc
    _require_directory(path, owner_uid=0, owner_gid=0, mode=0o700, label=label)


def _prepare_destination_root(path: Path) -> None:
    _require_root_controlled_directory(
        path.parent,
        label="Project Snow system configuration directory",
    )
    _ensure_root_directory(path, label="origin TLS destination")


def _read_validated_regular_file(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    minimum_size: int,
    maximum_size: int,
    expected_sha256: str,
    label: str,
) -> ValidatedRegularFile:
    expected_hash = _validate_hex(expected_sha256, HEX_64, f"{label} SHA256")
    if not hasattr(os, "O_NOFOLLOW"):
        raise OriginTlsError("O_NOFOLLOW is required for origin TLS installation")
    # The deploy-owned inbox may contain attacker-controlled special files.
    # O_NONBLOCK ensures a FIFO cannot hold the global release lock before the
    # descriptor reaches the regular-file fstat gate below.  It is inert for
    # the regular certificate/key files accepted by this function.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OriginTlsError(f"{label} could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OriginTlsError(f"{label} is not a regular file")
        if (
            metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_nlink != 1
        ):
            raise OriginTlsError(f"{label} ownership, mode or link count is invalid")
        if metadata.st_size < minimum_size or metadata.st_size > maximum_size:
            raise OriginTlsError(f"{label} size is outside the allowed range")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, maximum_size + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > maximum_size:
                raise OriginTlsError(f"{label} exceeds the allowed size")
        if len(payload) != metadata.st_size:
            raise OriginTlsError(f"{label} changed while it was read")
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise OriginTlsError(f"{label} does not match its exact SHA256")
        return ValidatedRegularFile(
            path=path,
            payload=bytes(payload),
            device=metadata.st_dev,
            inode=metadata.st_ino,
            owner_uid=metadata.st_uid,
            owner_gid=metadata.st_gid,
            mode=stat.S_IMODE(metadata.st_mode),
            link_count=metadata.st_nlink,
            size=metadata.st_size,
        )
    finally:
        os.close(descriptor)


def _read_exact_regular_file(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    mode: int,
    minimum_size: int,
    maximum_size: int,
    expected_sha256: str,
    label: str,
) -> bytes:
    return _read_validated_regular_file(
        path,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        mode=mode,
        minimum_size=minimum_size,
        maximum_size=maximum_size,
        expected_sha256=expected_sha256,
        label=label,
    ).payload


def _consume_validated_file(validated: ValidatedRegularFile, *, label: str) -> None:
    try:
        current = validated.path.lstat()
    except OSError as exc:
        raise OriginTlsError(f"{label} changed before it could be consumed") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != validated.device
        or current.st_ino != validated.inode
        or current.st_uid != validated.owner_uid
        or current.st_gid != validated.owner_gid
        or stat.S_IMODE(current.st_mode) != validated.mode
        or current.st_nlink != validated.link_count
        or current.st_size != validated.size
    ):
        raise OriginTlsError(f"{label} was replaced or changed after validation")
    try:
        validated.path.unlink()
        _fsync_directory(validated.path.parent)
    except OSError as exc:
        raise OriginTlsError(f"{label} could not be removed from the inbox") from exc


def _write_root_file(directory: Path, name: str, payload: bytes) -> Path:
    if name not in {"origin-cert.pem", "origin-key.pem", "aop-ca.pem", "metadata.json"}:
        raise OriginTlsError("origin TLS installer refused an unknown destination file")
    destination = directory / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o400)
    except OSError as exc:
        raise OriginTlsError(f"could not create immutable origin TLS file: {name}") from exc
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o400)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OriginTlsError(f"short write while creating origin TLS file: {name}")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return destination


def _require_single_unencrypted_private_key(payload: bytes) -> None:
    if b"-----BEGIN ENCRYPTED PRIVATE KEY-----" in payload:
        raise OriginTlsError("origin private key must be unencrypted for unattended Caddy startup")
    key_labels = (b"PRIVATE KEY", b"EC PRIVATE KEY", b"RSA PRIVATE KEY")
    matched = [
        label
        for label in key_labels
        if payload.count(b"-----BEGIN " + label + b"-----") == 1
        and payload.count(b"-----END " + label + b"-----") == 1
    ]
    if len(matched) != 1 or payload.count(b"-----BEGIN ") != 1 or payload.count(b"-----END ") != 1:
        raise OriginTlsError("origin key upload must contain exactly one PEM private key")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _run_openssl(openssl: Path, arguments: list[str], *, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            [os.fspath(openssl), *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OriginTlsError("OpenSSL could not validate origin TLS material") from exc
    if completed.returncode != 0:
        raise OriginTlsError("OpenSSL rejected origin TLS material")
    return completed.stdout


def _verify_tls_material(bundle: Path, openssl: Path) -> None:
    certificate = bundle / "origin-cert.pem"
    private_key = bundle / "origin-key.pem"
    aop_ca = bundle / "aop-ca.pem"
    _run_openssl(
        openssl,
        ["x509", "-in", os.fspath(certificate), "-noout", "-checkhost", ORIGIN_HOSTNAME],
    )
    _run_openssl(
        openssl,
        ["x509", "-in", os.fspath(certificate), "-noout", "-checkend", "86400"],
    )
    aop_text = _run_openssl(
        openssl, ["x509", "-in", os.fspath(aop_ca), "-noout", "-text"]
    )
    if b"CA:TRUE" not in aop_text:
        raise OriginTlsError("AOP trust material is not a certificate authority")
    _run_openssl(
        openssl, ["x509", "-in", os.fspath(aop_ca), "-noout", "-checkend", "86400"]
    )
    _run_openssl(
        openssl,
        ["pkey", "-in", os.fspath(private_key), "-passin", "pass:", "-check", "-noout"],
    )
    certificate_pem = _run_openssl(
        openssl, ["x509", "-in", os.fspath(certificate), "-pubkey", "-noout"]
    )
    certificate_public_key = _run_openssl(
        openssl, ["pkey", "-pubin", "-outform", "DER"], input_bytes=certificate_pem
    )
    private_public_key = _run_openssl(
        openssl,
        [
            "pkey",
            "-in",
            os.fspath(private_key),
            "-passin",
            "pass:",
            "-pubout",
            "-outform",
            "DER",
        ],
    )
    if not certificate_public_key or certificate_public_key != private_public_key:
        raise OriginTlsError("origin private key does not match the Git-bound certificate")


def _metadata_payload(
    *,
    bundle_sha256: str,
    origin_cert_sha256: str,
    aop_ca_sha256: str,
    origin_key_sha256: str,
) -> bytes:
    document = {
        "schema_version": INSTALL_SCHEMA,
        "hostname": ORIGIN_HOSTNAME,
        "bundle_sha256": bundle_sha256,
        "origin_certificate_sha256": origin_cert_sha256,
        "aop_ca_sha256": aop_ca_sha256,
        "origin_private_key_sha256": origin_key_sha256,
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _validate_installed_bundle(
    bundle: Path,
    *,
    bundle_sha256: str,
    origin_cert_sha256: str,
    aop_ca_sha256: str,
    openssl: Path,
) -> str:
    _require_directory(bundle, owner_uid=0, owner_gid=0, mode=0o700, label="origin TLS bundle")
    metadata_path = bundle / "metadata.json"
    try:
        metadata_stat = metadata_path.lstat()
        if (
            not stat.S_ISREG(metadata_stat.st_mode)
            or metadata_stat.st_uid != 0
            or metadata_stat.st_gid != 0
            or stat.S_IMODE(metadata_stat.st_mode) != 0o400
            or metadata_stat.st_nlink != 1
            or metadata_stat.st_size < 2
            or metadata_stat.st_size > 4096
        ):
            raise OriginTlsError("installed origin TLS metadata is unsafe")
        metadata = json.loads(metadata_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OriginTlsError("installed origin TLS metadata is invalid") from exc
    expected_keys = {
        "schema_version",
        "hostname",
        "bundle_sha256",
        "origin_certificate_sha256",
        "aop_ca_sha256",
        "origin_private_key_sha256",
    }
    if not isinstance(metadata, dict) or set(metadata) != expected_keys:
        raise OriginTlsError("installed origin TLS metadata has unexpected fields")
    key_sha256 = _validate_hex(
        str(metadata.get("origin_private_key_sha256") or ""),
        HEX_64,
        "installed origin key SHA256",
    )
    expected_metadata = {
        "schema_version": INSTALL_SCHEMA,
        "hostname": ORIGIN_HOSTNAME,
        "bundle_sha256": bundle_sha256,
        "origin_certificate_sha256": origin_cert_sha256,
        "aop_ca_sha256": aop_ca_sha256,
        "origin_private_key_sha256": key_sha256,
    }
    if metadata != expected_metadata:
        raise OriginTlsError("installed origin TLS metadata does not match the release")
    _read_exact_regular_file(
        bundle / "origin-cert.pem",
        owner_uid=0,
        owner_gid=0,
        mode=0o400,
        minimum_size=MIN_CERT_BYTES,
        maximum_size=MAX_CERT_BYTES,
        expected_sha256=origin_cert_sha256,
        label="installed origin certificate",
    )
    _read_exact_regular_file(
        bundle / "aop-ca.pem",
        owner_uid=0,
        owner_gid=0,
        mode=0o400,
        minimum_size=MIN_CERT_BYTES,
        maximum_size=MAX_CERT_BYTES,
        expected_sha256=aop_ca_sha256,
        label="installed AOP CA",
    )
    installed_private_key = _read_exact_regular_file(
        bundle / "origin-key.pem",
        owner_uid=0,
        owner_gid=0,
        mode=0o400,
        minimum_size=MIN_KEY_BYTES,
        maximum_size=MAX_KEY_BYTES,
        expected_sha256=key_sha256,
        label="installed origin private key",
    )
    _require_single_unencrypted_private_key(installed_private_key)
    _verify_tls_material(bundle, openssl)
    return key_sha256


def _install_new_bundle(
    *,
    releases: Path,
    final_bundle: Path,
    bundle_sha256: str,
    origin_cert_sha256: str,
    aop_ca_sha256: str,
    origin_certificate: bytes,
    aop_ca: bytes,
    uploaded_private_key: ValidatedRegularFile,
    origin_key_sha256: str,
    openssl: Path,
) -> Path:
    private_key = uploaded_private_key.payload
    _require_single_unencrypted_private_key(private_key)
    candidate = Path(
        tempfile.mkdtemp(prefix=f".{bundle_sha256}.candidate-", dir=releases)
    )
    try:
        os.chown(candidate, 0, 0)
        os.chmod(candidate, 0o700)
        _write_root_file(candidate, "origin-cert.pem", origin_certificate)
        _write_root_file(candidate, "origin-key.pem", private_key)
        _write_root_file(candidate, "aop-ca.pem", aop_ca)
        _write_root_file(
            candidate,
            "metadata.json",
            _metadata_payload(
                bundle_sha256=bundle_sha256,
                origin_cert_sha256=origin_cert_sha256,
                aop_ca_sha256=aop_ca_sha256,
                origin_key_sha256=origin_key_sha256,
            ),
        )
        _fsync_directory(candidate)
        _verify_tls_material(candidate, openssl)
        os.rename(candidate, final_bundle)
        _fsync_directory(releases)
    except Exception:
        if candidate.exists() and not candidate.is_symlink():
            shutil.rmtree(candidate)
        raise
    installed_key_sha256 = _validate_installed_bundle(
        final_bundle,
        bundle_sha256=bundle_sha256,
        origin_cert_sha256=origin_cert_sha256,
        aop_ca_sha256=aop_ca_sha256,
        openssl=openssl,
    )
    if installed_key_sha256 != origin_key_sha256:
        raise OriginTlsError("installed origin key identity changed after installation")
    _consume_validated_file(
        uploaded_private_key,
        label="uploaded origin private key",
    )
    return final_bundle


def install_origin_tls(
    *,
    release_sha: str,
    bundle_sha256: str,
    origin_cert_sha256: str,
    aop_ca_sha256: str,
    paths: RuntimePaths = RuntimePaths(),
) -> Path:
    import pwd  # Linux-only production lookup; kept local so identity helpers stay portable.

    release_identity = _validate_hex(release_sha, HEX_40, "release SHA")
    certificate_hash = _validate_hex(
        origin_cert_sha256, HEX_64, "origin certificate SHA256"
    )
    aop_hash = _validate_hex(aop_ca_sha256, HEX_64, "AOP CA SHA256")
    bundle_identity = _validate_hex(bundle_sha256, HEX_64, "origin TLS bundle SHA256")
    if bundle_identity != origin_tls_bundle_sha256(certificate_hash, aop_hash):
        raise OriginTlsError("origin TLS bundle identity does not match its public certificates")

    try:
        deploy = pwd.getpwnam("deploy")
    except KeyError as exc:
        raise OriginTlsError("the dedicated deploy account is missing") from exc
    if deploy.pw_gid <= 0:
        raise OriginTlsError("the dedicated deploy account has an invalid primary group")
    _require_directory(
        paths.inbox,
        owner_uid=deploy.pw_uid,
        owner_gid=deploy.pw_gid,
        mode=0o700,
        label="origin key inbox",
    )
    _prepare_destination_root(paths.destination_root)
    releases = paths.destination_root / "releases"
    _ensure_root_directory(releases, label="origin TLS releases directory")

    configuration = paths.configuration_root / release_identity
    _require_directory(
        configuration,
        owner_uid=0,
        owner_gid=0,
        mode=0o555,
        label="immutable release configuration",
    )
    origin_certificate = _read_exact_regular_file(
        configuration / "config" / "origin-edge" / "origin-cert.pem",
        owner_uid=0,
        owner_gid=0,
        mode=0o444,
        minimum_size=MIN_CERT_BYTES,
        maximum_size=MAX_CERT_BYTES,
        expected_sha256=certificate_hash,
        label="Git-bound origin certificate",
    )
    aop_ca = _read_exact_regular_file(
        configuration / "config" / "origin-edge" / "aop-ca.pem",
        owner_uid=0,
        owner_gid=0,
        mode=0o444,
        minimum_size=MIN_CERT_BYTES,
        maximum_size=MAX_CERT_BYTES,
        expected_sha256=aop_hash,
        label="Git-bound AOP CA",
    )

    final_bundle = releases / bundle_identity
    key_pattern = re.compile(
        rf"^origin-key-{re.escape(release_identity)}-([0-9a-f]{{64}})\.pem$"
    )
    try:
        matching_keys = sorted(
            (entry.name, match.group(1))
            for entry in os.scandir(paths.inbox)
            if (match := key_pattern.fullmatch(entry.name)) is not None
        )
    except OSError as exc:
        raise OriginTlsError("origin key inbox could not be enumerated") from exc
    if len(matching_keys) > 1:
        raise OriginTlsError("origin key inbox contains more than one key for this release")

    if final_bundle.exists() or final_bundle.is_symlink():
        installed_key_sha256 = _validate_installed_bundle(
            final_bundle,
            bundle_sha256=bundle_identity,
            origin_cert_sha256=certificate_hash,
            aop_ca_sha256=aop_hash,
            openssl=paths.openssl,
        )
        if matching_keys:
            key_name, uploaded_key_sha256 = matching_keys[0]
            uploaded_private_key = _read_validated_regular_file(
                paths.inbox / key_name,
                owner_uid=deploy.pw_uid,
                owner_gid=deploy.pw_gid,
                mode=0o600,
                minimum_size=MIN_KEY_BYTES,
                maximum_size=MAX_KEY_BYTES,
                expected_sha256=uploaded_key_sha256,
                label="uploaded origin private key",
            )
            if uploaded_key_sha256 != installed_key_sha256:
                raise OriginTlsError("uploaded origin key differs from the immutable installed key")
            _consume_validated_file(
                uploaded_private_key,
                label="uploaded origin private key",
            )
        return final_bundle

    if len(matching_keys) != 1:
        raise OriginTlsError("one exact origin private key is required for a new TLS bundle")
    key_name, key_sha256 = matching_keys[0]
    uploaded_private_key = _read_validated_regular_file(
        paths.inbox / key_name,
        owner_uid=deploy.pw_uid,
        owner_gid=deploy.pw_gid,
        mode=0o600,
        minimum_size=MIN_KEY_BYTES,
        maximum_size=MAX_KEY_BYTES,
        expected_sha256=key_sha256,
        label="uploaded origin private key",
    )
    return _install_new_bundle(
        releases=releases,
        final_bundle=final_bundle,
        bundle_sha256=bundle_identity,
        origin_cert_sha256=certificate_hash,
        aop_ca_sha256=aop_hash,
        origin_certificate=origin_certificate,
        aop_ca=aop_ca,
        uploaded_private_key=uploaded_private_key,
        origin_key_sha256=key_sha256,
        openssl=paths.openssl,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--origin-cert-sha256", required=True)
    parser.add_argument("--aop-ca-sha256", required=True)
    args = parser.parse_args()
    try:
        installed = install_origin_tls(
            release_sha=args.release_sha,
            bundle_sha256=args.bundle_sha256,
            origin_cert_sha256=args.origin_cert_sha256,
            aop_ca_sha256=args.aop_ca_sha256,
        )
    except OriginTlsError as exc:
        parser.error(str(exc))
    print(installed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
