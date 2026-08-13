"""Runtime configuration with safe local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional for production env-only runs
    load_dotenv = None

try:  # pragma: no cover - exercised on Windows with the local credential vault
    import keyring
except ImportError:  # pragma: no cover - optional for env-only deployments
    keyring = None


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent


@dataclass(frozen=True)
class Settings:
    data_root: Path
    runtime_root: Path
    chat_enabled: bool
    embedding_model: str
    allowed_origins: list[str]
    # The MVP conversation harness is intentionally separate from the
    # product-stage /api/v1/chat gate.  It is disabled unless explicitly
    # enabled for local validation.
    mvp_chat_enabled: bool = False
    mvp_chat_provider: str = "disabled"
    mvp_chat_base_url: str = ""
    mvp_chat_api_key: str = ""
    mvp_chat_model: str = ""
    mvp_chat_timeout_seconds: float = 120.0
    mvp_chat_credential_ref: str = ""

    @classmethod
    def from_environment(cls) -> "Settings":
        # Local development keeps provider settings in App/.env.  Explicit
        # process environment variables still win because python-dotenv does
        # not override values that are already present.
        if load_dotenv is not None:
            load_dotenv(PACKAGE_ROOT / ".env", override=False)
        data_root = Path(os.getenv("DATA_ROOT") or (PROJECT_ROOT / "Data")).resolve()
        runtime_root = Path(os.getenv("APP_RUNTIME") or (PACKAGE_ROOT / "runtime")).resolve()
        origins = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8080").split(",") if origin.strip()]
        credential_ref = os.getenv("MVP_CHAT_CREDENTIAL_REF", "").strip()
        api_key = os.getenv("MVP_CHAT_API_KEY", "")
        if not api_key and credential_ref and keyring is not None:
            try:
                api_key = str(keyring.get_password("ProjectSnow", credential_ref) or "")
            except Exception:
                # Read-only mode remains available when the OS credential
                # backend cannot be reached.
                api_key = ""
        return cls(
            data_root=data_root,
            runtime_root=runtime_root,
            chat_enabled=os.getenv("CHAT_ENABLED", "false").lower() == "true",
            embedding_model=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
            allowed_origins=origins,
            mvp_chat_enabled=os.getenv("MVP_CHAT_ENABLED", "false").lower() == "true",
            mvp_chat_provider=os.getenv(
                "MVP_CHAT_PROVIDER",
                "openai-compatible"
                if (
                    os.getenv("MVP_CHAT_BASE_URL")
                    or os.getenv("DASHSCOPE_BASE_URL")
                    or os.getenv("OPENAI_COMPATIBLE_BASE_URL")
                )
                else "disabled",
            ),
            mvp_chat_base_url=os.getenv(
                "MVP_CHAT_BASE_URL",
                os.getenv("DASHSCOPE_BASE_URL", os.getenv("OPENAI_COMPATIBLE_BASE_URL", "")),
            ),
            mvp_chat_api_key=api_key
            or os.getenv("DASHSCOPE_API_KEY", os.getenv("OPENAI_COMPATIBLE_API_KEY", "")),
            mvp_chat_model=os.getenv(
                "MVP_CHAT_MODEL",
                os.getenv("OPENAI_COMPATIBLE_MODEL", "qwen3.7-max"),
            ),
            mvp_chat_timeout_seconds=float(os.getenv("MVP_CHAT_TIMEOUT_SECONDS", "120")),
            mvp_chat_credential_ref=credential_ref,
        )
