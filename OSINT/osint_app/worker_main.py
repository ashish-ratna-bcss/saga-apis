"""Standalone worker entrypoint (docker-compose "worker" service) -- runs
only app.job_queue.run_postgres_worker_loop(), no HTTP server. Run as many
copies of this process as needed to scale horizontally; `SELECT ... FOR
UPDATE SKIP LOCKED` against Postgres means they never duplicate work (see
job_queue.py).

    python -m osint_app.worker_main

Requires OSINT_DATABASE_URL to point at Postgres (SQLite dev doesn't use
this -- app/main.py's lifespan runs an in-process worker pool instead) and
migrations already applied (`alembic upgrade head`).
"""
import asyncio
import logging

from osint_app.config import settings
from osint_app.db import is_postgres
from osint_app.job_queue import run_postgres_worker_loop
from osint_app.logging_config import configure_logging

configure_logging()
logger = logging.getLogger("worker_main")


async def main() -> None:
    if not is_postgres:
        raise SystemExit(
            "app.worker_main requires OSINT_DATABASE_URL to point at Postgres -- "
            "SQLite dev runs its worker pool in-process via the API server instead."
        )
    logger.info("starting %d worker loop(s)", settings.worker_concurrency)
    await asyncio.gather(*[run_postgres_worker_loop() for _ in range(settings.worker_concurrency)])


if __name__ == "__main__":
    asyncio.run(main())
