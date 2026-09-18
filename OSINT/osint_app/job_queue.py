"""Postgres-backed job queue -- the multi-process-safe alternative to
orchestrator.py's in-process asyncio.Queue, active only when
`app.db.is_postgres` is True (SQLite/single-process dev keeps the simpler
in-process queue unchanged, see STEP 5's "smallest architecture" guidance).

Uses `SELECT ... FOR UPDATE SKIP LOCKED` (via SQLAlchemy's
`with_for_update(skip_locked=True)`) to atomically claim one queued
investigation per call, so N worker processes polling concurrently never
grab the same investigation twice -- no separate broker required, since
Postgres is already the required source of truth.

Crash recovery (STEP 11): a worker holding an investigation renews its
lease periodically while processing; `reclaim_expired_leases()` finds
RUNNING investigations whose lease has lapsed (worker died mid-job) and
puts them back to QUEUED for another worker to pick up. No duplicate
*concurrent* execution: SKIP LOCKED means only one worker ever holds the row
at a time, and reclaiming only happens after the lease has actually expired.
"""
import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from osint_app.config import settings
from osint_app.enums import JobStatus
from osint_app.models import Investigation

logger = logging.getLogger("job_queue")

# Per-process identity, embedded in `locked_by` -- purely for observability
# (source_health-style debugging: "which worker had this?"), not used for
# any locking decision itself (SKIP LOCKED handles that).
WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"


def claim_next_investigation(db: Session) -> str | None:
    """Atomically claims one QUEUED investigation, transitioning it to
    RUNNING with a fresh lease. Returns its id, or None if the queue is
    empty. Safe to call concurrently from any number of worker processes --
    SKIP LOCKED means a row already locked by another worker's transaction
    is invisible to this query rather than blocking on it."""
    stmt = (
        select(Investigation)
        .where(Investigation.status == JobStatus.QUEUED)
        .order_by(Investigation.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    investigation = db.execute(stmt).scalar_one_or_none()
    if investigation is None:
        return None

    investigation.status = JobStatus.RUNNING
    investigation.locked_by = WORKER_ID
    investigation.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.investigation_lease_seconds)
    db.commit()
    return investigation.id


def renew_lease(db: Session, investigation_id: str) -> None:
    investigation = db.get(Investigation, investigation_id)
    if investigation is None or investigation.status != JobStatus.RUNNING:
        return  # finished (or cancelled) already -- nothing to renew
    investigation.lease_expires_at = datetime.now(UTC) + timedelta(seconds=settings.investigation_lease_seconds)
    db.commit()


def reclaim_expired_leases(db: Session) -> int:
    """Requeues RUNNING investigations whose worker lease has expired (the
    worker holding them crashed or was killed without finishing). Called
    periodically by every worker process during idle polling."""
    stmt = (
        select(Investigation)
        .where(Investigation.status == JobStatus.RUNNING, Investigation.lease_expires_at < datetime.now(UTC))
        .with_for_update(skip_locked=True)
    )
    stale = db.execute(stmt).scalars().all()
    for investigation in stale:
        logger.warning(
            "reclaiming investigation %s: lease held by %s expired", investigation.id, investigation.locked_by
        )
        investigation.status = JobStatus.QUEUED
        investigation.locked_by = None
        investigation.lease_expires_at = None
    db.commit()
    return len(stale)


async def _lease_renewal_task(investigation_id: str, interval_seconds: float) -> None:
    from osint_app.db import SessionLocal

    while True:
        await asyncio.sleep(interval_seconds)
        db = SessionLocal()
        try:
            renew_lease(db, investigation_id)
        finally:
            db.close()


async def run_postgres_worker_loop(poll_interval_seconds: float = 2.0) -> None:
    """One polling worker loop -- run several of these concurrently (in one
    process, or across many processes/containers) to scale horizontally.
    Each iteration: try to claim an investigation; if found, process it
    with a background task keeping its lease alive; if the queue is empty,
    use the idle moment to reclaim any peers' expired leases, then sleep."""
    from osint_app.db import SessionLocal
    from osint_app.orchestrator import process_investigation

    renewal_interval = max(settings.investigation_lease_seconds / 3, 5)

    while True:
        db = SessionLocal()
        try:
            investigation_id = claim_next_investigation(db)
        finally:
            db.close()

        if investigation_id is None:
            db2 = SessionLocal()
            try:
                reclaim_expired_leases(db2)
            finally:
                db2.close()
            await asyncio.sleep(poll_interval_seconds)
            continue

        logger.info("worker %s claimed investigation %s", WORKER_ID, investigation_id)
        renewal_task = asyncio.create_task(_lease_renewal_task(investigation_id, renewal_interval))
        try:
            await process_investigation(investigation_id)
        except Exception:
            logger.exception("investigation %s crashed the worker loop", investigation_id)
        finally:
            renewal_task.cancel()
