"""FastAPI application factory, lifespan and error handling.

This service has exactly one Reddit transport: ``/api/reddit/rss/*``, Reddit's
public, unauthenticated RSS/Atom feeds. There is no OAuth client, no Reddit
credentials anywhere in this codebase -- it works from a bare checkout with no
``.env`` at all.

Startup order
-------------
1. Load configuration and configure logging (with secret redaction).
2. Start the RSS HTTP client (no authentication step -- there is nothing to
   authenticate).

Shutdown closes the RSS client's HTTP connection.

This service is intentionally stateless: no database, no scheduler, no
``/monitoring/start``/``/stop`` lifecycle. SOC Eye owns polling/storage/alerts and
calls this service's endpoints on its own schedule -- see
app/services/reddit_rss_service.py for why.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from reddit_app.api.routes_health import SERVICE_VERSION
from reddit_app.api.routes_health import router as health_router
from reddit_app.api.routes_reddit_rss import router as reddit_rss_router
from reddit_app.core.config import Settings, get_settings
from reddit_app.core.exceptions import RedditServiceError
from reddit_app.core.logging import configure_logging, get_logger, register_secret
from reddit_app.core.rate_limit import ClientRateLimiter
from reddit_app.reddit.tenant_queue import tenant_key_var
from reddit_app.core.security import register_api_key_secrets, require_api_key
from reddit_app.reddit.client import RedditClientManager

logger = get_logger(__name__)

DESCRIPTION = """
Standalone Reddit RSS provider service.

**`/api/reddit/rss/*`** — keyword/event/profile monitoring through Reddit's
**public, unauthenticated RSS/Atom feeds** only. No Reddit OAuth, no client
credentials. Subject to Reddit's own RSS availability and rate limits.

Owns feed fetch/parse and rate-limit/retry handling; deliberately owns nothing
else — no storage, no cross-request deduplication, no polling scheduler, no
alerting, no UI. Callers own that and hit these endpoints on their own schedule.
"""


def _startup_banner(settings: Settings) -> None:
    logger.info(
        "Starting Reddit RSS provider service",
        extra={"version": SERVICE_VERSION, **settings.public_summary()},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    clients: RedditClientManager = app.state.clients

    _startup_banner(settings)

    if not settings.auth_enabled:
        logger.warning(
            "API authentication is DISABLED (API_KEYS is empty). Every /api/reddit/* "
            "endpoint is open to anything that can reach this port. Set API_KEYS "
            "before exposing this service beyond localhost."
        )
    else:
        logger.info("API authentication enabled", extra={"configured_keys": len(settings.api_key_list)})

    await clients.start()
    logger.info("Startup complete")
    try:
        yield
    finally:
        logger.info("Shutting down Reddit RSS provider service")
        await clients.close()
        logger.info("Shutdown complete")


async def bind_reddit_tenant(request: Request) -> AsyncIterator[None]:
    """Tag this request with the calling tenant, the same key sentiment uses.

    ``x-tenant-key`` is the tenant database name. ``x-client-id`` is the
    BluGate client code and is the fallback when the app did not send a key.
    """

    raw = (
        request.headers.get("x-tenant-key")
        or request.headers.get("x-client-id")
        or "unknown"
    )
    token = tenant_key_var.set(raw.strip() or "unknown")
    try:
        yield
    finally:
        tenant_key_var.reset(token)


def create_app(
    settings: Settings | None = None,
    *,
    rss_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``rss_transport`` lets tests substitute Reddit's public RSS host without
    patching globals or touching the network.
    """

    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    register_secret(settings.reddit_rss_feed_value)
    register_api_key_secrets(settings)

    app = FastAPI(
        title="Reddit RSS Provider Service",
        description=DESCRIPTION,
        version=SERVICE_VERSION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "health", "description": "Service liveness and readiness checks."},
            {
                "name": "reddit-rss",
                "description": (
                    "Keyword/event/profile monitoring via Reddit's public RSS/Atom feeds. "
                    "authenticated=false in every response — this service has no Reddit "
                    "credentials. Still governed by X-API-Key (when API_KEYS is set) and a "
                    "per-caller request budget. Shared short-TTL feed cache coalesces "
                    "identical fetches; filtering is per call. No polling loop or "
                    "cross-request deduplication."
                ),
            },
        ],
    )

    clients = RedditClientManager(settings, rss_transport=rss_transport)

    app.state.settings = settings
    app.state.clients = clients
    app.state.rss_client_limiter = ClientRateLimiter(
        max_requests=settings.reddit_rss_client_limit,
        window_seconds=settings.reddit_rss_client_window_seconds,
        max_tracked_clients=settings.reddit_rss_client_limit_max_tracked_clients,
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )
        logger.info("CORS enabled", extra={"origins": len(settings.cors_origin_list)})

    app.include_router(health_router)
    app.include_router(
        reddit_rss_router,
        dependencies=[Depends(require_api_key), Depends(bind_reddit_tenant)],
    )
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Translate internal exceptions into the SOC Eye ``{"error": {...}}`` shape."""

    @app.exception_handler(RedditServiceError)
    async def _handle_service_error(request: Request, exc: RedditServiceError) -> JSONResponse:
        log = logger.warning if exc.http_status < 500 else logger.error
        log(
            "Request failed",
            extra={"path": request.url.path, "code": exc.code, "http_status": exc.http_status},
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": {"errors": _sanitize_validation_errors(exc.errors())},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled error", extra={"path": request.url.path, "error_type": type(exc).__name__}
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An internal error occurred.",
                    "details": {"timestamp": datetime.now(UTC).isoformat()},
                }
            },
        )


def _sanitize_validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip inputs and exception objects out of Pydantic validation errors."""

    return [
        {
            "location": list(error.get("loc", [])),
            "message": str(error.get("msg", "invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in errors
    ]


app = create_app()
