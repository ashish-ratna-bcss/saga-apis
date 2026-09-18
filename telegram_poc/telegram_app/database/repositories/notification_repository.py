"""Repository for notifications."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import Notification


class NotificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields) -> Notification:
        notification = Notification(**fields)
        self.session.add(notification)
        await self.session.flush()
        return notification

    async def get(self, notification_id: int) -> Notification | None:
        return await self.session.get(Notification, notification_id)

    async def list_all(self, limit: int = 50, offset: int = 0, unread_only: bool = False) -> list[Notification]:
        query = select(Notification)
        if unread_only:
            query = query.where(Notification.is_read.is_(False))
        query = query.order_by(Notification.id.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def list_unprocessed(self) -> list[Notification]:
        result = await self.session.execute(select(Notification).where(Notification.processed.is_(False)))
        return list(result.scalars().all())

    async def mark_read(self, notification: Notification) -> Notification:
        notification.is_read = True
        await self.session.flush()
        return notification

    async def mark_processed(self, notification: Notification) -> Notification:
        notification.processed = True
        await self.session.flush()
        return notification
