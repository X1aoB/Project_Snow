"""Runtime configuration with safe local defaults."""

from __future__ import annotations

import os
import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

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


def _secret_value(name: str, default: str = "") -> str:
    """Read a secret from an env value or an explicitly configured file."""

    file_path = str(os.getenv(f"{name}_FILE") or "").strip()
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError:
            return default
    return str(os.getenv(name) or default).strip()


def _public_database_url() -> str:
    """Return either the full API DSN or a least-privilege component DSN.

    Public API and admin containers receive a root-owned full DSN. Narrow
    workers receive only a password plus non-secret connection components so
    they cannot inherit the application's database role by accident.
    """

    configured = _secret_value("PUBLIC_DATABASE_URL")
    if configured:
        return configured
    password = _secret_value("PUBLIC_DATABASE_PASSWORD")
    if not password:
        return ""
    user = str(os.getenv("PUBLIC_DATABASE_USER") or "project_snow").strip()
    host = str(os.getenv("PUBLIC_DATABASE_HOST") or "postgres").strip()
    port = str(os.getenv("PUBLIC_DATABASE_PORT") or "5432").strip()
    database = str(os.getenv("PUBLIC_DATABASE_NAME") or "project_snow").strip()
    if not user or not host or not database or not port.isdigit():
        return ""
    return (
        "postgresql+psycopg://"
        f"{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(database, safe='')}"
    )


def _decode_32_byte_key(value: str) -> bytes | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    return decoded if len(decoded) == 32 else None


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


