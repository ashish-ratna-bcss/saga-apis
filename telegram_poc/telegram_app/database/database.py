"""Async SQLAlchemy engine/session setup.

Everything above the repository layer talks to the database only through
`AsyncSession` objects handed out here, and repositories are the only code
that builds queries - so swapping `DATABASE_URL` from SQLite to Postgres
later requires no change to services, routes, or the Telegram layer.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

from telegram_app.config import get_settings
from telegram_app.database.models import Base

logger = logging.getLogger("telegram_service.database")

settings = get_settings()

if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///")[-1]
    Path(db_path).resolve().parent.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False, future=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# (table, column, DDL type + default) additive patches for columns added to
# an ORM model after a table may already exist on disk from an earlier
# version of the app. create_all() only creates missing *tables*, never
# adds columns to ones that already exist, so a real deployment's existing
# SQLite file needs this to pick up new columns without losing data.
_ADDITIVE_COLUMN_PATCHES: list[tuple[str, str, str]] = [
    ("telegram_messages", "collection_method", "VARCHAR(32) DEFAULT 'monitoring'"),
    ("telegram_sources", "last_probe_status", "VARCHAR(32)"),
    ("telegram_sources", "last_probe_reason", "TEXT"),
    ("telegram_sources", "last_probe_at", "DATETIME"),
    ("telegram_sources", "discovered_from_source_id", "INTEGER"),
    ("telegram_sources", "discovery_type", "VARCHAR(32)"),
]


async def _apply_additive_column_patches(conn: AsyncConnection) -> None:
    """SQLite-only. Not a substitute for a real migration tool (Alembic) -
    if this project moves to Postgres, replace this with Alembic migrations."""
    if conn.engine.url.get_backend_name() != "sqlite":
        return
    for table, column, ddl_type_and_default in _ADDITIVE_COLUMN_PATCHES:
        result = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing_columns = {row[1] for row in result.fetchall()}
        if column not in existing_columns:
            logger.info("migrating: adding column %s.%s", table, column)
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type_and_default}"))


async def init_db() -> None:
    """Creates all tables if they do not exist yet (SQLite-friendly bootstrap),
    then applies any additive column patches to tables that already existed."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_additive_column_patches(conn)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped session."""
    async with async_session_factory() as session:
        yield session
