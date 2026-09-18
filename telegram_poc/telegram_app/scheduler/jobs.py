"""Background jobs: 12h access reconciliation, collection, connection health,
notification processing.

No-overlap / idempotency guarantee: every job is registered with
`max_instances=1` (see `setup_scheduler`), so APScheduler itself refuses to
start a second run of the same job while one is still in flight - this is
the mechanism satisfying "do not run multiple copies of the same job for one
source simultaneously," rather than a hand-rolled lock.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from telegram_app.config import Settings
from telegram_app.database.database import async_session_factory
from telegram_app.database.models import AccessRequestStatus, SourceAccessStatus, TelegramAccount
from telegram_app.database.repositories.access_request_repository import AccessRequestRepository
from telegram_app.database.repositories.scheduler_job_repository import SchedulerJobRepository
from telegram_app.database.repositories.source_repository import SourceRepository
from telegram_app.notifications.manager import notification_manager
from telegram_app.services import audit_service as audit_events
from telegram_app.services import notification_service as notif_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.monitoring_service import MonitoringService
from telegram_app.services.notification_service import NotificationService
from telegram_app.telegram import access_manager, join_request_manager
from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.discovery import IdentifierKind, normalize_identifier

logger = logging.getLogger("telegram_service.scheduler")


async def reconcile_pending_access(client_manager: TelegramClientManager, settings: Settings) -> None:
    """JOB 1 - mandatory 12h reconciliation, the source of truth fallback for
    join-request status (event_handler.py is only a best-effort fast path)."""
    async with async_session_factory() as session:
        job_repo = SchedulerJobRepository(session)
        run = await job_repo.start_run("reconcile_pending_access")
        await session.commit()

        access_requests = AccessRequestRepository(session)
        sources = SourceRepository(session)
        audit = AuditService(session)
        notifications = NotificationService(session)

        checked = approved = rejected = errors = 0

        # Single upfront check, not one per pending request: if the session is
        # already known invalid this returns instantly (no RPC), and if it's
        # merely unreachable there is no point attempting every request only
        # to fail identically on each one - either way, do not hammer Telegram.
        ok, err = await client_manager.verify_authorized()
        if not ok:
            logger.warning("reconciliation: telegram not authorized (%s) - skipping this cycle", err)
            await job_repo.finish_run(
                run, "COMPLETED", {"checked": 0, "approved": 0, "rejected": 0, "errors": 1, "skipped_reason": err}
            )
            await session.commit()
            return

        pending = await access_requests.list_pending()

        for request in pending:
            source = await sources.get(request.source_id)
            if source is None:
                continue

            if client_manager.session_invalid:
                # Became invalid mid-cycle (e.g. a cached-authorized session
                # revoked between requests) - stop instead of hammering the
                # remaining pending requests with a now-known-bad key.
                logger.warning("reconciliation: telegram session invalidated mid-cycle - stopping this cycle early")
                errors += 1
                break

            normalized = normalize_identifier(source.identifier)
            result = await join_request_manager.check_pending_status(
                client_manager.client,
                username=normalized.value if normalized.kind == IdentifierKind.USERNAME else source.username,
                invite_hash=normalized.value if normalized.kind == IdentifierKind.INVITE_HASH else None,
            )
            checked += 1
            now = datetime.now(timezone.utc)
            next_check = now + timedelta(hours=settings.access_reconciliation_interval_hours)

            if result.status == SourceAccessStatus.JOIN_REQUEST_APPROVED:
                await access_requests.update(
                    request,
                    status=AccessRequestStatus.APPROVED.value,
                    last_checked_at=now,
                    next_check_at=None,
                    approved_at=now,
                    attempt_count=request.attempt_count + 1,
                )
                access_manager.transition(SourceAccessStatus(source.access_status), SourceAccessStatus.JOIN_REQUEST_APPROVED)
                access_manager.transition(SourceAccessStatus.JOIN_REQUEST_APPROVED, SourceAccessStatus.JOINED)
                update_fields = {"access_status": SourceAccessStatus.JOINED.value, "status_reason": "access approved and membership verified"}
                if result.telegram_entity_id:
                    update_fields["telegram_entity_id"] = result.telegram_entity_id
                await sources.update(source, **update_fields)
                await audit.log(audit_events.ACCESS_GRANTED, source_id=source.id)
                await notifications.create(
                    notif_events.EVENT_ACCESS_GRANTED,
                    source=source,
                    previous_status=SourceAccessStatus.JOIN_REQUEST_PENDING.value,
                    new_status=SourceAccessStatus.JOINED.value,
                )
                approved += 1
                await session.commit()

                # Section 9: monitoring must start automatically, only now that
                # access is verified - never while still pending.
                monitoring = MonitoringService(session, client_manager, settings)
                await monitoring.start_monitoring(source.id)

            elif result.status == SourceAccessStatus.JOIN_REQUEST_REJECTED:
                await access_requests.update(
                    request,
                    status=AccessRequestStatus.REJECTED.value,
                    last_checked_at=now,
                    next_check_at=None,
                    rejected_at=now,
                    error_message=result.reason,
                    attempt_count=request.attempt_count + 1,
                )
                access_manager.transition(SourceAccessStatus(source.access_status), SourceAccessStatus.JOIN_REQUEST_REJECTED)
                await sources.update(
                    source,
                    access_status=SourceAccessStatus.JOIN_REQUEST_REJECTED.value,
                    status_reason=result.reason,
                    monitoring_enabled=False,
                )
                await audit.log(audit_events.ACCESS_REJECTED, source_id=source.id, details={"reason": result.reason})
                await notifications.create(
                    notif_events.EVENT_ACCESS_REJECTED,
                    source=source,
                    previous_status=SourceAccessStatus.JOIN_REQUEST_PENDING.value,
                    new_status=SourceAccessStatus.JOIN_REQUEST_REJECTED.value,
                )
                rejected += 1
                await session.commit()

            else:
                error_msg = result.reason if result.status.name == "ERROR" else None
                await access_requests.update(
                    request,
                    last_checked_at=now,
                    next_check_at=next_check,
                    error_message=error_msg,
                    attempt_count=request.attempt_count + 1,
                )
                if error_msg:
                    errors += 1
                await audit.log(
                    audit_events.JOIN_REQUEST_STATUS_CHANGED,
                    source_id=source.id,
                    success=error_msg is None,
                    details={"status": result.status.value, "reason": result.reason},
                )
                await session.commit()
                if result.session_invalid:
                    client_manager.mark_session_invalid(result.reason or "session invalidated during reconciliation")
                    logger.warning("reconciliation: telegram session invalidated - stopping this cycle early")
                    break

        await job_repo.finish_run(
            run, "COMPLETED", {"checked": checked, "approved": approved, "rejected": rejected, "errors": errors}
        )
        await session.commit()
        logger.info(
            "reconcile_pending_access completed: checked=%d approved=%d rejected=%d errors=%d",
            checked, approved, rejected, errors,
        )


async def run_collection_cycle(client_manager: TelegramClientManager, settings: Settings) -> None:
    """JOB 2 - collects new messages for every source currently in MONITORING."""
    async with async_session_factory() as session:
        job_repo = SchedulerJobRepository(session)
        run = await job_repo.start_run("run_collection_cycle")
        await session.commit()

        sources = SourceRepository(session)
        targets = await sources.list_monitoring_enabled()

        results = []
        for source in targets:
            monitoring = MonitoringService(session, client_manager, settings)
            try:
                result = await monitoring.run_collection_for_source(source.id)
                results.append(result)
            except Exception:  # noqa: BLE001 - one source's failure must not abort the cycle for others
                logger.exception("collection cycle failed for source %s", source.id)
                await session.rollback()

        await job_repo.finish_run(run, "COMPLETED", {"sources_processed": len(results)})
        await session.commit()
        logger.info("run_collection_cycle completed: sources_processed=%d", len(results))


async def connection_health_check(client_manager: TelegramClientManager, settings: Settings) -> None:
    """JOB 3 - checks the Telegram connection and reconnects if necessary."""
    ok, err = await client_manager.ensure_connected()
    async with async_session_factory() as session:
        result = await session.execute(select(TelegramAccount).limit(1))
        account = result.scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if account is None:
            session.add(
                TelegramAccount(
                    session_path=settings.telegram_session_path,
                    is_authenticated=await client_manager.is_authenticated() if ok else False,
                    connected_at=now if ok else None,
                    last_seen_at=now,
                )
            )
        else:
            account.is_authenticated = await client_manager.is_authenticated() if ok else False
            account.last_seen_at = now
            if ok:
                account.connected_at = now
        await session.commit()
    if not ok:
        logger.warning("connection_health_check: telegram client not connected (%s)", err)


async def process_notifications(settings: Settings) -> None:
    """JOB 4 - dispatches any persisted-but-unprocessed notifications to
    registered providers (websocket/webhook/email/etc - none hard-coded)."""
    async with async_session_factory() as session:
        notifications = NotificationService(session)
        pending = await notifications.repo.list_unprocessed()
        for notification in pending:
            await notification_manager.dispatch(notification.payload)
            await notifications.repo.mark_processed(notification)
        if pending:
            await session.commit()
            logger.info("process_notifications: dispatched %d notification(s)", len(pending))


def setup_scheduler(client_manager: TelegramClientManager, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(executors={"default": AsyncIOExecutor()}, timezone="UTC")

    scheduler.add_job(
        reconcile_pending_access,
        "interval",
        hours=settings.access_reconciliation_interval_hours,
        args=[client_manager, settings],
        id="reconcile_pending_access",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_collection_cycle,
        "interval",
        minutes=settings.collection_interval_minutes,
        args=[client_manager, settings],
        id="run_collection_cycle",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        connection_health_check,
        "interval",
        minutes=settings.connection_health_interval_minutes,
        args=[client_manager, settings],
        id="connection_health_check",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        process_notifications,
        "interval",
        minutes=settings.notification_processing_interval_minutes,
        args=[settings],
        id="process_notifications",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    return scheduler
