"""Repository for telegram_entities - a resolution cache, never proof of access."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import TelegramEntity


class EntityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: str) -> TelegramEntity | None:
        result = await self.session.execute(select(TelegramEntity).where(TelegramEntity.telegram_id == telegram_id))
        return result.scalar_one_or_none()

    async def upsert(self, telegram_id: str, **fields) -> TelegramEntity:
        entity = await self.get_by_telegram_id(telegram_id)
        if entity is None:
            entity = TelegramEntity(telegram_id=telegram_id, **fields)
            self.session.add(entity)
        else:
            for key, value in fields.items():
                setattr(entity, key, value)
        await self.session.flush()
        return entity
