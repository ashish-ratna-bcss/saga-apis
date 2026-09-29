"""Legacy DB session module.

Bluweb is a stateless API service and does not own a database. This module
remains only so older crawl/document code under bluweb_app/db can still be
imported by tests without crashing at import time. The live FastAPI app
never uses it.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class DatabaseNotConfiguredError(RuntimeError):
    pass


def _disabled_engine():
    raise DatabaseNotConfiguredError(
        "Bluweb no longer owns a database. Use the scrape/preflight APIs; "
        "the caller stores results."
    )


# Placeholder attributes so `from bluweb_app.db.session import engine` still
# resolves; any real use raises.
engine = None  # type: ignore[assignment]
AsyncSessionLocal = None  # type: ignore[assignment]


async def get_db() -> AsyncIterator[AsyncSession]:
    _disabled_engine()
    yield  # pragma: no cover
