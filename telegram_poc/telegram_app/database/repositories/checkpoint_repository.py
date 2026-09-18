"""Repository for collection_checkpoints - the collector's restart-safety anchor."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import CollectionCheckpoint


class CheckpointRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, source_id: int) -> CollectionCheckpoint:
        result = await self.session.execute(
            select(CollectionCheckpoint).where(CollectionCheckpoint.source_id == source_id)
        )
        checkpoint = result.scalar_one_or_none()
        if checkpoint is None:
            checkpoint = CollectionCheckpoint(source_id=source_id, last_message_id=0)
            self.session.add(checkpoint)
            await self.session.flush()
        return checkpoint

    async def record_success(self, checkpoint: CollectionCheckpoint, last_message_id: int) -> CollectionCheckpoint:
        """Advances the checkpoint only as far as what was actually persisted.

        Never called with an id lower than what is already stored - the
        collector only calls this after messages up to that id are committed.
        """
        now = datetime.now(timezone.utc)
        if last_message_id > checkpoint.last_message_id:
            checkpoint.last_message_id = last_message_id
        checkpoint.last_collected_at = now
        checkpoint.last_success_at = now
        await self.session.flush()
        return checkpoint

    async def record_error(self, checkpoint: CollectionCheckpoint, error_message: str) -> CollectionCheckpoint:
        checkpoint.last_error_at = datetime.now(timezone.utc)
        checkpoint.last_error_message = error_message
        checkpoint.error_count += 1
        await self.session.flush()
        return checkpoint
