"""Repository for audit_logs."""
from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import AuditLog


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields) -> AuditLog:
        entry = AuditLog(**fields)
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def detach_source(self, source_id: int) -> None:
        """Nulls the source reference on this source's audit rows, keeping the
        events themselves. Used before deleting a source row so the trail never
        points at an id that no longer exists - the schema's ON DELETE SET NULL
        cannot be relied on here, since SQLite enforces foreign keys only when
        `PRAGMA foreign_keys=ON`, which this app does not set."""
        await self.session.execute(
            update(AuditLog).where(AuditLog.source_id == source_id).values(source_id=None)
        )
        await self.session.flush()

    async def list_for_source(self, source_id: int, limit: int = 100) -> list[AuditLog]:
        result = await self.session.execute(
            select(AuditLog).where(AuditLog.source_id == source_id).order_by(AuditLog.id.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def list_recent(self, limit: int = 100) -> list[AuditLog]:
        result = await self.session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
        return list(result.scalars().all())
