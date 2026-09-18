"""Repository for telegram_messages - dedup is enforced by the DB unique constraint."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import TelegramMessage


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_ignore_duplicate(self, **fields) -> bool:
        """Inserts a message, silently skipping it if (source_id, telegram_message_id) already exists.

        Returns True if a new row was inserted, False if it was a duplicate.
        Relies on the DB-level UNIQUE constraint as the source of truth, not a
        pre-check, to stay correct under concurrent/interrupted collection.
        """
        stmt = sqlite_insert(TelegramMessage).values(**fields)
        stmt = stmt.on_conflict_do_nothing(index_elements=["source_id", "telegram_message_id"])
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount > 0

    async def get_by_source_and_telegram_id(self, source_id: int, telegram_message_id: int) -> TelegramMessage | None:
        result = await self.session.execute(
            select(TelegramMessage).where(
                TelegramMessage.source_id == source_id,
                TelegramMessage.telegram_message_id == telegram_message_id,
            )
        )
        return result.scalar_one_or_none()

    async def get(self, message_id: int) -> TelegramMessage | None:
        return await self.session.get(TelegramMessage, message_id)

    async def list_for_source(
        self,
        source_id: int,
        limit: int = 50,
        offset: int = 0,
        keyword: str | None = None,
        sender_username: str | None = None,
        date_from=None,
        date_to=None,
        media_type: str | None = None,
    ) -> list[TelegramMessage]:
        query = select(TelegramMessage).where(TelegramMessage.source_id == source_id)
        if keyword:
            query = query.where(TelegramMessage.text.ilike(f"%{keyword}%"))
        if sender_username:
            query = query.where(TelegramMessage.sender_username == sender_username)
        if date_from:
            query = query.where(TelegramMessage.message_date >= date_from)
        if date_to:
            query = query.where(TelegramMessage.message_date <= date_to)
        if media_type:
            query = query.where(TelegramMessage.media_type == media_type)
        query = query.order_by(TelegramMessage.telegram_message_id.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def count_for_source(self, source_id: int) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(TelegramMessage).where(TelegramMessage.source_id == source_id)
        )
        return int(result.scalar_one())

    async def search(
        self,
        keyword: str | None = None,
        source_id: int | None = None,
        sender_username: str | None = None,
        date_from=None,
        date_to=None,
        media_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TelegramMessage]:
        """Application-level search over already-collected data.

        Independent from any Telegram discovery/search RPC - operates purely
        on normalized, previously-persisted rows.
        """
        query = select(TelegramMessage)
        if keyword:
            query = query.where(TelegramMessage.text.ilike(f"%{keyword}%"))
        if source_id is not None:
            query = query.where(TelegramMessage.source_id == source_id)
        if sender_username:
            query = query.where(TelegramMessage.sender_username == sender_username)
        if date_from:
            query = query.where(TelegramMessage.message_date >= date_from)
        if date_to:
            query = query.where(TelegramMessage.message_date <= date_to)
        if media_type:
            query = query.where(TelegramMessage.media_type == media_type)
        query = query.order_by(TelegramMessage.message_date.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return list(result.scalars().all())
