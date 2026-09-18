"""Runtime configuration. Free-only: no paid provider is wired in (see
app/adapters/registry.py) and this project has decided not to add one.
"""
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OSINT_", env_file=str(BASE_DIR / ".env"), extra="ignore"
    )

    # Dev default: SQLite, zero setup, single-process (in-process asyncio.Queue
    # worker, in-process rate limiting -- see orchestrator.py). Set to a
    # postgresql+psycopg:// URL for production/multi-process: the job queue,
    # rate limiter, and concurrent-investigation cap all become genuinely
    # cross-process, backed by the same Postgres DB (SELECT ... FOR UPDATE
    # SKIP LOCKED -- see app/job_queue.py) instead of Redis, since Postgres is
    # already the required source of truth and this workload doesn't need a
    # second piece of infrastructure to satisfy the multi-worker requirement.
    database_url: str = "sqlite:///./osint.db"

    # Pooling -- only meaningful for Postgres (SQLite's "pool" is a single
    # file-backed connection regardless of these values).
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle_seconds: int = 1800  # recycle idle connections, avoids stale-connection errors

    # Investigation lease duration for the Postgres worker -- see
    # job_queue.py's crash-recovery (STEP 11). A worker holding an
    # investigation renews this periodically; if it stops (crash), a peer
    # worker reclaims the investigation once the lease expires.
    investigation_lease_seconds: int = 120

    # Self-hosted, free, open-source meta search engine (https://docs.searxng.org).
    # Optional: PublicWebAdapter and GET/POST /api/v1/search both degrade to
    # "unavailable" (not faked) if unset or unreachable. Running your own
    # instance (no Docker required -- see scripts/setup_searxng.sh) keeps
    # search execution inside infrastructure you control, respecting each
    # engine's own rate limits. Both call the same client, app/search_client.py.
    searxng_url: str | None = None

    # Comma-separated engine names to actually query, restricting SearxNG's
    # full configured set to the ones verified (see scripts/verify_searxng_engines.py)
    # to return real results without CAPTCHA/login/parsing failures when
    # self-hosted. Empty string = let SearxNG use its own configured default set.
    searxng_engines: str = "google,bing,brave,wikipedia"

    # Host/port for `python -m osint_app.main` (uvicorn's own --host/--port flags
    # take precedence if you invoke uvicorn directly instead).
    host: str = "127.0.0.1"
    port: int = 8000

    # Per-request timeout (seconds) used by every adapter's own HTTP client.
    # Prevents a slow/hung external site from blocking a worker forever.
    adapter_timeout_seconds: int = 45

    # Max concurrent jobs processed by the in-process worker pool.
    worker_concurrency: int = 4

    # Max concurrent calls to the *same* source adapter within one process
    # (STEP 10) -- e.g. 4 investigations all pivoting into sherlock at once
    # shouldn't spawn 4x the load on it simultaneously. Per-investigation
    # concurrency needs no separate control: adapters within one investigation
    # already run strictly sequentially (see orchestrator._investigate_identifier).
    # Per-process only, not cluster-wide -- a true cluster-wide cap would need
    # Postgres advisory locks or Redis, unjustified extra infrastructure for
    # this workload (see "smallest architecture" guidance).
    source_max_concurrency: int = 3

    # maigret's bundled database has 3000+ sites; scanning all of them takes
    # several minutes. Cap to the top-ranked N (by Alexa-style rank) for a
    # bounded default job duration -- raise it for a deeper, slower scan.
    maigret_top_sites: int = 300

    # --- Pivot engine safety controls (see PIVOT SAFETY CONTROLS) ---
    pivot_enabled: bool = True
    max_pivot_depth: int = 2  # root=depth 0; a depth-1 pivot's own pivots are depth 2, then stop
    max_total_pivots_per_investigation: int = 15
    max_pivots_per_entity: int = 5
    pivot_confidence_threshold: float = 0.5
    max_investigation_runtime_seconds: int = 600  # wall-clock budget across root + all pivots

    # --- Rate limiting (in-process token bucket, see RATE LIMITING) ---
    max_concurrent_investigations: int = 10

    # --- Document (PDF/HTML/TXT) mining -- see document_fetch.py / document_adapter.py ---
    document_mining_enabled: bool = True
    max_documents_per_investigation: int = 10
    document_fetch_timeout_seconds: int = 15
    max_document_bytes: int = 10 * 1024 * 1024  # 10MB

    # --- Retry policy for transient adapter failures (see SourceUnavailable.retryable
    # in adapters/base.py). Never applies to non-retryable failures (CAPTCHA-blocked
    # sources, validation errors) regardless of these settings.
    adapter_max_retries: int = 2  # total attempts = 1 + this
    adapter_retry_backoff_seconds: float = 0.5  # exponential: 0.5s, 1s, 2s, ...

    # --- Data retention (STEP 22). Both None (default): keep forever -- an
    # operator decision, not one Phase 1 should force. Set to enable
    # app.retention.purge_expired, run manually or via cron/systemd timer
    # (no built-in scheduler, see "smallest architecture" guidance).
    investigation_retention_days: int | None = None
    audit_log_retention_days: int | None = None

    @field_validator("database_url", mode="after")
    @classmethod
    def _anchor_relative_sqlite_url_to_base_dir(cls, value: str) -> str:
        """A relative sqlite URL (three slashes, SQLAlchemy's own convention)
        resolves against the process's CWD, not this package's own
        directory — fine standalone (CWD is always this repo), but silently
        opens/creates a stray, disconnected database file elsewhere when
        this app runs from a different CWD (e.g. mounted inside the unified
        API). Anchor it to BASE_DIR instead. Four-slash (already absolute)
        and non-sqlite URLs (e.g. postgresql://) pass through unchanged."""
        prefix = "sqlite:///"
        if not value.startswith(prefix) or value.startswith(prefix + "/"):
            return value
        relative_path = value[len(prefix) :]
        return prefix + str((BASE_DIR / relative_path).resolve())

    @property
    def searxng_engines_list(self) -> list[str]:
        return [e.strip() for e in self.searxng_engines.split(",") if e.strip()]


settings = Settings()
