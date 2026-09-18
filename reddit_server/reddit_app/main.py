"""FastAPI application factory, lifespan and error handling.

Two independent Reddit transports live here, both under ``/api/reddit/*`` and
both behind the same ``X-API-Key`` policy:

- ``/api/reddit/*`` (except ``/rss/*``) -- the authenticated OAuth2 REST API.
  Requires ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET``/``REDDIT_USER_AGENT``.
- ``/api/reddit/rss/*`` -- Reddit's public, unauthenticated RSS/Atom endpoints.
  Requires none of the above; works even when Reddit OAuth is not configured.

Startup order
-------------
1. Load configuration and configure logging (with secret redaction).
2. Initialize both Reddit clients (HTTP clients only; the OAuth client requests no
   token yet -- the first real request authenticates lazily; the RSS client never
   authenticates at all).
3. If OAuth is configured, perform one best-effort OAuth2 authentication so a
   broken credential shows up in the startup log immediately rather than on the
   first call. Skipped for RSS -- there is no credential to check.

Shutdown closes both clients' HTTP connections.

This service is intentionally stateless: no database, no scheduler, no
``/monitoring/start``/``/stop`` lifecycle. SOC Eye owns polling/storage/alerts and
calls this service's endpoints (subreddit/search/post/comment/user/resolve/rss/*) on
its own schedule -- see app/services/provider_service.py and
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

from reddit_app.api.routes_comment import router as comment_router
from reddit_app.api.routes_health import SERVICE_VERSION
from reddit_app.api.routes_health import router as health_router
from reddit_app.api.routes_post import router as post_router
from reddit_app.api.routes_reddit_rss import router as reddit_rss_router
from reddit_app.api.routes_resolve import router as resolve_router
from reddit_app.api.routes_search import router as search_router
from reddit_app.api.routes_status import router as status_router
from reddit_app.api.routes_subreddit import router as subreddit_router
from reddit_app.api.routes_user import router as user_router
from reddit_app.core.config import Settings, get_settings
from reddit_app.core.exceptions import RedditServiceError
from reddit_app.core.logging import configure_logging, get_logger, register_secret
from reddit_app.core.rate_limit import ClientRateLimiter, rate_limit_rss_client
from reddit_app.core.security import register_api_key_secrets, require_api_key
from reddit_app.reddit.client import RedditClientManager

logger = get_logger(__name__)

DESCRIPTION = """
Standalone Reddit data-provider service for SOC Eye.

Two independent Reddit transports:

- **`/api/reddit/*`** (except `/rss/*`) -- fetches public Reddit data (subreddits,
  posts, comments, users, search) through the **official Reddit OAuth2 API** and
  returns stable, normalized JSON. Requires Reddit OAuth credentials.
- **`/api/reddit/rss/*`** -- keyword/event monitoring through Reddit's **public,
  unauthenticated RSS/Atom feeds**. Requires no Reddit credentials at all, is a
  public search interface (not the official API), and is subject to Reddit's own
  RSS availability/rate limits.

Both own Reddit connectivity, pagination/parsing and rate-limit/retry handling;
both deliberately own nothing else -- no storage, no deduplication across
requests, no polling scheduler, no alerting, no UI. SOC Eye owns all of that and
calls this service's endpoints on its own schedule.
"""


def _startup_banner(settings: Settings) -> None:
    logger.info(
        "Starting Reddit provider service",
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
    if settings.reddit_configured:
        try:
            await clients.rest.ensure_token()
        except Exception:  # noqa: BLE001 - never block startup on Reddit
            logger.error("Could not authenticate with Reddit at startup", exc_info=True)
    else:
        logger.warning(
            "Reddit OAuth credentials are not configured. Every /api/reddit/* endpoint "
            "except /api/reddit/rss/* will return 503 REDDIT_NOT_CONFIGURED. "
            "/api/reddit/rss/* needs no Reddit credentials and remains fully usable."
        )

    logger.info("Startup complete")
    try:
        yield
    finally:
        logger.info("Shutting down Reddit provider service")
        await clients.close()
        logger.info("Shutdown complete")


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    token_transport: httpx.AsyncBaseTransport | None = None,
    rss_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``transport``/``token_transport``/``rss_transport`` let tests substitute the
    OAuth REST API and the public RSS host without patching globals or touching
    the network.
    """

    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    register_secret(settings.client_secret)
    register_secret(settings.password)
    register_secret(settings.reddit_rss_feed_value)
    register_api_key_secrets(settings)

    app = FastAPI(
        title="Reddit Provider Service",
        description=DESCRIPTION,
        version=SERVICE_VERSION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "health", "description": "Service liveness and configuration checks."},
            {
                "name": "reddit",
                "description": (
                    "Subreddit/post/comment/user lookup, search, and URL resolution via the "
                    "official OAuth2 API. Stateless: every call reads live from Reddit. "
                    "Requires Reddit OAuth credentials."
                ),
            },
            {
                "name": "reddit-rss",
                "description": (
                    "Keyword/event monitoring via Reddit's public, unauthenticated RSS/Atom "
                    "feeds. authenticated=false in every response: no Reddit OAuth credentials "
                    "are read or required. Still governed by this service's own X-API-Key "
                    "policy, same as every other /api/reddit/* route, plus a per-caller request "
                    "budget (see the RSS integration docs). A shared, short-TTL raw-feed cache "
                    "means concurrent callers requesting the same feed share one Reddit fetch; "
                    "filtering is still computed fresh per call, and no polling loop or "
                    "cross-request result deduplication is done on the caller's behalf."
                ),
            },
        ],
    )

    clients = RedditClientManager(
        settings, transport=transport, token_transport=token_transport, rss_transport=rss_transport
    )

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
    api_key_dependency = [Depends(require_api_key)]
    app.include_router(status_router, dependencies=api_key_dependency)
    app.include_router(subreddit_router, dependencies=api_key_dependency)
    app.include_router(post_router, dependencies=api_key_dependency)
    app.include_router(comment_router, dependencies=api_key_dependency)
    app.include_router(user_router, dependencies=api_key_dependency)
    app.include_router(search_router, dependencies=api_key_dependency)
    app.include_router(resolve_router, dependencies=api_key_dependency)
    app.include_router(
        reddit_rss_router,
        dependencies=[*api_key_dependency, Depends(rate_limit_rss_client)],
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
