"""Orchestrates the explicit Telegram bot-start workflow.

    bot source (SourceType.BOT)
            |
            v
    POST /api/telegram/bots/{id}/start   <- this module
            |
            v
    send exactly one "/start" (app/telegram/bot_interaction.py)
            |
            v
    bounded wait for the bot's one reply
            |
            v
    scan reply text + button URLs for Telegram links only
    (app/telegram/discovery/url_classifier.py - never fetches a non-
    Telegram URL, never executes a callback button)
            |
            v
    each Telegram link -> register (or refresh) as a DORMANT source
    (monitoring_enabled=False, discovery_type=BOT_RESPONSE) via the
    existing SourceService.register_source/check_access - never joined,
    never monitored automatically here

The operator decides what happens next via the existing, unmodified
endpoints: POST /api/sources/{id}/request-access and
POST /api/sources/{id}/monitoring/start.

Nothing about the bot's wider chat history is fetched or persisted - only
the one /start reply is inspected, and it is never written to
telegram_messages (see message_service.py's evidence-only persistence
policy, which this workflow does not touch).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.config import Settings
from telegram_app.database.models import SourceType, TelegramSource
from telegram_app.database.repositories.source_repository import SourceRepository
from telegram_app.services import audit_service as audit_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.source_service import SourceAlreadyExistsError, SourceService
from telegram_app.telegram import access_manager, bot_interaction
from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.discovery import IdentifierKind, classify_telegram_url, extract_telegram_urls, normalize_identifier

logger = logging.getLogger("telegram_service.services.bot")

DISCOVERY_TYPE_BOT_RESPONSE = "BOT_RESPONSE"

# Hard cap on how many links from one bot reply are ever resolved against
# Telegram - bounded, controlled discovery (see project non-goal: no
# recursive/unrestricted crawling), never an open-ended walk of everything a
# bot's response happens to contain.
MAX_LINKS_PER_BOT_RESPONSE = 10


class BotNotFoundError(Exception):
    pass


class SourceIsNotABotError(Exception):
    pass


@dataclass
class DiscoveredSource:
    source_id: int
    identifier: str
    access_status: str
    source_type: str
    already_existed: bool
    raw_url: str


@dataclass
class BotStartResult:
    source: TelegramSource
    status: str  # "started" | "timed_out" | "failed"
    response_text: str | None = None
    response_message_id: int | None = None
    response_date: datetime | None = None
    buttons: list = field(default_factory=list)  # list[bot_interaction.BotButton]
    discovered: list[DiscoveredSource] = field(default_factory=list)
    error: str | None = None


class BotService:
    def __init__(self, session: AsyncSession, client_manager: TelegramClientManager, settings: Settings) -> None:
        self.session = session
        self.client_manager = client_manager
        self.settings = settings
        self.sources = SourceRepository(session)
        self.audit = AuditService(session)
        self.source_service = SourceService(session, client_manager)

    async def start_bot(self, source_id: int) -> BotStartResult:
        source = await self.sources.get(source_id)
        if source is None:
            raise BotNotFoundError(f"source {source_id} not found")
        if source.source_type != SourceType.BOT.value:
            raise SourceIsNotABotError(
                f"source {source_id} is a '{source.source_type}', not a bot - the bot-start workflow only applies to SourceType.BOT sources"
            )

        ok, err = await self.client_manager.verify_authorized()
        if not ok:
            return await self._fail(source, err)

        normalized = normalize_identifier(source.identifier)
        try:
            entity = await access_manager.resolve_entity(self.client_manager.client, normalized)
        except Exception as exc:  # noqa: BLE001 - resolution failure ends this attempt cleanly, not a crash
            return await self._fail(source, str(exc))

        await self.audit.log(audit_events.TELEGRAM_BOT_START_REQUESTED, source_id=source.id)
        await self.session.commit()

        outcome = await bot_interaction.start_bot(
            self.client_manager.client, entity,
            timeout_seconds=self.settings.bot_start_timeout_seconds,
            poll_interval_seconds=self.settings.bot_start_poll_interval_seconds,
        )

        if outcome.session_invalid:
            self.client_manager.mark_session_invalid(outcome.error or "session invalidated during bot start")

        # Checked before `outcome.error`: a timeout also carries a
        # human-readable `error` string, but it must be reported as
        # "timed_out", not folded into the generic "failed" status.
        if outcome.timed_out:
            await self.audit.log(
                audit_events.TELEGRAM_BOT_START_FAILED, source_id=source.id, success=False,
                details={"error": "timed out waiting for bot response"},
            )
            await self.session.commit()
            return BotStartResult(source=source, status="timed_out", error="no response from bot within timeout")

        if outcome.error:
            return await self._fail(source, outcome.error)

        await self.audit.log(
            audit_events.TELEGRAM_BOT_START_COMPLETED, source_id=source.id,
            details={"response_message_id": outcome.response_message_id, "button_count": len(outcome.buttons)},
        )
        await self.session.commit()

        discovered = await self._discover_from_bot_response(source, outcome)

        return BotStartResult(
            source=source, status="started",
            response_text=outcome.response_text, response_message_id=outcome.response_message_id,
            response_date=outcome.response_date, buttons=outcome.buttons, discovered=discovered,
        )

    async def _fail(self, source: TelegramSource, error: str | None) -> BotStartResult:
        await self.audit.log(audit_events.TELEGRAM_BOT_START_FAILED, source_id=source.id, success=False, details={"error": error})
        await self.session.commit()
        return BotStartResult(source=source, status="failed", error=error)

    async def _discover_from_bot_response(self, bot_source: TelegramSource, outcome: bot_interaction.BotStartOutcome) -> list[DiscoveredSource]:
        """Read-only classification, then bounded, deduplicated
        registration of any Telegram channel/group/invite the bot's reply
        pointed at - never a join, never monitoring. Non-Telegram URLs
        (including any button URL that isn't t.me/telegram.me) are
        classified and discarded here, never fetched."""
        candidates = list(extract_telegram_urls(outcome.response_text or ""))
        for button in outcome.buttons:
            if button.url:
                candidates.append(classify_telegram_url(button.url))

        candidates = [c for c in candidates if c.is_telegram and c.identifier is not None][:MAX_LINKS_PER_BOT_RESPONSE]

        discovered: list[DiscoveredSource] = []
        for classification in candidates:
            await self.audit.log(
                audit_events.TELEGRAM_LINK_DISCOVERED, source_id=bot_source.id,
                details={"raw_url": classification.raw_url, "identifier": classification.identifier.normalized},
            )
            if classification.identifier.kind == IdentifierKind.INVITE_HASH:
                await self.audit.log(
                    audit_events.TELEGRAM_INVITE_RESOLVED, source_id=bot_source.id,
                    details={"identifier": classification.identifier.normalized},
                )

            existing = await self.sources.get_by_identifier(classification.identifier.normalized)
            already_existed = existing is not None
            if existing is None:
                try:
                    target = await self.source_service.register_source(
                        classification.identifier.normalized, monitoring_enabled=False,
                        discovered_from_source_id=bot_source.id, discovery_type=DISCOVERY_TYPE_BOT_RESPONSE,
                    )
                except SourceAlreadyExistsError as exc:
                    # Either registered concurrently between the
                    # get_by_identifier check above and this call, or already
                    # tracked under a different identifier spelling that
                    # resolves to the same channel - in the second case an
                    # identifier lookup finds nothing, so prefer the id the
                    # error carries.
                    target = None
                    if exc.existing_source_id is not None:
                        target = await self.sources.get(exc.existing_source_id)
                    if target is None:
                        target = await self.sources.get_by_identifier(classification.identifier.normalized)
                    if target is None:
                        logger.warning(
                            "bot-response link %s reported as duplicate but could not be located - skipping",
                            classification.raw_url,
                        )
                        continue
                    already_existed = True
            else:
                target = await self.source_service.check_access(existing.id)

            discovered.append(
                DiscoveredSource(
                    source_id=target.id, identifier=target.identifier, access_status=target.access_status,
                    source_type=target.source_type, already_existed=already_existed, raw_url=classification.raw_url,
                )
            )

        await self.session.commit()
        return discovered
