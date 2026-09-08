"""Read the identity of the single frontend bundled into a public image."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

IDENTITY_PATH = Path(__file__).resolve().parents[2] / "frontend-identity.json"
IDENTITY_SCHEMA = "project-snow-frontend-identity-1"
MAX_IDENTITY_BYTES = 4096
IDENTITY_FIELDS = {"schema_version", "track", "version", "bundle_sha256"}


class FrontendIdentityError(RuntimeError):
    """The image cannot truthfully identify its bundled frontend."""


@dataclass(frozen=True)
class FrontendIdentity:
    track: str
    version: str
    bundle_sha256: str

    def public_value(self) -> dict[str, str]:
        return {
            "track": self.track,
            "version": self.version,
            "bundle_sha256": self.bundle_sha256,
        }


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrontendIdentityError("Frontend release identity has duplicate fields")
        result[key] = value
    return result


def read_frontend_identity(*, required: bool) -> FrontendIdentity | None:
    """Read once at startup; environment variables never choose the UI track.

    The build validates the complete static bundle before recording this identity.
    Deployment binds these fields to the signed manifest and actual image digest.
    A source checkout without an identity is explicitly a development frontend.
    """
    try:
        before = IDENTITY_PATH.lstat()
    except FileNotFoundError:
        if required:
            raise FrontendIdentityError("Frontend release identity is missing") from None
        return None
    except OSError as exc:
        raise FrontendIdentityError("Frontend release identity is unreadable") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_IDENTITY_BYTES:
        raise FrontendIdentityError("Frontend release identity must be a bounded regular file")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(IDENTITY_PATH, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise FrontendIdentityError("Frontend release identity changed while opening")
            payload = source.read(MAX_IDENTITY_BYTES + 1)
    except OSError as exc:
        raise FrontendIdentityError("Frontend release identity is unreadable") from exc
    if len(payload) > MAX_IDENTITY_BYTES:
        raise FrontendIdentityError("Frontend release identity exceeds its size limit")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (ValueError, UnicodeError) as exc:
        raise FrontendIdentityError("Frontend release identity is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != IDENTITY_FIELDS:
        raise FrontendIdentityError("Frontend release identity has unexpected fields")
    if (
        value["schema_version"] != IDENTITY_SCHEMA
        or value["track"] not in ("compat", "current")
        or not isinstance(value["version"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value["version"])
        or not isinstance(value["bundle_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["bundle_sha256"])
    ):
        raise FrontendIdentityError("Frontend release identity has invalid values")
    return FrontendIdentity(value["track"], value["version"], value["bundle_sha256"])
