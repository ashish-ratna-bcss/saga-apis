"""Application configuration, loaded from environment variables / ``.env``.

Secrets are held in :class:`pydantic.SecretStr` so they never appear in ``repr()``,
log records, tracebacks or API responses.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Runtime configuration for the standalone Reddit provider service."""

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

    # --------------------------------------------------------------------- reddit --
    reddit_client_id: str = Field(default="", description="Reddit OAuth2 app client id.")
    reddit_client_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Reddit OAuth2 app client secret. Never logged and never returned by the API.",
    )
    reddit_username: str = Field(
        default="",
        description=(
            "Optional. With reddit_password, authenticates as this Reddit account "
            "(OAuth2 'password' grant). Omit both for application-only "
            "'client_credentials' auth (public read access, no specific account)."
        ),
    )
    reddit_password: SecretStr = Field(default=SecretStr(""), description="Reddit account password.")
    reddit_user_agent: str = Field(
        default="",
        description=(
            "Required by Reddit's API rules: a unique, descriptive user agent. "
            "Reddit throttles or blocks generic/default user agents."
        ),
    )
    reddit_oauth_base_url: str = Field(
        default="https://www.reddit.com",
        description="Base URL for the Reddit OAuth2 token endpoint.",
    )
    reddit_api_base_url: str = Field(
        default="https://oauth.reddit.com",
        description="Base URL for the authenticated Reddit REST API.",
    )
    reddit_request_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    reddit_max_retries: int = Field(
        default=3, ge=0, le=10, description="Bounded retries for transient failures."
    )
    reddit_retry_base_delay_seconds: float = Field(default=0.5, gt=0, le=30)
    reddit_max_retry_delay_seconds: float = Field(default=30.0, gt=0, le=300)
    reddit_max_rate_limit_wait_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Refuse to sleep longer than this for a 429; raise instead.",
    )
    reddit_token_refresh_margin_seconds: int = Field(
        default=60,
        ge=0,
        description="Renew the access token this many seconds before it actually expires.",
    )

    # ---------------------------------------------------------- reddit rss (no auth) --
    #: Independent of the OAuth settings above -- the RSS transport never reads
    #: reddit_client_id/reddit_client_secret and works when they are unset.
    reddit_rss_user_agent: str = Field(
        default="reddit-rss-monitor/1.0 (stateless keyword/event monitor)",
        description="User-Agent sent to Reddit's public .rss endpoints. No credentials involved.",
    )
    reddit_rss_user: str = Field(
        default="",
        description=(
            "Optional: your Reddit username, paired with reddit_rss_feed, to append "
            "'user='/'feed=' to every RSS request -- Reddit's own June 2026 workaround "
            "for its unauthenticated RSS rate cut (~100/10min -> ~1/min). Get both from "
            "reddit.com -> Preferences -> RSS Feeds. Leave both unset to still work, "
            "just rate-limited harder (this client throttles proactively either way "
            "from Reddit's own X-Ratelimit-* response headers, present on both)."
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
    #: These four groups exist because this service is a shared gateway: many callers
    #: hitting one process, which hits one Reddit-facing IP with a hard ~1 req/min
    #: budget. See app/reddit/rate_limiter.py and app/reddit/feed_cache.py.
    reddit_rss_global_rate: float = Field(
        default=1 / 60,
        gt=0,
        description=(
            "Process-wide Reddit RSS outbound tokens/sec -- a proactive pacing gate, "
            "separate from (and in addition to) the reactive X-Ratelimit-* backoff "
            "above. Every outbound attempt, retries included, waits for a token."
        ),
    )
    reddit_rss_global_burst: int = Field(
        default=1, ge=1, description="Token bucket capacity for reddit_rss_global_rate."
    )
    reddit_rss_max_queue_wait_seconds: float = Field(
        default=55.0,
        gt=0,
        description="Give up waiting for the shared Reddit RSS budget after this long.",
    )
    reddit_rss_cache_ttl_seconds: float = Field(
        default=60.0,
        gt=0,
        description="How long a fetched feed is reused across callers before refetching.",
    )
    reddit_rss_cache_max_entries: int = Field(
        default=256, ge=1, description="Bounds the feed cache's memory use."
    )
    reddit_rss_client_limit: int = Field(
        default=10,
        ge=1,
        description="Max /api/reddit/rss/* requests per caller within the window below.",
    )
    reddit_rss_client_window_seconds: float = Field(default=60.0, gt=0)
    reddit_rss_client_limit_max_tracked_clients: int = Field(
        default=10_000,
        ge=1,
        description="Bounds the per-client limiter's memory use across distinct callers.",
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

    @field_validator("reddit_oauth_base_url")
    @classmethod
    def _validate_oauth_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("https://www.reddit.com", "https://reddit.com")):
            # Hard allow-list: this service only ever talks to the official API.
            raise ValueError("reddit_oauth_base_url must start with https://www.reddit.com")
        return cleaned

    @field_validator("reddit_api_base_url")
    @classmethod
    def _validate_api_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith("https://oauth.reddit.com"):
            raise ValueError("reddit_api_base_url must start with https://oauth.reddit.com")
        return cleaned

    @model_validator(mode="after")
    def _validate_retry_window(self) -> Settings:
        if self.reddit_retry_base_delay_seconds > self.reddit_max_retry_delay_seconds:
            raise ValueError(
                "reddit_retry_base_delay_seconds must be <= reddit_max_retry_delay_seconds"
            )
        return self

    # --------------------------------------------------------------------- helpers --
    @property
    def client_secret(self) -> str:
        """The raw client secret. Only ever passed to the token-exchange request.

        Tolerates a plain string because ``model_copy(update=...)`` bypasses
        validation and would otherwise leave a bare ``str`` in this field.
        """

        value = self.reddit_client_secret
        if isinstance(value, SecretStr):
            return value.get_secret_value().strip()
        return str(value or "").strip()

    @property
    def password(self) -> str:
        value = self.reddit_password
        if isinstance(value, SecretStr):
            return value.get_secret_value().strip()
        return str(value or "").strip()

    @property
    def reddit_rss_feed_value(self) -> str:
        """The raw RSS feed token. Only ever appended to RSS request params."""

        value = self.reddit_rss_feed
        if isinstance(value, SecretStr):
            return value.get_secret_value().strip()
        return str(value or "").strip()

    @property
    def reddit_configured(self) -> bool:
        """True when the minimum credentials to authenticate are present."""

        return bool(self.reddit_client_id and self.client_secret and self.reddit_user_agent)

    @property
    def has_account_credentials(self) -> bool:
        return bool(self.reddit_username and self.password)

    @property
    def grant_type(self) -> Literal["password", "client_credentials"]:
        return "password" if self.has_account_credentials else "client_credentials"

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
            "reddit_api_base_url": self.reddit_api_base_url,
            "reddit_configured": self.reddit_configured,
            "grant_type": self.grant_type if self.reddit_configured else None,
            "auth_enabled": self.auth_enabled,
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton used by the app and by FastAPI dependencies."""

    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that patch the environment)."""

    get_settings.cache_clear()
