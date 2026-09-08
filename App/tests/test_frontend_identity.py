from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.snow_app import frontend_identity
from backend.snow_app.frontend_identity import FrontendIdentityError, read_frontend_identity
from backend.snow_app.public_main import create_app
from backend.snow_app.public_store import PublicStore
from tests.test_public_security import _settings


@pytest.fixture
def identity_path(tmp_path, monkeypatch):
    path = tmp_path / "frontend-identity.json"
    monkeypatch.setattr(frontend_identity, "IDENTITY_PATH", path)
    for name in ("APP_REVISION", "GIT_SHA", "APP_BUILD_TIME"):
        monkeypatch.delenv(name, raising=False)
    return path


def identity(track="compat"):
    return {
        "schema_version": "project-snow-frontend-identity-1",
        "track": track,
        "version": "compat-096-r1" if track == "compat" else "0.10.0-rc.1",
        "bundle_sha256": "a" * 64,
    }


def test_missing_identity_is_development_only(identity_path):
    assert read_frontend_identity(required=False) is None
    with pytest.raises(FrontendIdentityError, match="missing"):
        read_frontend_identity(required=True)
    assert not identity_path.exists()


@pytest.mark.parametrize("track", ["compat", "current"])
def test_image_identity_returns_only_public_binding(identity_path, track):
    value = identity(track)
    identity_path.write_text(json.dumps(value), encoding="utf-8")
    result = read_frontend_identity(required=True)
    assert result.public_value() == {key: value[key] for key in ("track", "version", "bundle_sha256")}


@pytest.mark.parametrize("changes", [
    {"schema_version": "unknown"},
    {"track": "development"},
    {"track": ["compat"]},
    {"version": ""},
    {"version": "a" * 65},
    {"version": "../current"},
    {"version": False},
    {"bundle_sha256": "A" * 64},
    {"bundle_sha256": "a" * 63},
    {"bundle_sha256": 123},
    {"environment_selector": "PUBLIC_UI_TRACK"},
])
@pytest.mark.parametrize("required", [False, True])
def test_invalid_identity_never_silently_becomes_development(identity_path, changes, required):
    identity_path.write_text(json.dumps({**identity(), **changes}), encoding="utf-8")
    with pytest.raises(FrontendIdentityError):
        read_frontend_identity(required=required)


@pytest.mark.parametrize("payload", [
    b"{}", b"[]", b"{", b"\xff", b" " * 4097,
    b'{"track":"compat","track":"current"}',
])
def test_malformed_or_unbounded_identity_is_rejected(identity_path, payload):
    identity_path.write_bytes(payload)
    with pytest.raises(FrontendIdentityError):
        read_frontend_identity(required=True)


def test_directory_cannot_substitute_for_identity(identity_path):
    identity_path.mkdir()
    with pytest.raises(FrontendIdentityError, match="regular file"):
        read_frontend_identity(required=True)


def test_identity_cannot_redirect_through_a_symlink(identity_path):
    target = identity_path.with_name("elsewhere.json")
    target.write_text(json.dumps(identity()), encoding="utf-8")
    try:
        identity_path.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    with pytest.raises(FrontendIdentityError, match="regular file"):
        read_frontend_identity(required=True)


@pytest.mark.parametrize("payload", [None, "{}"])
def test_release_refuses_startup_before_store_or_services_without_identity(
    identity_path, monkeypatch, payload,
):
    if payload is not None:
        identity_path.write_text(payload, encoding="utf-8")
    monkeypatch.setenv("APP_REVISION", "a" * 40)
    with patch("backend.snow_app.public_main.PublicStore") as store:
        with patch("backend.snow_app.public_main.PublicChatService") as service:
            with pytest.raises(FrontendIdentityError):
                create_app(_settings(), SimpleNamespace())
    store.assert_not_called()
    service.assert_not_called()


@pytest.mark.parametrize("track", [None, "compat", "current"])
def test_build_info_snapshots_image_identity_and_never_selects_ui_from_environment(
    identity_path, monkeypatch, track,
):
    value = identity(track) if track else None
    if value:
        identity_path.write_text(json.dumps(value), encoding="utf-8")
        monkeypatch.setenv("APP_REVISION", "b" * 40)
        monkeypatch.setenv("APP_BUILD_TIME", "2026-09-08T00:00:00Z")
    monkeypatch.setenv("PUBLIC_UI_TRACK", "current")
    monkeypatch.setenv("FRONTEND_IDENTITY_PATH", "/untrusted/frontend-identity.json")
    service = SimpleNamespace(
        media=SimpleNamespace(verify=lambda **_kwargs: {}),
        stickers=SimpleNamespace(verify=lambda **_kwargs: {}),
        provider_client=object(),
        close=lambda: None,
    )
    app = create_app(_settings(), SimpleNamespace(), PublicStore(""), service)
    # Identity and build metadata belong to the started image, not later changes
    # to environment variables or the on-disk descriptor.
    identity_path.write_text(json.dumps(identity("current")), encoding="utf-8")
    monkeypatch.setenv("APP_REVISION", "c" * 40)
    monkeypatch.setenv("APP_BUILD_TIME", "2026-09-09T00:00:00Z")
    with TestClient(app) as client:
        response = client.get("/public/v1/build-info")
    assert response.status_code == 200
    result = response.json()
    assert result["frontend"] == (
        {key: value[key] for key in ("track", "version", "bundle_sha256")} if value else None
    )
    assert result["revision"] == ("b" * 40 if value else None)
    assert result["build_time"] == ("2026-09-08T00:00:00Z" if value else None)
    assert result["api_schema"] == "public-v1"
    assert result["state_schema"] == "public-state-2"
