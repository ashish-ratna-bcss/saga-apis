"""Repository for telegram_sources."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import SourceAccessStatus, TelegramSource


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields) -> TelegramSource:
        source = TelegramSource(**fields)
        self.session.add(source)
        await self.session.flush()
        return source

    async def get(self, source_id: int) -> TelegramSource | None:
        return await self.session.get(TelegramSource, source_id)

    async def get_by_identifier(self, identifier: str) -> TelegramSource | None:
        result = await self.session.execute(select(TelegramSource).where(TelegramSource.identifier == identifier))
        return result.scalar_one_or_none()

    async def get_by_entity_id(self, telegram_entity_id: str) -> TelegramSource | None:
        result = await self.session.execute(
            select(TelegramSource).where(TelegramSource.telegram_entity_id == telegram_entity_id)
        )
        return result.scalar_one_or_none()

    async def get_first_other_by_entity_id(
        self, telegram_entity_id: str, *, excluding_source_id: int
    ) -> TelegramSource | None:
        """The oldest source, other than `excluding_source_id`, already tracking
        this Telegram entity. Deliberately not `scalar_one_or_none` like
        `get_by_entity_id`: databases written before entity-level dedup existed
        may already hold several rows for one channel, and this must report the
        winner rather than raise MultipleResultsFound on that history."""
        result = await self.session.execute(
            select(TelegramSource)
            .where(
                TelegramSource.telegram_entity_id == telegram_entity_id,
                TelegramSource.id != excluding_source_id,
            )
            .order_by(TelegramSource.id)
            .limit(1)
        )
        return result.scalars().first()

    async def list_all(self, limit: int = 100, offset: int = 0) -> list[TelegramSource]:
        result = await self.session.execute(select(TelegramSource).order_by(TelegramSource.id).limit(limit).offset(offset))
        return list(result.scalars().all())

    async def list_by_status(self, status: SourceAccessStatus | str) -> list[TelegramSource]:
        value = status.value if isinstance(status, SourceAccessStatus) else status
        result = await self.session.execute(select(TelegramSource).where(TelegramSource.access_status == value))
        return list(result.scalars().all())

    async def list_monitoring_enabled(self) -> list[TelegramSource]:
        result = await self.session.execute(
            select(TelegramSource).where(
                TelegramSource.monitoring_enabled.is_(True),
                TelegramSource.access_status == SourceAccessStatus.MONITORING.value,
            )
        )
        return list(result.scalars().all())

    async def update(self, source: TelegramSource, **fields) -> TelegramSource:
        for key, value in fields.items():
            setattr(source, key, value)
        source.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        return source

    async def delete(self, source: TelegramSource) -> None:
        await self.session.delete(source)
        await self.session.flush()

    async def commit(self) -> None:
        await self.session.commit()
