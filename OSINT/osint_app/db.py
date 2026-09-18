from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from osint_app.config import settings

is_sqlite = settings.database_url.startswith("sqlite")
is_postgres = settings.database_url.startswith("postgresql")

if is_sqlite:
    # `timeout`: how long Python's sqlite3 driver retries a locked write
    # before raising "database is locked", instead of failing instantly --
    # matters because a long-running adapter call (e.g. sherlock, minutes)
    # can hold a write transaction open (see the connect-time WAL PRAGMA
    # below and orchestrator._run_adapter_job's early commit) while another
    # request (e.g. POST .../cancel) needs to write concurrently.
    engine = create_engine(
        settings.database_url, connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL: readers never block on a writer (and vice versa), which is
        # the standard fix for SQLite + concurrent web-app access -- without
        # it, one connection's write blocks every other read AND write for
        # the life of its transaction.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

else:
    # Postgres (or any other real server-based DB): real connection pool,
    # not a single file-backed connection. pool_pre_ping avoids handing out
    # a connection that's gone stale (e.g. DB restarted, idle firewall
    # timeout) -- the alternative is a confusing "connection already closed"
    # error surfacing mid-request.
    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
    )


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Dev convenience only (SQLite): creates tables directly from the ORM
    metadata. For Postgres, run `alembic upgrade head` as a deploy step
    before starting the app -- see MIGRATIONS in the README. The app itself
    never auto-runs migrations; that's an operator decision, not something
    to happen implicitly on every process start."""
    if not is_sqlite:
        return
    from osint_app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
