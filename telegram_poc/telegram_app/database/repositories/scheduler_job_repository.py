"""Repository for scheduler_jobs - run history for observability, not a lock.

The no-overlap guarantee itself comes from APScheduler's max_instances=1 on
each job (see app/scheduler/jobs.py); this table is a record of what ran.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import SchedulerJobRun


class SchedulerJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def start_run(self, job_name: str) -> SchedulerJobRun:
        run = SchedulerJobRun(job_name=job_name, status="RUNNING")
        self.session.add(run)
        await self.session.flush()
        return run

    async def finish_run(self, run: SchedulerJobRun, status: str, details: dict) -> SchedulerJobRun:
        run.status = status
        run.details = details
        run.finished_at = datetime.now(timezone.utc)
        await self.session.flush()
        return run
