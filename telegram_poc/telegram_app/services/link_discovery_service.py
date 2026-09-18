"""Auto-discover Telegram channels/groups from links found in collected messages.

When a monitored source posts messages containing Telegram invite links
(``t.me/+hash``, ``t.me/joinchat/hash``, ``@username``, etc.), this service
extracts them, registers each as a new source, and — where possible — joins
or sends a join request automatically. The result is hands-free expansion of
monitored sources as the existing ones post invite links.

Hard rules:
  - Auto-discovered sources are always registered with
    ``monitoring_enabled=False`` to prevent recursive runaway (A → B → C → A).
    The operator enables monitoring explicitly after reviewing.
  - Links are capped per message (MAX_LINKS_PER_MESSAGE) and per collection
    cycle (MAX_LINKS_PER_CYCLE) to bound Telegram API usage.
  - Every action is audit-logged with full provenance.
  - A failure processing one link never blocks the rest, and a failure in
    link discovery never blocks or fails the primary message collection.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import SourceAccessStatus
from telegram_app.services import audit_service as audit_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.source_service import SourceAlreadyExistsError, SourceService
from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.collector import NormalizedMessage
from telegram_app.telegram.discovery import IdentifierKind, extract_telegram_urls

logger = logging.getLogger("telegram_service.services.link_discovery")

DISCOVERY_TYPE = "LINK_IN_MESSAGE"

# Audit event for the new auto-discovery flow (distinct from manual
# TELEGRAM_LINK_DISCOVERED which is fired by bot_service).
LINK_AUTO_DISCOVERED = "LINK_AUTO_DISCOVERED"
LINK_AUTO_JOIN_ATTEMPTED = "LINK_AUTO_JOIN_ATTEMPTED"
LINK_AUTO_JOIN_RESULT = "LINK_AUTO_JOIN_RESULT"


@dataclass
class LinkDiscoveryResult:
    """Outcome for one discovered link."""

    raw_url: str
    identifier: str | None
    action: str  # "registered", "already_exists", "joined", "join_requested", "skipped", "error"
    source_id: int | None = None
    reason: str | None = None


@dataclass
class LinkDiscoveryBatchResult:
    """Aggregate outcome for one collection cycle."""

    links_found: int = 0
    links_processed: int = 0
    sources_registered: int = 0
    joins_attempted: int = 0
    join_requests_sent: int = 0
    already_existed: int = 0
    errors: int = 0
    results: list[LinkDiscoveryResult] = field(default_factory=list)


class LinkDiscoveryService:
    """Extracts Telegram links from message text, registers + auto-joins."""

    MAX_LINKS_PER_MESSAGE = 10
    MAX_LINKS_PER_CYCLE = 50

    def __init__(
        self,
        session: AsyncSession,
        client_manager: TelegramClientManager,
    ) -> None:
        self.session = session
        self.client_manager = client_manager
        self.source_service = SourceService(session, client_manager)
        self.audit = AuditService(session)

    async def process_batch(
        self,
        messages: list[NormalizedMessage],
        source_id: int,
    ) -> LinkDiscoveryBatchResult:
        """Process all messages from one collection cycle.

        Extracts Telegram links, deduplicates across the batch, registers
        new sources, and auto-joins/requests access. Fire-and-forget: errors
        are logged and counted but never raised.
        """
        batch = LinkDiscoveryBatchResult()
        seen_identifiers: set[str] = set()

        for message in messages:
            if not message.text:
                continue

            classifications = [
                c
                for c in extract_telegram_urls(message.text)
                if c.is_telegram and c.identifier is not None
            ]
            batch.links_found += len(classifications)

            for classification in classifications[: self.MAX_LINKS_PER_MESSAGE]:
                if batch.links_processed >= self.MAX_LINKS_PER_CYCLE:
                    logger.debug(
                        "hit per-cycle link cap (%d) for source %d — stopping",
                        self.MAX_LINKS_PER_CYCLE,
                        source_id,
                    )
                    break

                identifier = classification.identifier
                # Dedup within this batch (same link in multiple messages)
                dedup_key = identifier.normalized
                if dedup_key in seen_identifiers:
                    continue
                seen_identifiers.add(dedup_key)

                result = await self._process_one_link(
                    raw_url=classification.raw_url,
                    identifier=identifier,
                    source_id=source_id,
                    message_id=message.telegram_message_id,
                )
                batch.links_processed += 1
                batch.results.append(result)

                if result.action == "already_exists":
                    batch.already_existed += 1
                elif result.action == "error":
                    batch.errors += 1
                elif result.action in ("registered", "joined", "join_requested"):
                    batch.sources_registered += 1
                    if result.action == "joined":
                        batch.joins_attempted += 1
                    elif result.action == "join_requested":
                        batch.join_requests_sent += 1

        if batch.links_found > 0:
            logger.info(
                "link discovery for source %d: found=%d processed=%d registered=%d joined=%d "
                "join_requested=%d already_existed=%d errors=%d",
                source_id,
                batch.links_found,
                batch.links_processed,
                batch.sources_registered,
                batch.joins_attempted,
                batch.join_requests_sent,
                batch.already_existed,
                batch.errors,
            )

        return batch

    async def _process_one_link(
        self,
        *,
        raw_url: str,
        identifier,
        source_id: int,
        message_id: int,
    ) -> LinkDiscoveryResult:
        """Register one link as a source, then auto-join/request if needed."""
        try:
            return await self._register_and_join(
                raw_url=raw_url,
                identifier=identifier,
                source_id=source_id,
                message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "link discovery error for %s (source %d, msg %d): %s",
                raw_url,
                source_id,
                message_id,
                exc,
            )
            await self.audit.log(
                LINK_AUTO_DISCOVERED,
                source_id=source_id,
                success=False,
                details={
                    "raw_url": raw_url,
                    "identifier": identifier.normalized if identifier else None,
                    "error": str(exc),
                    "message_id": message_id,
                },
            )
            return LinkDiscoveryResult(
                raw_url=raw_url,
                identifier=identifier.normalized if identifier else None,
                action="error",
                reason=str(exc),
            )

    async def _register_and_join(
        self,
        *,
        raw_url: str,
        identifier,
        source_id: int,
        message_id: int,
    ) -> LinkDiscoveryResult:
        """Core logic: register → check result → join/request as appropriate."""
        normalized_id = identifier.normalized
        is_invite = identifier.kind == IdentifierKind.INVITE_HASH

        # -- Step 1: Try registering as a new source ----------------------- #
        # For invite links, use join_by_invite() which handles register +
        # join + request-access in a single call.
        if is_invite:
            return await self._handle_invite_link(
                raw_url=raw_url,
                identifier=identifier,
                source_id=source_id,
                message_id=message_id,
            )

        # For usernames / numeric IDs: register normally
        try:
            source = await self.source_service.register_source(
                identifier.original or normalized_id,
                monitoring_enabled=False,
                discovered_from_source_id=source_id,
                discovery_type=DISCOVERY_TYPE,
            )
        except SourceAlreadyExistsError as exc:
            await self.audit.log(
                LINK_AUTO_DISCOVERED,
                source_id=exc.existing_source_id or source_id,
                details={
                    "raw_url": raw_url,
                    "identifier": normalized_id,
                    "action": "already_exists",
                    "message_id": message_id,
                },
            )
            return LinkDiscoveryResult(
                raw_url=raw_url,
                identifier=normalized_id,
                action="already_exists",
                source_id=exc.existing_source_id,
                reason="source already registered",
            )

        await self.audit.log(
            LINK_AUTO_DISCOVERED,
            source_id=source.id,
            details={
                "raw_url": raw_url,
                "identifier": normalized_id,
                "discovered_from": source_id,
                "message_id": message_id,
                "access_status": source.access_status,
            },
        )

        # -- Step 2: Auto-request access if private ------------------------ #
        # register_source already called check_access internally, so
        # source.access_status reflects the live state.
        return await self._auto_request_if_needed(source, raw_url, normalized_id)

    async def _handle_invite_link(
        self,
        *,
        raw_url: str,
        identifier,
        source_id: int,
        message_id: int,
    ) -> LinkDiscoveryResult:
        """Use join_by_invite for t.me/+ links — it handles the full
        register + join + request-access flow in one call."""
        try:
            outcome = await self.source_service.join_by_invite(identifier.original or raw_url)
        except SourceAlreadyExistsError as exc:
            return LinkDiscoveryResult(
                raw_url=raw_url,
                identifier=identifier.normalized,
                action="already_exists",
                source_id=exc.existing_source_id,
                reason="source already registered",
            )
        except ValueError as exc:
            return LinkDiscoveryResult(
                raw_url=raw_url,
                identifier=identifier.normalized,
                action="error",
                reason=str(exc),
            )

        await self.audit.log(
            LINK_AUTO_JOIN_RESULT,
            source_id=outcome.source.id if outcome.source else source_id,
            details={
                "raw_url": raw_url,
                "identifier": identifier.normalized,
                "discovered_from": source_id,
                "message_id": message_id,
                "join_status": outcome.status,
                "reason": outcome.reason,
            },
        )

        action_map = {
            "joined": "joined",
            "already_member": "already_exists",
            "pending_approval": "join_requested",
            "already_pending": "already_exists",
            "failed": "error",
        }

        return LinkDiscoveryResult(
            raw_url=raw_url,
            identifier=identifier.normalized,
            action=action_map.get(outcome.status, "error"),
            source_id=outcome.source.id if outcome.source else None,
            reason=outcome.reason,
        )

    async def _auto_request_if_needed(
        self,
        source,
        raw_url: str,
        normalized_id: str,
    ) -> LinkDiscoveryResult:
        """If the newly registered source needs a join request, send it."""
        status = SourceAccessStatus(source.access_status)

        if status == SourceAccessStatus.JOIN_REQUEST_REQUIRED:
            try:
                await self.audit.log(
                    LINK_AUTO_JOIN_ATTEMPTED,
                    source_id=source.id,
                    details={"identifier": normalized_id, "raw_url": raw_url},
                )
                await self.source_service.request_access(source.id)
                # Refresh to get updated status
                refreshed = await self.source_service.get_source(source.id)
                final_status = SourceAccessStatus(refreshed.access_status) if refreshed else status

                if final_status in (
                    SourceAccessStatus.JOINED,
                    SourceAccessStatus.ACCESSIBLE,
                ):
                    return LinkDiscoveryResult(
                        raw_url=raw_url,
                        identifier=normalized_id,
                        action="joined",
                        source_id=source.id,
                        reason="auto-joined successfully",
                    )
                return LinkDiscoveryResult(
                    raw_url=raw_url,
                    identifier=normalized_id,
                    action="join_requested",
                    source_id=source.id,
                    reason="join request sent — awaiting admin approval",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-request-access failed for source %d: %s", source.id, exc)
                return LinkDiscoveryResult(
                    raw_url=raw_url,
                    identifier=normalized_id,
                    action="registered",
                    source_id=source.id,
                    reason=f"registered but auto-join failed: {exc}",
                )

        # Already accessible or any other state — just report as registered
        return LinkDiscoveryResult(
            raw_url=raw_url,
            identifier=normalized_id,
            action="registered",
            source_id=source.id,
            reason=f"registered with status {status.value}",
        )
