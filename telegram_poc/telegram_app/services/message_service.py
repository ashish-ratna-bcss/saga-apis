"""Message + media persistence: dedup, evidence preservation, checkpointing.

Each message is committed in its own small transaction so a mid-batch
failure leaves every already-persisted message durable and the checkpoint
accurate - restarting the app (or the next scheduled cycle) resumes exactly
where it left off, never re-inserting what is already stored (the DB unique
constraint on (source_id, telegram_message_id) is the actual dedup
guarantee; this code never assumes it succeeded without checking).

Two persistence entry points share the same per-message write path
(`_persist_one`) but advance different progress trackers:
  - `persist_collected_messages` advances the source's `collection_checkpoints`
    row - the live, forward-only incremental monitoring pipeline's anchor.
  - `persist_backfill_batch` advances a `BackfillJob`'s own
    `checkpoint_message_id` instead. Backfill walks *backward* into older
    history and must never touch the forward monitoring checkpoint.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.config import Settings
from telegram_app.database.models import BackfillJob, CollectionMethod, ProcessingStatus, TelegramMessage
from telegram_app.database.repositories.backfill_repository import BackfillJobRepository
from telegram_app.database.repositories.checkpoint_repository import CheckpointRepository
from telegram_app.database.repositories.media_repository import MediaRepository
from telegram_app.database.repositories.message_repository import MessageRepository
from telegram_app.telegram.collector import NormalizedMessage

logger = logging.getLogger("telegram_service.services.message")


@dataclass
class CollectionCycleMetrics:
    messages_found: int = 0
    messages_inserted: int = 0
    messages_skipped: int = 0
    media_found: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "messages_found": self.messages_found,
            "messages_inserted": self.messages_inserted,
            "messages_skipped": self.messages_skipped,
            "media_found": self.media_found,
            "errors": self.errors,
        }


def _write_raw_evidence(base_path: str, source_id: int, telegram_message_id: int, raw_json: dict) -> str:
    directory = Path(base_path) / str(source_id)
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / f"{telegram_message_id}.json"
    file_path.write_text(json.dumps(raw_json, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    return str(file_path)


class MessageService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.messages = MessageRepository(session)
        self.media = MediaRepository(session)
        self.checkpoints = CheckpointRepository(session)
        self.backfill_jobs = BackfillJobRepository(session)

    async def _persist_one(self, source_id: int, normalized: NormalizedMessage, collection_method: str) -> tuple[bool, int]:
        """Writes evidence + the message row + its media rows (flush only,
        no commit - the caller commits together with its own progress
        tracker update, exactly like the pre-existing single-checkpoint
        path did). Returns (inserted, media_count)."""
        raw_ref = (
            _write_raw_evidence(
                self.settings.raw_evidence_storage_path, source_id, normalized.telegram_message_id, normalized.raw_json
            )
            if self.settings.raw_evidence_enabled
            else None
        )
        inserted = await self.messages.upsert_ignore_duplicate(
            source_id=source_id,
            telegram_message_id=normalized.telegram_message_id,
            sender_id=normalized.sender_id,
            sender_username=normalized.sender_username,
            sender_display_name=normalized.sender_display_name,
            message_date=normalized.message_date,
            edit_date=normalized.edit_date,
            text=normalized.text,
            views=normalized.views,
            forwards=normalized.forwards,
            reply_count=normalized.reply_count,
            grouped_id=normalized.grouped_id,
            media_type=normalized.media_type,
            source_url=normalized.source_url,
            raw_data_hash=normalized.raw_data_hash,
            raw_data_ref=raw_ref,
            processing_status=ProcessingStatus.RAW.value,
            collection_method=collection_method,
        )

        media_count = 0
        if inserted:
            message_row = await self.messages.get_by_source_and_telegram_id(source_id, normalized.telegram_message_id)
            for media_item in normalized.media:
                await self.media.create(
                    message_id=message_row.id,
                    media_type=media_item.media_type,
                    telegram_media_id=media_item.telegram_media_id,
                    filename=media_item.filename,
                    mime_type=media_item.mime_type,
                    file_size=media_item.file_size,
                    local_path=media_item.local_path,
                    sha256=media_item.sha256,
                    downloaded=media_item.downloaded,
                    downloaded_at=media_item.downloaded_at,
                )
                media_count += 1
        return inserted, media_count

    async def persist_collected_messages(
        self, source_id: int, normalized_messages: list[NormalizedMessage], collection_method: str = CollectionMethod.MONITORING.value
    ) -> CollectionCycleMetrics:
        metrics = CollectionCycleMetrics(messages_found=len(normalized_messages))
        checkpoint = await self.checkpoints.get_or_create(source_id)
        last_persisted_id = checkpoint.last_message_id

        for normalized in normalized_messages:
            try:
                inserted, media_count = await self._persist_one(source_id, normalized, collection_method)
                if inserted:
                    metrics.messages_inserted += 1
                    metrics.media_found += media_count
                else:
                    metrics.messages_skipped += 1

                last_persisted_id = max(last_persisted_id, normalized.telegram_message_id)
                await self.checkpoints.record_success(checkpoint, last_persisted_id)
                await self.session.commit()
            except Exception as exc:  # noqa: BLE001 - persisted as a checkpoint error, not a crash
                await self.session.rollback()
                metrics.errors += 1
                logger.exception("failed to persist message %s for source %s", normalized.telegram_message_id, source_id)
                checkpoint = await self.checkpoints.get_or_create(source_id)
                await self.checkpoints.record_error(checkpoint, f"{exc.__class__.__name__}: {exc}")
                await self.session.commit()
                break  # stop this cycle; already-persisted messages remain durable, next cycle resumes from checkpoint

        return metrics

    async def persist_backfill_batch(
        self, source_id: int, normalized_messages: list[NormalizedMessage], backfill_job: BackfillJob
    ) -> CollectionCycleMetrics:
        """Same restart-safe, per-message-commit persistence as
        `persist_collected_messages`, but tracks progress on `backfill_job`
        (the oldest message id seen so far) instead of the source's live
        `collection_checkpoints` row."""
        metrics = CollectionCycleMetrics(messages_found=len(normalized_messages))

        for normalized in normalized_messages:
            try:
                inserted, media_count = await self._persist_one(
                    source_id, normalized, CollectionMethod.HISTORICAL_BACKFILL.value
                )
                if inserted:
                    metrics.messages_inserted += 1
                    metrics.media_found += media_count
                else:
                    metrics.messages_skipped += 1

                backfill_job.messages_found += 1
                backfill_job.messages_inserted += 1 if inserted else 0
                backfill_job.messages_skipped += 0 if inserted else 1
                if backfill_job.checkpoint_message_id is None or normalized.telegram_message_id < backfill_job.checkpoint_message_id:
                    backfill_job.checkpoint_message_id = normalized.telegram_message_id
                await self.backfill_jobs.update(backfill_job)
                await self.session.commit()
            except Exception as exc:  # noqa: BLE001 - recorded on the job, not a crash
                await self.session.rollback()
                metrics.errors += 1
                logger.exception(
                    "failed to persist backfill message %s for source %s (job %s)",
                    normalized.telegram_message_id, source_id, backfill_job.id,
                )
                backfill_job = await self.backfill_jobs.get(backfill_job.id)
                backfill_job.error_count += 1
                backfill_job.error_message = f"{exc.__class__.__name__}: {exc}"
                await self.backfill_jobs.update(backfill_job)
                await self.session.commit()
                break

        return metrics

    async def persist_single_message(self, source_id: int, normalized: NormalizedMessage, collection_method: str) -> bool:
        """One-off persistence for a single, out-of-band message (e.g.
        promoting a search result via POST /api/telegram/search/results/save).
        Deliberately does not touch `collection_checkpoints` - advancing the
        live monitoring checkpoint to an arbitrary message id picked from a
        search result (which may be newer than what the incremental
        collector has actually walked through sequentially) would make it
        silently skip everything in between on its next cycle."""
        inserted, _media_count = await self._persist_one(source_id, normalized, collection_method)
        await self.session.commit()
        return inserted

    async def get_message(self, message_id: int) -> TelegramMessage | None:
        return await self.messages.get(message_id)

    async def list_for_source(self, source_id: int, **kwargs) -> list[TelegramMessage]:
        return await self.messages.list_for_source(source_id, **kwargs)

    async def search(self, **kwargs) -> list[TelegramMessage]:
        return await self.messages.search(**kwargs)
