"""Application notifications - always persisted first; dispatch is optional/pluggable."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import Notification, TelegramSource
from telegram_app.database.repositories.notification_repository import NotificationRepository

EVENT_ACCESS_GRANTED = "TELEGRAM_ACCESS_GRANTED"
EVENT_ACCESS_REJECTED = "TELEGRAM_ACCESS_REJECTED"
EVENT_MONITORING_STARTED = "TELEGRAM_MONITORING_STARTED"
EVENT_COLLECTION_ERROR = "TELEGRAM_COLLECTION_ERROR"


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = NotificationRepository(session)

    async def create(
        self,
        event_type: str,
        *,
        source: TelegramSource | None,
        previous_status: str | None,
        new_status: str | None,
        extra: dict | None = None,
    ) -> Notification:
        payload = {
            "event_type": event_type,
            "source_id": source.id if source else None,
            "telegram_entity_id": source.telegram_entity_id if source else None,
            "source_name": (source.title or source.username or source.identifier) if source else None,
            "previous_status": previous_status,
            "new_status": new_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)
        return await self.repo.create(
            event_type=event_type,
            source_id=source.id if source else None,
            telegram_entity_id=source.telegram_entity_id if source else None,
            source_name=payload["source_name"],
            previous_status=previous_status,
            new_status=new_status,
            payload=payload,
        )

    async def list_notifications(self, limit: int = 50, offset: int = 0, unread_only: bool = False) -> list[Notification]:
        return await self.repo.list_all(limit=limit, offset=offset, unread_only=unread_only)

    async def mark_read(self, notification_id: int) -> Notification | None:
        notification = await self.repo.get(notification_id)
        if notification is None:
            return None
        return await self.repo.mark_read(notification)
