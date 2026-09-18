"""Application configuration, loaded from environment variables / .env.

No secret (api_hash, phone, session data) ever has a hard-coded default or
is logged. See README "Security" for the full policy.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

_PATH_FIELDS = (
    "telegram_session_path",
    "media_storage_path",
    "raw_evidence_storage_path",
    "avatar_cache_path",
    "message_media_cache_path",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore")

    # Telegram credentials - required only once a real connection is attempted.
    telegram_api_id: int | None = Field(default=None)
    telegram_api_hash: str | None = Field(default=None)
    telegram_session_path: str = Field(default=str(BASE_DIR / "data" / "telegram_service.session"))
    telegram_phone: str | None = Field(default=None)

    database_url: str = Field(default=f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'telegram_service.db'}")

    media_download_enabled: bool = Field(default=False)
    media_storage_path: str = Field(default=str(BASE_DIR / "data" / "media"))
    maximum_media_size_bytes: int = Field(default=20 * 1024 * 1024)
    allowed_media_types: str = Field(default="photo,document,video,audio,voice")

    raw_evidence_storage_path: str = Field(default=str(BASE_DIR / "data" / "raw_messages"))
    # Write-only archival -- no endpoint reads these files back (Milestone 4
    # data-ownership audit: the file path is never stored on TelegramMessage
    # or returned in any response). Defaults to True to preserve current
    # behavior exactly; set False to stop writing a per-message JSON file
    # without affecting message collection/storage/API behavior at all.
    raw_evidence_enabled: bool = Field(default=True)
    collection_batch_limit: int = Field(default=200)

    avatar_cache_path: str = Field(default=str(BASE_DIR / "data" / "avatars"))
    message_media_cache_path: str = Field(default=str(BASE_DIR / "data" / "message_media"))
    message_media_max_size_bytes: int = Field(default=20 * 1024 * 1024)

    # Bounds on the explicit bot-start interaction (POST /api/telegram/bots/
    # {id}/start) - how long to wait for the bot's reply, and how often to
    # poll for it. Never used to wait for anything else.
    bot_start_timeout_seconds: float = Field(default=15.0)
    bot_start_poll_interval_seconds: float = Field(default=1.0)

    access_reconciliation_interval_hours: int = Field(default=12)
    collection_interval_minutes: int = Field(default=5)
    connection_health_interval_minutes: int = Field(default=5)
    notification_processing_interval_minutes: int = Field(default=1)

    log_level: str = Field(default="INFO")
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    @field_validator(*_PATH_FIELDS, mode="after")
    @classmethod
    def _anchor_relative_path_to_base_dir(cls, value: str) -> str:
        """A relative value here (including one set via .env, e.g. the
        shipped `./data/...` convention) must resolve against this package's
        own directory, not whatever the process's CWD happens to be —
        otherwise running this app from a different CWD (e.g. mounted inside
        the unified API) silently starts a brand-new session/DB instead of
        the real persistent one. Absolute values pass through unchanged."""
        path = Path(value)
        return str(path) if path.is_absolute() else str((BASE_DIR / path).resolve())

    @field_validator("database_url", mode="after")
    @classmethod
    def _anchor_relative_sqlite_url_to_base_dir(cls, value: str) -> str:
        """Same fix as above, for the one case relevant to a SQLite
        ``database_url`` (three slashes = relative path per SQLAlchemy's own
        URL convention; four slashes = already absolute — left untouched, as
        is any non-sqlite URL such as Postgres)."""
        prefix = "sqlite+aiosqlite:///"
        if not value.startswith(prefix) or value.startswith(prefix + "/"):
            return value
        relative_path = value[len(prefix) :]
        return prefix + str((BASE_DIR / relative_path).resolve())

    @property
    def allowed_media_types_list(self) -> list[str]:
        return [t.strip() for t in self.allowed_media_types.split(",") if t.strip()]

    def mask_phone(self) -> str | None:
        if not self.telegram_phone:
            return None
        phone = self.telegram_phone
        if len(phone) <= 4:
            return "*" * len(phone)
        return phone[:3] + "*" * (len(phone) - 5) + phone[-2:]


@lru_cache
def get_settings() -> Settings:
    return Settings()
