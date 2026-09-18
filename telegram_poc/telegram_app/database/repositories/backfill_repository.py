"""Repository for backfill_jobs."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import BackfillJob


class BackfillJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields) -> BackfillJob:
        job = BackfillJob(**fields)
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: int) -> BackfillJob | None:
        return await self.session.get(BackfillJob, job_id)

    async def get_for_source(self, job_id: int, source_id: int) -> BackfillJob | None:
        result = await self.session.execute(
            select(BackfillJob).where(BackfillJob.id == job_id, BackfillJob.source_id == source_id)
        )
        return result.scalar_one_or_none()

    async def list_for_source(self, source_id: int, limit: int = 20) -> list[BackfillJob]:
        result = await self.session.execute(
            select(BackfillJob).where(BackfillJob.source_id == source_id).order_by(BackfillJob.id.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def update(self, job: BackfillJob, **fields) -> BackfillJob:
        for key, value in fields.items():
            setattr(job, key, value)
        await self.session.flush()
        return job
