"""Historical collection (backfill) for an already-accessible source.

Runs as a background asyncio task (there is no separate worker/queue in
this architecture) so the triggering HTTP request returns immediately with
a job id; `GET /api/sources/{id}/backfill/{job_id}` polls status. The
background task owns its own DB session (mirrors the scheduler jobs
pattern) since the request's session closes when the request ends.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.config import Settings
from telegram_app.database.database import async_session_factory
from telegram_app.database.models import BackfillJob, BackfillJobStatus, SourceAccessStatus
from telegram_app.database.repositories.backfill_repository import BackfillJobRepository
from telegram_app.database.repositories.source_repository import SourceRepository
from telegram_app.observability import log_structured
from telegram_app.services import audit_service as audit_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.message_service import MessageService
from telegram_app.telegram import access_manager, collector
from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.discovery import normalize_identifier

logger = logging.getLogger("telegram_service.services.backfill")

# Same set monitoring_service/search_service require for real access - a
# backfill is exactly "collect messages", so it must never run for a source
# that has never been positively verified accessible.
BACKFILLABLE_STATUSES = {
    SourceAccessStatus.PUBLIC_ACCESSIBLE,
    SourceAccessStatus.ACCESSIBLE,
    SourceAccessStatus.JOINED,
    SourceAccessStatus.MONITORING,
}


class BackfillNotAllowedError(Exception):
    pass


class BackfillJobNotFoundError(Exception):
    pass


class BackfillService:
    def __init__(self, session: AsyncSession, client_manager: TelegramClientManager, settings: Settings) -> None:
        self.session = session
        self.client_manager = client_manager
        self.settings = settings
        self.sources = SourceRepository(session)
        self.jobs = BackfillJobRepository(session)
        self.audit = AuditService(session)

    async def start_backfill(
        self, source_id: int, *, from_date: datetime | None = None, to_date: datetime | None = None, limit: int = 1000
    ) -> BackfillJob:
        source = await self.sources.get(source_id)
        if source is None:
            raise BackfillJobNotFoundError(f"source {source_id} not found")

        status = SourceAccessStatus(source.access_status)
        if status not in BACKFILLABLE_STATUSES:
            raise BackfillNotAllowedError(
                f"source {source_id} is in state {status.value}; backfill requires verified access first"
            )

        limit = max(1, min(limit, 10000))
        job = await self.jobs.create(
            source_id=source_id,
            status=BackfillJobStatus.PENDING.value,
            from_date=from_date,
            to_date=to_date,
            requested_limit=limit,
        )
        await self.audit.log(
            audit_events.BACKFILL_STARTED, source_id=source_id,
            details={"job_id": job.id, "from_date": str(from_date), "to_date": str(to_date), "limit": limit},
        )
        await self.session.commit()

        asyncio.create_task(
            _run_backfill_job(job.id, source_id, from_date, to_date, limit, self.client_manager, self.settings)
        )
        return job

    async def get_backfill_job(self, source_id: int, job_id: int) -> BackfillJob:
        job = await self.jobs.get_for_source(job_id, source_id)
        if job is None:
            raise BackfillJobNotFoundError(f"backfill job {job_id} not found for source {source_id}")
        return job

    async def list_backfill_jobs(self, source_id: int) -> list[BackfillJob]:
        return await self.jobs.list_for_source(source_id)


async def _run_backfill_job(
    job_id: int,
    source_id: int,
    from_date: datetime | None,
    to_date: datetime | None,
    limit: int,
    client_manager: TelegramClientManager,
    settings: Settings,
) -> None:
    started = time.monotonic()
    async with async_session_factory() as session:
        jobs_repo = BackfillJobRepository(session)
        sources_repo = SourceRepository(session)
        messages_service = MessageService(session, settings)
        audit = AuditService(session)

        job = await jobs_repo.get(job_id)
        source = await sources_repo.get(source_id)
        if job is None or source is None:
            logger.error("backfill job %s or source %s vanished before it could run", job_id, source_id)
            return

        await jobs_repo.update(job, status=BackfillJobStatus.RUNNING.value, started_at=datetime.now(timezone.utc))
        await session.commit()

        ok, err = await client_manager.verify_authorized()
        if not ok:
            await jobs_repo.update(
                job, status=BackfillJobStatus.FAILED.value, error_message=err, completed_at=datetime.now(timezone.utc)
            )
            await audit.log(audit_events.BACKFILL_FAILED, source_id=source_id, success=False, details={"job_id": job_id, "error": err})
            await session.commit()
            return

        normalized = normalize_identifier(source.identifier)
        try:
            entity = await access_manager.resolve_entity(client_manager.client, normalized)
        except Exception as exc:  # noqa: BLE001 - resolution failure ends the job cleanly, not a crash
            await jobs_repo.update(
                job, status=BackfillJobStatus.FAILED.value, error_message=str(exc), completed_at=datetime.now(timezone.utc)
            )
            await audit.log(audit_events.BACKFILL_FAILED, source_id=source_id, success=False, details={"job_id": job_id, "error": str(exc)})
            await session.commit()
            return

        # Restart-safety: resume strictly older than whatever this job has
        # already walked past, rather than starting over from `to_date`.
        resume_before_id = job.checkpoint_message_id
        remaining = max(0, limit - job.messages_found)

        outcome = await collector.collect_historical_messages(
            client_manager.client, entity, from_date=from_date, to_date=to_date,
            limit=remaining or limit, source_username=source.username, before_message_id=resume_before_id,
        )

        metrics = await messages_service.persist_backfill_batch(source_id, outcome.messages, job)

        job = await jobs_repo.get(job_id)  # re-fetch: persist_backfill_batch commits/rolls back this session repeatedly
        final_status = BackfillJobStatus.COMPLETED.value
        if outcome.error:
            final_status = BackfillJobStatus.PARTIAL.value if job.messages_inserted > 0 else BackfillJobStatus.FAILED.value
            job.error_message = outcome.error
        await jobs_repo.update(job, status=final_status, completed_at=datetime.now(timezone.utc))
        await session.commit()

        duration_ms = int((time.monotonic() - started) * 1000)
        success = final_status != BackfillJobStatus.FAILED.value
        await audit.log(
            audit_events.BACKFILL_COMPLETED if success else audit_events.BACKFILL_FAILED,
            source_id=source_id, success=success,
            details={"job_id": job_id, "status": final_status, **metrics.as_dict()},
        )
        await session.commit()
        log_structured(
            logger, "backfill_completed", job_id=job_id, source_id=source_id, status=final_status,
            duration_ms=duration_ms, **metrics.as_dict(),
        )
