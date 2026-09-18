"""Repository for telegram_media."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import TelegramMedia


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields) -> TelegramMedia:
        media = TelegramMedia(**fields)
        self.session.add(media)
        await self.session.flush()
        return media
