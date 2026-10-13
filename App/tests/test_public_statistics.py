from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.snow_app.public_statistics import install_statistics_config, statistics_module

SOURCE = b'''export default Object.freeze({
  enabled: false,
  app: "project_snow",
  endpoint: "https://stats.xiaob.dev/analytics/v1/events",
  paths: ["/"],
  characters: ["synthetic-character"],
});
'''


@pytest.fixture
def module(tmp_path):
    root = tmp_path / "frontend"
    (root / "statistics").mkdir(parents=True)
    (root / "statistics" / "config.mjs").write_bytes(SOURCE)
    return root


@pytest.mark.parametrize("value", [None, "", "false", "True", "TRUE", "1", " true", "true "])
def test_runtime_switch_defaults_off_and_requires_exact_true(module, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PUBLIC_STATISTICS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PUBLIC_STATISTICS_ENABLED", value)
    app = FastAPI()
    install_statistics_config(app, module)
    with TestClient(app) as client:
        response = client.get("/statistics/config.mjs")
    assert response.status_code == 200 and response.content == SOURCE
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["content-type"].startswith("text/javascript")


def test_enabled_module_changes_only_boolean_and_keeps_bundle_immutable(module, monkeypatch):
    monkeypatch.setenv("PUBLIC_STATISTICS_ENABLED", "true")
    app = FastAPI()
    install_statistics_config(app, module)
    with TestClient(app) as client:
        response = client.get("/statistics/config.mjs")
        assert response.content == SOURCE.replace(b"  enabled: false,", b"  enabled: true,")
        monkeypatch.setenv("PUBLIC_STATISTICS_ENABLED", "false")
        assert client.get("/statistics/config.mjs").content == SOURCE
    assert (module / "statistics" / "config.mjs").read_bytes() == SOURCE


@pytest.mark.parametrize("data", [b"", b"export default {enabled:true}", SOURCE + SOURCE, b"x" * 16385])
def test_unexpected_template_fails_closed_without_business_dependency(module, monkeypatch, data):
    (module / "statistics" / "config.mjs").write_bytes(data)
    monkeypatch.setenv("PUBLIC_STATISTICS_ENABLED", "true")
    app = FastAPI()
    install_statistics_config(app, module)
    app.get("/business-health")(lambda: {"ok": True})
    with TestClient(app) as client:
        assert client.get("/statistics/config.mjs").content == b"export default Object.freeze({enabled:false});\n"
        assert client.get("/business-health").json() == {"ok": True}


def test_statistics_directory_absent_does_not_prevent_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_STATISTICS_ENABLED", "true")
    app = FastAPI()
    install_statistics_config(app, tmp_path / "does-not-exist")
    with TestClient(app) as client:
        assert client.get("/statistics/config.mjs").status_code == 200
        assert b"enabled:false" in client.get("/statistics/config.mjs").content


def test_current_release_template_has_one_default_off_switch():
    path = Path(__file__).resolve().parents[1] / "public_frontend" / "statistics" / "config.mjs"
    assert statistics_module(path, False) == path.read_bytes()
    assert statistics_module(path, True) == path.read_bytes().replace(b"  enabled: false,", b"  enabled: true,")
