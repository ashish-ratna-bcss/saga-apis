"""FastAPI application entry point.

Wiring only - no business logic and no Telethon calls live here. Routes are
included from app/api/*, all backed by the service layer.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from telegram_app.api import (
    routes_auth,
    routes_backfill,
    routes_bots,
    routes_messages,
    routes_monitoring,
    routes_notifications,
    routes_provider,
    routes_search,
    routes_sources,
)
from telegram_app.config import get_settings
from telegram_app.database.database import init_db
from telegram_app.scheduler.jobs import setup_scheduler
from telegram_app.telegram.client import get_client_manager
from telegram_app.telegram.errors import TelegramSearchError

logging.basicConfig(
    level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_service.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()

    client_manager = get_client_manager()
    if client_manager.is_configured:
        ok, err = await client_manager.connect()
        if not ok:
            logger.warning("startup: could not connect to Telegram yet (%s) - will retry via health-check job", err)
    else:
        logger.warning("startup: TELEGRAM_API_ID/TELEGRAM_API_HASH not configured - auth endpoints will report errors")

    scheduler = setup_scheduler(client_manager, settings)
    scheduler.start()
    app.state.scheduler = scheduler

    yield

    scheduler.shutdown(wait=False)
    await client_manager.disconnect()


app = FastAPI(
    title="Telegram Collection Service",
    description="Access-managed Telegram data collection service (standalone).",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(routes_auth.router)
app.include_router(routes_sources.router)
app.include_router(routes_monitoring.router)
app.include_router(routes_messages.router)
app.include_router(routes_notifications.router)
app.include_router(routes_search.router)
app.include_router(routes_bots.router)
app.include_router(routes_backfill.router)
app.include_router(routes_provider.router)


@app.exception_handler(TelegramSearchError)
async def telegram_search_error_handler(request: Request, exc: TelegramSearchError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_payload())


CONSOLE_FILE = Path(__file__).resolve().parent / "static" / "console.html"


@app.get("/console", include_in_schema=False)
async def console():
    """Serves the operator console UI.

    Deliberately served by this app rather than opened as a local file: that
    makes it same-origin with the API, so no CORS middleware is needed. Adding
    permissive CORS instead would let any website in the browser drive this
    (unauthenticated) API and the Telegram account behind it.
    """
    return FileResponse(CONSOLE_FILE, media_type="text/html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    client_manager = get_client_manager()
    return {
        "status": "ready" if client_manager.is_configured else "not_configured",
        "telegram_configured": client_manager.is_configured,
    }
