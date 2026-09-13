"""Optional statistics switch over the immutable, default-off frontend module."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Response

_DISABLED = b"  enabled: false,"
_ENABLED = b"  enabled: true,"
_FALLBACK = b"export default Object.freeze({enabled:false});\n"
_MAX_MODULE_BYTES = 16 * 1024


def statistics_module(path: Path, enabled: bool) -> bytes:
    """Keep endpoints/catalog bytes fixed; missing or unexpected bundles stay off."""
    try:
        with path.open("rb") as stream:
            source = stream.read(_MAX_MODULE_BYTES + 1)
    except OSError:
        return _FALLBACK
    if len(source) > _MAX_MODULE_BYTES or source.count(_DISABLED) != 1:
        return _FALLBACK
    return source.replace(_DISABLED, _ENABLED, 1) if enabled else source


def install_statistics_config(app: FastAPI, frontend_path: Path) -> None:
    # Read only on this optional request. No startup, health, database, network
    # or mounted-directory dependency is introduced into the business service.
    @app.get("/statistics/config.mjs", include_in_schema=False)
    def statistics_config() -> Response:
        return Response(
            statistics_module(
                frontend_path / "statistics" / "config.mjs",
                os.getenv("PUBLIC_STATISTICS_ENABLED") == "true",
            ),
            media_type="text/javascript",
            headers={"Cache-Control": "no-store, max-age=0", "X-Content-Type-Options": "nosniff"},
        )
