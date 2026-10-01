"""Application configuration, loaded from environment variables / ``.env``.

Secrets are held in :class:`pydantic.SecretStr` so they never appear in ``repr()``,
log records, tracebacks or API responses.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Runtime configuration for the standalone Reddit RSS provider service."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------------------------------------------- application --
    app_name: str = Field(default="reddit-service", description="Service identifier.")
    environment: Literal["development", "staging", "production", "test"] = "development"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = Field(default="INFO")
    log_json: bool = Field(
        default=False, description="Emit newline-delimited JSON logs instead of text."
    )
    cors_origins: str = Field(
        default="",
        description="Comma-separated allowed CORS origins. Empty disables CORS.",
    )
    api_keys: str = Field(
        default="",
        description=(
            "Comma-separated API keys accepted in the X-API-Key header on every "
            "/api/reddit/* route. Empty disables authentication (development only)."
        ),
    )

    # ---------------------------------------------------------- reddit rss (no auth) --
    #: This service only ever talks to Reddit's public, unauthenticated .rss
    #: endpoints -- no Reddit OAuth client id/secret exist anywhere here.
    reddit_rss_user_agent: str = Field(
        default="reddit-rss-monitor/1.0 (stateless keyword/event monitor)",
        description="User-Agent sent to Reddit's public .rss endpoints. No credentials involved.",
    )
    reddit_rss_user: str = Field(
        default="",
        description=(
            "Optional: your Reddit username, paired with reddit_rss_feed, to append "
            "'user='/'feed=' to every RSS request. Get both from reddit.com -> "
            "Preferences -> RSS Feeds. This service does not add its own request cap; "
            "it waits only when Reddit's X-Ratelimit-* headers say the quota is used up."
        ),
    )
    reddit_rss_feed: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Optional RSS feed token paired with reddit_rss_user. Tied to a Reddit "
            "account (not a login credential, but treated as one: never logged, "
            "never returned by any endpoint)."
        ),
    )
    reddit_rss_accounts: str = Field(
        default="",
        description=(
            "Additional RSS accounts, comma-separated user:feed pairs from "
            "reddit.com -> Preferences -> RSS Feeds. A keyword list on one request "
            "is split across these accounts, fetched, then returned as one response."
        ),
    )
    reddit_rss_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    reddit_rss_max_retries: int = Field(
        default=2, ge=0, le=5, description="Bounded retries for transient RSS failures."
    )
    reddit_rss_retry_base_delay_seconds: float = Field(default=0.5, gt=0, le=10)
    reddit_rss_max_response_bytes: int = Field(
        default=2_000_000,
        gt=0,
        description="Abort an RSS fetch once the response body exceeds this many bytes.",
    )
    reddit_rss_event_threshold: int = Field(
        default=10,
        ge=1,
        description=(
            "event_signal.detected is true once a single response's post_count reaches this."
        ),
    )

    # ------------------------------------------------- reddit rss shared-gateway infra --
    #: Outbound quota is Reddit's own X-Ratelimit-* headers. The cache only avoids
    #: refetching an identical feed; it is not a second rate limit.
    reddit_rss_cache_ttl_seconds: float = Field(
        default=60.0,
        gt=0,
        description="How long a fetched feed is reused across callers before refetching.",
    )
    reddit_rss_cache_max_entries: int = Field(
        default=256, ge=1, description="Bounds the feed cache's memory use."
    )
    reddit_rss_gate_size: int = Field(
        default=64,
        ge=1,
        description=(
            "How many Reddit fetches may run at once. Extra callers wait in the "
            "per-tenant round-robin queue until a slot frees. They are not rejected "
            "and this is not a request quota."
        ),
    )

    # ------------------------------------------------------------------ validators --
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if normalized not in allowed:
            raise ValueError("log_level must be one of " + ", ".join(sorted(allowed)))
        return normalized

    # --------------------------------------------------------------------- helpers --
    @property
    def rss_account_pairs(self) -> list[tuple[str, str]]:
        """RSS ``(user, feed)`` pairs. The single ``reddit_rss_user`` pair is included."""

        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add(user: str, feed: str) -> None:
            key = (user.strip(), feed.strip())
            if key[0] and key[1] and key not in seen:
                seen.add(key)
                pairs.append(key)

        for part in self.reddit_rss_accounts.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            user, feed = part.split(":", 1)
            add(user, feed)
        add(self.reddit_rss_user, self.reddit_rss_feed_value)
        return pairs

    @property
    def reddit_rss_feed_value(self) -> str:
        """The raw RSS feed token. Only ever appended to RSS request params."""

        value = self.reddit_rss_feed
        if isinstance(value, SecretStr):
            return value.get_secret_value().strip()
        return str(value or "").strip()

    @property
    def api_key_list(self) -> list[str]:
        """Configured API keys. Empty means authentication is disabled."""

        return [key.strip() for key in self.api_keys.split(",") if key.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key_list)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def public_summary(self) -> dict[str, Any]:
        """Configuration snapshot that is safe to expose. Never includes secrets."""

        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "auth_enabled": self.auth_enabled,
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton used by the app and by FastAPI dependencies."""

    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that patch the environment)."""

    get_settings.cache_clear()
