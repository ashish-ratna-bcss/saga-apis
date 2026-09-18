"""Starting/stopping monitoring and running one collection cycle for a source.

Only sources that are actually accessible (verified, not assumed) are ever
allowed into MONITORING - see access_manager's state machine for what states
can reach it.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.config import Settings
from telegram_app.database.models import SourceAccessStatus, TelegramSource
from telegram_app.database.repositories.source_repository import SourceRepository
from telegram_app.services import audit_service as audit_events
from telegram_app.services import notification_service as notif_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.link_discovery_service import LinkDiscoveryService
from telegram_app.services.message_service import MessageService
from telegram_app.services.notification_service import NotificationService
from telegram_app.telegram import access_manager, collector
from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE, TelegramClientManager
from telegram_app.telegram.discovery import normalize_identifier

logger = logging.getLogger("telegram_service.services.monitoring")


class MonitoringStateError(Exception):
    pass


def _log_structured(event: str, **fields) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str))


class MonitoringService:
    def __init__(self, session: AsyncSession, client_manager: TelegramClientManager, settings: Settings) -> None:
        self.session = session
        self.client_manager = client_manager
        self.settings = settings
        self.sources = SourceRepository(session)
        self.messages = MessageService(session, settings)
        self.link_discovery = LinkDiscoveryService(session, client_manager)
        self.audit = AuditService(session)
        self.notifications = NotificationService(session)

    async def start_monitoring(self, source_id: int) -> TelegramSource:
        source = await self._require_source(source_id)
        # Monitoring must only start once access is positively verified AND
        # the Telegram session backing that verification is still good.
        ok, err = await self.client_manager.verify_authorized()
        if not ok:
            raise MonitoringStateError(f"cannot start monitoring: {err}")
        current = SourceAccessStatus(source.access_status)
        if current not in (
            SourceAccessStatus.PUBLIC_ACCESSIBLE,
            SourceAccessStatus.ACCESSIBLE,
            SourceAccessStatus.JOINED,
            SourceAccessStatus.MONITORING,
        ):
            raise MonitoringStateError(
                f"source {source.id} is in state {current.value}; monitoring requires verified access first"
            )

        if current != SourceAccessStatus.MONITORING:
            access_manager.transition(current, SourceAccessStatus.MONITORING)
            await self.sources.update(
                source,
                access_status=SourceAccessStatus.MONITORING.value,
                monitoring_enabled=True,
                monitoring_started_at=datetime.now(timezone.utc),
            )
        else:
            await self.sources.update(source, monitoring_enabled=True)

        await self.audit.log(audit_events.MONITORING_STARTED, source_id=source.id)
        await self.session.commit()

        # Initial collection happens right away rather than waiting for the
        # next scheduled cycle (spec section 9: JOINED -> INITIAL COLLECTION -> MONITORING).
        await self.run_collection_for_source(source.id)
        return await self._require_source(source_id)

    async def stop_monitoring(self, source_id: int) -> TelegramSource:
        source = await self._require_source(source_id)
        await self.sources.update(source, monitoring_enabled=False)
        await self.audit.log(audit_events.MONITORING_STOPPED, source_id=source.id)
        await self.session.commit()
        return source

    async def get_monitoring_status(self, source_id: int) -> TelegramSource:
        return await self._require_source(source_id)

    async def run_collection_for_source(self, source_id: int) -> dict:
        source = await self._require_source(source_id)
        started_at = time.monotonic()
        started_wall = datetime.now(timezone.utc)

        # Verify the session before resolving the entity (ResolveUsernameRequest)
        # or collecting - a plain transient network hiccup just skips this
        # cycle (monitoring stays on, retried next cycle), but a genuinely
        # invalid session must stop and disable monitoring rather than being
        # silently retried forever.
        ok, err = await self.client_manager.verify_authorized()
        if not ok:
            await self.audit.log(audit_events.MESSAGE_COLLECTION_FAILED, source_id=source.id, success=False, details={"error": err})
            if self.client_manager.session_invalid:
                await self._transition_to_error(source, reason=err)
            await self.session.commit()
            return {"error": err}

        normalized_id = normalize_identifier(source.identifier)
        try:
            entity = await access_manager.resolve_entity(self.client_manager.client, normalized_id)
        except SESSION_INVALID_ERRORS as exc:
            reason = f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})"
            self.client_manager.mark_session_invalid(reason)
            await self.audit.log(
                audit_events.MESSAGE_COLLECTION_FAILED, source_id=source.id, success=False, details={"error": reason}
            )
            await self._transition_to_error(source, reason=reason)
            await self.session.commit()
            return {"error": reason}
        except Exception as exc:  # noqa: BLE001 - any other resolution failure ends this cycle cleanly
            await self.audit.log(
                audit_events.MESSAGE_COLLECTION_FAILED, source_id=source.id, success=False, details={"error": str(exc)}
            )
            await self.session.commit()
            return {"error": str(exc)}

        checkpoint = await self.messages.checkpoints.get_or_create(source.id)
        await self.audit.log(audit_events.MESSAGE_COLLECTION_STARTED, source_id=source.id, details={"since_message_id": checkpoint.last_message_id})

        outcome = await collector.collect_new_messages(
            self.client_manager.client,
            entity,
            min_id=checkpoint.last_message_id,
            limit=self.settings.collection_batch_limit,
            source_username=source.username,
        )

        if self.settings.media_download_enabled:
            for normalized in outcome.messages:
                if normalized.media and normalized.telethon_message is not None:
                    normalized.media = [
                        await collector.download_media(
                            self.client_manager.client,
                            normalized.telethon_message,
                            media_item,
                            storage_dir=self.settings.media_storage_path,
                            max_size_bytes=self.settings.maximum_media_size_bytes,
                            allowed_types=self.settings.allowed_media_types_list,
                        )
                        for media_item in normalized.media
                    ]

        metrics = await self.messages.persist_collected_messages(source.id, outcome.messages)

        checkpoint = await self.messages.checkpoints.get_or_create(source.id)
        await self.sources.update(
            source, last_message_id=checkpoint.last_message_id, last_collected_at=datetime.now(timezone.utc)
        )

        # -- Auto-discover Telegram links in collected messages ------------ #
        # Fire-and-forget: link discovery failures never block or fail the
        # primary message collection pipeline.
        link_discovery_result = None
        if outcome.messages:
            try:
                link_discovery_result = await self.link_discovery.process_batch(
                    outcome.messages, source.id
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "link discovery failed for source %d — primary collection unaffected",
                    source.id,
                )

        if outcome.session_invalid:
            self.client_manager.mark_session_invalid(outcome.error or SESSION_INVALID_MESSAGE)

        if outcome.error and ("access was revoked" in outcome.error or outcome.session_invalid):
            await self._transition_to_error(source, reason=outcome.error)
            await self.notifications.create(
                notif_events.EVENT_COLLECTION_ERROR,
                source=source,
                previous_status=SourceAccessStatus.MONITORING.value,
                new_status=SourceAccessStatus.ERROR.value,
                extra={"reason": outcome.error},
            )

        duration_ms = int((time.monotonic() - started_at) * 1000)
        result = {
            "source_id": source.id,
            "status": source.access_status,
            "started_at": started_wall.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "error": outcome.error,
            **metrics.as_dict(),
        }
        if link_discovery_result and link_discovery_result.links_found > 0:
            result["link_discovery"] = {
                "links_found": link_discovery_result.links_found,
                "links_processed": link_discovery_result.links_processed,
                "sources_registered": link_discovery_result.sources_registered,
                "joins_attempted": link_discovery_result.joins_attempted,
                "join_requests_sent": link_discovery_result.join_requests_sent,
            }

        await self.audit.log(
            audit_events.MESSAGE_COLLECTION_COMPLETED if not outcome.error else audit_events.MESSAGE_COLLECTION_FAILED,
            source_id=source.id,
            success=outcome.error is None,
            details=result,
        )
        await self.session.commit()
        _log_structured("collection_completed", **result)
        return result

    async def _transition_to_error(self, source: TelegramSource, *, reason: str | None) -> None:
        """Moves a MONITORING source to ERROR and disables monitoring in the
        same write - the state machine (access_manager.transition) is still
        the gate, never a bare boolean flip. A no-op if already ERROR."""
        current = SourceAccessStatus(source.access_status)
        if current != SourceAccessStatus.ERROR:
            access_manager.transition(current, SourceAccessStatus.ERROR)
        await self.sources.update(
            source, access_status=SourceAccessStatus.ERROR.value, monitoring_enabled=False, status_reason=reason
        )

    async def _require_source(self, source_id: int) -> TelegramSource:
        source = await self.sources.get(source_id)
        if source is None:
            raise ValueError(f"source {source_id} not found")
        return source
