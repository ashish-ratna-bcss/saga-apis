"""Audit trail writes. Every significant operation in the system calls this."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.repositories.audit_repository import AuditRepository

# Canonical event type constants (section 19 of the spec) - kept as plain
# strings, not a strict enum, so future OSINT-pipeline components can log
# their own event types through the same table without a schema change.
TELEGRAM_AUTHENTICATED = "TELEGRAM_AUTHENTICATED"
SOURCE_REGISTERED = "SOURCE_REGISTERED"
SOURCE_RESOLVED = "SOURCE_RESOLVED"
ACCESS_CHECKED = "ACCESS_CHECKED"
JOIN_REQUEST_SUBMITTED = "JOIN_REQUEST_SUBMITTED"
JOIN_REQUEST_STATUS_CHANGED = "JOIN_REQUEST_STATUS_CHANGED"
ACCESS_GRANTED = "ACCESS_GRANTED"
ACCESS_REJECTED = "ACCESS_REJECTED"
MONITORING_STARTED = "MONITORING_STARTED"
MONITORING_STOPPED = "MONITORING_STOPPED"
MESSAGE_COLLECTION_STARTED = "MESSAGE_COLLECTION_STARTED"
MESSAGE_COLLECTION_COMPLETED = "MESSAGE_COLLECTION_COMPLETED"
MESSAGE_COLLECTION_FAILED = "MESSAGE_COLLECTION_FAILED"
MEDIA_DOWNLOADED = "MEDIA_DOWNLOADED"
ERROR_OCCURRED = "ERROR_OCCURRED"

# Discovery/search/backfill additions.
SEARCH_GLOBAL = "SEARCH_GLOBAL"
SEARCH_CHANNEL = "SEARCH_CHANNEL"
CHANNEL_DISCOVERY = "CHANNEL_DISCOVERY"
CHANNEL_REGISTERED_FROM_DISCOVERY = "CHANNEL_REGISTERED_FROM_DISCOVERY"
SEARCH_RESULT_SAVED = "SEARCH_RESULT_SAVED"
BACKFILL_STARTED = "BACKFILL_STARTED"
BACKFILL_COMPLETED = "BACKFILL_COMPLETED"
BACKFILL_FAILED = "BACKFILL_FAILED"

# Telegram URL classification / linked-channel discovery, and the
# account-unauthorized case distinct from a generic ACCESS_CHECKED failure.
TELEGRAM_URL_CLASSIFIED = "TELEGRAM_URL_CLASSIFIED"
TELEGRAM_ACCOUNT_UNAUTHORIZED = "TELEGRAM_ACCOUNT_UNAUTHORIZED"

# Bot-start workflow + explicit invite-link join (bot_service.py /
# SourceService.join_by_invite). Distinct from the pre-existing
# JOIN_REQUEST_SUBMITTED (used by SourceService.request_access, untouched)
# because these are logged at a different, newer entry point with finer
# granularity, per the requirement that added them by name.
TELEGRAM_BOT_DETECTED = "TELEGRAM_BOT_DETECTED"
TELEGRAM_BOT_START_REQUESTED = "TELEGRAM_BOT_START_REQUESTED"
TELEGRAM_BOT_START_COMPLETED = "TELEGRAM_BOT_START_COMPLETED"
TELEGRAM_BOT_START_FAILED = "TELEGRAM_BOT_START_FAILED"
TELEGRAM_LINK_DISCOVERED = "TELEGRAM_LINK_DISCOVERED"
TELEGRAM_INVITE_RESOLVED = "TELEGRAM_INVITE_RESOLVED"
TELEGRAM_JOIN_REQUESTED = "TELEGRAM_JOIN_REQUESTED"
TELEGRAM_JOIN_COMPLETED = "TELEGRAM_JOIN_COMPLETED"
TELEGRAM_JOIN_ALREADY_MEMBER = "TELEGRAM_JOIN_ALREADY_MEMBER"
TELEGRAM_JOIN_REQUEST_PENDING = "TELEGRAM_JOIN_REQUEST_PENDING"
TELEGRAM_JOIN_FAILED = "TELEGRAM_JOIN_FAILED"


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = AuditRepository(session)

    async def log(
        self,
        event_type: str,
        *,
        source_id: int | None = None,
        actor: str = "system",
        details: dict | None = None,
        success: bool = True,
    ) -> None:
        await self.repo.create(
            event_type=event_type,
            source_id=source_id,
            actor=actor,
            details=details or {},
            success=success,
        )

    async def detach_source(self, source_id: int) -> None:
        """Keeps this source's audit events but clears their source reference,
        for use immediately before the source row itself is deleted."""
        await self.repo.detach_source(source_id)