@dataclass(frozen=True)
class PublicSettings:
    app_version: str
    data_version: str
    database_url: str
    public_origin: str
    development_origins: tuple[str, ...]
    turnstile_site_key: str
    turnstile_secret: str
    credential_key: bytes | None
    state_hmac_key: bytes | None
    ip_hmac_key: bytes | None
    qq_key: bytes | None
    admin_token: str
    enabled_providers: tuple[str, ...]
    allow_insecure_dev: bool
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str
    embedding_url: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    auto_create_schema: bool = False
    trust_proxy_headers: bool = False
    media_version: str = "2026.08.19.avatar.1"
    media_root: Path = Path("/srv/project-snow/media/current")
    experience_notice_version: str = "0.9.2"
    arrival_probability: float = 0.5
    sticker_version: str = "2026.08.19.sticker.1"
    sticker_root: Path = Path("/srv/project-snow/media/stickers/current")
    feedback_email_to: str = "admin@xiaob.dev"
    feedback_email_from: str = ""
    feedback_smtp_host: str = ""
    feedback_smtp_port: int = 465
    feedback_smtp_username: str = ""
    feedback_smtp_password: str = ""
    state_hmac_previous_key: bytes | None = None
    state_key_id: str = "2026-08-19"
    state_previous_key_id: str = ""
    turnstile_hostname: str = "snow.xiaob.dev"
    turnstile_max_age_seconds: int = 300
    privacy_policy_version: str = "0.9.2"
    privacy_effective_at: str = "2026-08-20"
    attribution_url: str = "/public/v1/attributions"
    max_provider_calls_per_action: int = 2
    byok_lifetime_hours: int = 12

    @classmethod
    def from_environment(cls) -> "PublicSettings":
        if load_dotenv is not None:
            load_dotenv(PACKAGE_ROOT / ".env", override=False)
        providers = tuple(
            value.strip().casefold()
            # Adapters exist for every supported vendor, but a vendor must not
            # appear in the public UI until its real-key smoke test succeeds.
            for value in os.getenv("PUBLIC_ENABLED_PROVIDERS", "").split(",")
            if value.strip()
        )
        development_origins = tuple(
            value.strip()
            for value in os.getenv(
                "PUBLIC_DEVELOPMENT_ORIGINS",
                "http://localhost:8080,http://127.0.0.1:8080,http://localhost:8000,http://127.0.0.1:8000",
            ).split(",")
            if value.strip()
        )
        try:
            arrival_probability = float(os.getenv("PUBLIC_ARRIVAL_PROBABILITY", "0.5"))
        except (TypeError, ValueError):
            arrival_probability = 0.5
        try:
            feedback_smtp_port = int(os.getenv("PUBLIC_FEEDBACK_SMTP_PORT", "465"))
        except (TypeError, ValueError):
            feedback_smtp_port = 465
        try:
            turnstile_max_age_seconds = int(
                os.getenv("PUBLIC_TURNSTILE_MAX_AGE_SECONDS", "300")
            )
        except (TypeError, ValueError):
            turnstile_max_age_seconds = 300
        try:
            max_provider_calls_per_action = int(
                os.getenv("PUBLIC_MAX_PROVIDER_CALLS_PER_ACTION", "2")
            )
        except (TypeError, ValueError):
            max_provider_calls_per_action = 2
        try:
            byok_lifetime_hours = int(os.getenv("PUBLIC_BYOK_LIFETIME_HOURS", "12"))
        except (TypeError, ValueError):
            byok_lifetime_hours = 12
        return cls(
            app_version=os.getenv("PUBLIC_APP_VERSION", "0.9.2"),
            data_version=os.getenv("PUBLIC_DATA_VERSION", "local-development"),
            database_url=_public_database_url(),
            public_origin=os.getenv("PUBLIC_ORIGIN", "https://snow.xiaob.dev").rstrip("/"),
            development_origins=development_origins,
            turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY", ""),
            turnstile_secret=_secret_value("TURNSTILE_SECRET"),
            credential_key=_decode_32_byte_key(_secret_value("PUBLIC_CREDENTIAL_KEY")),
            state_hmac_key=_decode_32_byte_key(_secret_value("PUBLIC_STATE_HMAC_KEY")),
            ip_hmac_key=_decode_32_byte_key(_secret_value("PUBLIC_IP_HMAC_KEY")),
            qq_key=_decode_32_byte_key(_secret_value("PUBLIC_QQ_KEY")),
            admin_token=_secret_value("PUBLIC_ADMIN_TOKEN"),
            enabled_providers=providers,
            allow_insecure_dev=os.getenv("PUBLIC_ALLOW_INSECURE_DEV", "false").casefold() == "true",
            qdrant_url=os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/"),
            qdrant_collection=os.getenv("QDRANT_COLLECTION", "project_snow_documents"),
            qdrant_api_key=_secret_value("QDRANT_API_KEY"),
            embedding_url=os.getenv("EMBEDDING_URL", "http://embedding:80").rstrip("/"),
            neo4j_uri=os.getenv("NEO4J_URI", "bolt://neo4j:7687"),
            neo4j_user=os.getenv("NEO4J_USER", "neo4j"),
            neo4j_password=_secret_value("NEO4J_PASSWORD"),
            auto_create_schema=os.getenv("PUBLIC_AUTO_CREATE_SCHEMA", "false").casefold() == "true",
            trust_proxy_headers=os.getenv("PUBLIC_TRUST_PROXY_HEADERS", "false").casefold() == "true",
            media_version=os.getenv("PUBLIC_MEDIA_VERSION", "2026.08.19.avatar.1").strip(),
            media_root=Path(
                os.getenv("PUBLIC_MEDIA_ROOT", "/srv/project-snow/media/current")
            ).resolve(),
            experience_notice_version=os.getenv(
                "PUBLIC_EXPERIENCE_NOTICE_VERSION", "0.9.2"
            ).strip(),
            arrival_probability=max(0.0, min(1.0, arrival_probability)),
            sticker_version=os.getenv("PUBLIC_STICKER_VERSION", "2026.08.19.sticker.1").strip(),
            sticker_root=Path(
                os.getenv("PUBLIC_STICKER_ROOT", "/srv/project-snow/media/stickers/current")
            ).resolve(),
            feedback_email_to=os.getenv("PUBLIC_FEEDBACK_EMAIL_TO", "admin@xiaob.dev").strip(),
            feedback_email_from=os.getenv("PUBLIC_FEEDBACK_EMAIL_FROM", "").strip(),
            feedback_smtp_host=os.getenv("PUBLIC_FEEDBACK_SMTP_HOST", "").strip(),
            feedback_smtp_port=feedback_smtp_port,
            feedback_smtp_username=os.getenv("PUBLIC_FEEDBACK_SMTP_USERNAME", "").strip(),
            feedback_smtp_password=_secret_value("PUBLIC_FEEDBACK_SMTP_PASSWORD"),
            state_hmac_previous_key=_decode_32_byte_key(
                _secret_value("PUBLIC_STATE_HMAC_PREVIOUS_KEY")
            ),
            state_key_id=os.getenv("PUBLIC_STATE_KEY_ID", "2026-08-19").strip(),
            state_previous_key_id=os.getenv("PUBLIC_STATE_PREVIOUS_KEY_ID", "").strip(),
            turnstile_hostname=os.getenv("PUBLIC_TURNSTILE_HOSTNAME", "snow.xiaob.dev").strip(),
            turnstile_max_age_seconds=max(60, min(600, turnstile_max_age_seconds)),
            privacy_policy_version=os.getenv("PUBLIC_PRIVACY_POLICY_VERSION", "0.9.2").strip(),
            privacy_effective_at=os.getenv(
                "PUBLIC_PRIVACY_EFFECTIVE_AT", "2026-08-20"
            ).strip(),
            attribution_url=os.getenv(
                "PUBLIC_ATTRIBUTION_URL", "/public/v1/attributions"
            ).strip(),
            max_provider_calls_per_action=max(1, min(2, max_provider_calls_per_action)),
            # Product policy is a fixed absolute envelope lifetime.  Keep the
            # environment knob bounded so a deployment cannot silently turn
            # a tab-scoped convenience token into a long-lived credential.
            byok_lifetime_hours=max(1, min(12, byok_lifetime_hours)),
        )

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return (
            (self.public_origin, *self.development_origins)
            if self.allow_insecure_dev
            else (self.public_origin,)
        )

    def missing_production_secrets(self) -> list[str]:
        missing = []
        for name, value in (
            ("PUBLIC_DATABASE_URL", self.database_url),
            ("TURNSTILE_SITE_KEY", self.turnstile_site_key),
            ("TURNSTILE_SECRET", self.turnstile_secret),
            ("PUBLIC_CREDENTIAL_KEY", self.credential_key),
            ("PUBLIC_STATE_HMAC_KEY", self.state_hmac_key),
            ("PUBLIC_IP_HMAC_KEY", self.ip_hmac_key),
            ("PUBLIC_QQ_KEY", self.qq_key),
            ("QDRANT_API_KEY", self.qdrant_api_key),
            ("NEO4J_PASSWORD", self.neo4j_password),
        ):
            if not value:
                missing.append(name)
        return missing
