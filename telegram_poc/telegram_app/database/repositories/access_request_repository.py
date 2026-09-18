"""Repository for telegram_access_requests."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import AccessRequestStatus, TelegramAccessRequest


class AccessRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields) -> TelegramAccessRequest:
        request = TelegramAccessRequest(**fields)
        self.session.add(request)
        await self.session.flush()
        return request

    async def get_active_for_source(self, source_id: int) -> TelegramAccessRequest | None:
        """The most recent non-terminal (still PENDING) request for a source, if any.

        Used to enforce "do not create duplicate join requests" - callers must
        check this before submitting a new one.
        """
        result = await self.session.execute(
            select(TelegramAccessRequest)
            .where(
                TelegramAccessRequest.source_id == source_id,
                TelegramAccessRequest.status == AccessRequestStatus.PENDING.value,
            )
            .order_by(TelegramAccessRequest.id.desc())
        )
        return result.scalars().first()

    async def list_pending(self) -> list[TelegramAccessRequest]:
        result = await self.session.execute(
            select(TelegramAccessRequest).where(TelegramAccessRequest.status == AccessRequestStatus.PENDING.value)
        )
        return list(result.scalars().all())

    async def update(self, request: TelegramAccessRequest, **fields) -> TelegramAccessRequest:
        for key, value in fields.items():
            setattr(request, key, value)
        await self.session.flush()
        return request
