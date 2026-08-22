from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import install_origin_tls as tls


PRIVATE_KEY_FIXTURE = (
    b"-----BEGIN " b"PRIVATE KEY-----\n"
    b"contract-test-placeholder\n"
    b"-----END " b"PRIVATE KEY-----\n"
)


def validated_file(path: Path) -> tls.ValidatedRegularFile:
    metadata = path.lstat()
    return tls.ValidatedRegularFile(
        path=path,
        payload=path.read_bytes(),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=metadata.st_uid,
        owner_gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
        link_count=metadata.st_nlink,
        size=metadata.st_size,
    )


def write_test_root_file(directory: Path, name: str, payload: bytes) -> Path:
    destination = directory / name
    destination.write_bytes(payload)
    return destination


class OriginTlsInstallerTests(TestCase):
    def test_first_install_creates_missing_destination_after_parent_gate(self) -> None:
        with TemporaryDirectory() as directory:
            system_root = Path(directory) / "project-snow"
            system_root.mkdir()
            destination = system_root / "origin-edge"

            with (
                patch.object(tls, "_require_root_controlled_directory") as parent_gate,
                patch.object(tls.os, "chown", create=True),
                patch.object(tls, "_fsync_directory"),
                patch.object(tls, "_require_directory") as destination_gate,
            ):
                tls._prepare_destination_root(destination)

            self.assertTrue(destination.is_dir())
            parent_gate.assert_called_once_with(
                system_root,
                label="Project Snow system configuration directory",
            )
            destination_gate.assert_called_once_with(
                destination,
                owner_uid=0,
                owner_gid=0,
                mode=0o700,
                label="origin TLS destination",
            )

    def test_validation_failure_retains_uploaded_private_key(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir()
            upload = root / "origin-key.pem"
            upload.write_bytes(PRIVATE_KEY_FIXTURE)
            uploaded = validated_file(upload)
            key_sha256 = sha256(PRIVATE_KEY_FIXTURE).hexdigest()
            final_bundle = releases / ("a" * 64)

            with (
                patch.object(tls.os, "chown", create=True),
                patch.object(tls, "_write_root_file", side_effect=write_test_root_file),
                patch.object(tls, "_fsync_directory"),
                patch.object(
                    tls,
                    "_verify_tls_material",
                    side_effect=tls.OriginTlsError("expected validation failure"),
                ),
            ):
                with self.assertRaisesRegex(tls.OriginTlsError, "expected validation"):
                    tls._install_new_bundle(
                        releases=releases,
                        final_bundle=final_bundle,
                        bundle_sha256="a" * 64,
                        origin_cert_sha256="b" * 64,
                        aop_ca_sha256="c" * 64,
                        origin_certificate=b"public origin certificate",
                        aop_ca=b"public AOP CA",
                        uploaded_private_key=uploaded,
                        origin_key_sha256=key_sha256,
                        openssl=Path("/usr/bin/openssl"),
                    )

            self.assertTrue(upload.is_file())
            self.assertFalse(final_bundle.exists())

    def test_success_consumes_key_only_after_installed_bundle_validation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            releases = root / "releases"
            releases.mkdir()
            upload = root / "origin-key.pem"
            upload.write_bytes(PRIVATE_KEY_FIXTURE)
            uploaded = validated_file(upload)
            key_sha256 = sha256(PRIVATE_KEY_FIXTURE).hexdigest()
            final_bundle = releases / ("a" * 64)

            def validate_after_rename(*args: object, **kwargs: object) -> str:
                self.assertTrue(final_bundle.is_dir())
                self.assertTrue(upload.is_file())
                return key_sha256

            with (
                patch.object(tls.os, "chown", create=True),
                patch.object(tls, "_write_root_file", side_effect=write_test_root_file),
                patch.object(tls, "_fsync_directory"),
                patch.object(tls, "_verify_tls_material"),
                patch.object(
                    tls,
                    "_validate_installed_bundle",
                    side_effect=validate_after_rename,
                ),
            ):
                installed = tls._install_new_bundle(
                    releases=releases,
                    final_bundle=final_bundle,
                    bundle_sha256="a" * 64,
                    origin_cert_sha256="b" * 64,
                    aop_ca_sha256="c" * 64,
                    origin_certificate=b"public origin certificate",
                    aop_ca=b"public AOP CA",
                    uploaded_private_key=uploaded,
                    origin_key_sha256=key_sha256,
                    openssl=Path("/usr/bin/openssl"),
                )

            self.assertEqual(installed, final_bundle)
            self.assertTrue(final_bundle.is_dir())
            self.assertFalse(upload.exists())

    def test_inode_replacement_is_not_deleted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            upload = root / "origin-key.pem"
            upload.write_bytes(PRIVATE_KEY_FIXTURE)
            uploaded = validated_file(upload)
            original = root / "original-key.pem"
            upload.rename(original)
            replacement = b"replacement must survive"
            upload.write_bytes(replacement)

            with self.assertRaisesRegex(tls.OriginTlsError, "replaced or changed"):
                tls._consume_validated_file(
                    uploaded,
                    label="uploaded origin private key",
                )

            self.assertEqual(upload.read_bytes(), replacement)
            self.assertTrue(original.is_file())
