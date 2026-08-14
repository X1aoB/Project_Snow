from __future__ import annotations

import os
from pathlib import Path


def load_required_secret(name: str) -> str:
    """Load a required value from ``NAME_FILE`` or the process environment."""

    file_path = str(os.getenv(f"{name}_FILE") or "").strip()
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Unable to read configured secret file for {name}") from exc
        if not value:
            raise RuntimeError(f"Configured secret file for {name} is empty")
        return value

    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} or {name}_FILE must be configured")
    return value
