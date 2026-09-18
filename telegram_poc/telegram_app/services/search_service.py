"""Orchestrates the search/discovery layer: connects/verifies the Telegram
session, enforces access rules, applies the shared rate policy, audits every
call, and translates errors - so routes and app/telegram/discovery/* stay
simple (discovery has no DB/session/audit concerns of its own; routes never
touch Telethon).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.config import Settings
from telegram_app.database.models import CollectionMethod, SourceAccessStatus, SourceType, TelegramMessage, TelegramSource
from telegram_app.database.repositories.source_repository import SourceRepository
from telegram_app.observability import log_structured
from telegram_app.services import audit_service as audit_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.message_service import MessageService
from telegram_app.services.source_service import SourceAlreadyExistsError, SourceService
from telegram_app.telegram import access_manager, avatars, collector, rate_policy
from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.discovery import (
    channel_discovery,
    channel_search,
    global_search,
    normalize_identifier,
)
from telegram_app.telegram.errors import TelegramSearchError, translate_telegram_error

logger = logging.getLogger("telegram_service.services.search")

# The only access states a channel/source may legitimately be searched from -
# matches what monitoring_service requires to start monitoring, since both
# represent "this account can actually read this source's messages."
SEARCHABLE_STATUSES = {
    SourceAccessStatus.PUBLIC_ACCESSIBLE,
    SourceAccessStatus.ACCESSIBLE,
    SourceAccessStatus.JOINED,
    SourceAccessStatus.MONITORING,
}

@dataclass
class SkippedSource:
    source_id: int
    status: str | None
    reason: str


@dataclass
class MultiSourceSearchOutcome:
    items: list = field(default_factory=list)
    searched_source_ids: list[int] = field(default_factory=list)
    skipped: list[SkippedSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_ACCESS_DENIAL_CODES = {
    SourceAccessStatus.JOIN_REQUEST_REQUIRED: (
        "JOIN_REQUIRED",
        403,
        "This source requires joining before it can be searched. Register it and submit a join request first.",
    ),
    SourceAccessStatus.JOIN_REQUEST_PENDING: (
        "JOIN_PENDING",
        403,
        "A join request for this source is still pending Telegram's approval.",
    ),
    SourceAccessStatus.JOIN_REQUEST_REJECTED: (
        "ACCESS_REJECTED", 403, "The join request for this source was rejected.",
    ),
    SourceAccessStatus.ACCESS_DENIED: ("ACCESS_DENIED", 403, "This source is private and inaccessible to this account."),
    SourceAccessStatus.NOT_FOUND: ("NOT_FOUND", 404, "No Telegram source was found for this identifier."),
}


class SearchService:
    def __init__(self, session: AsyncSession, client_manager: TelegramClientManager, settings: Settings) -> None:
        self.session = session
        self.client_manager = client_manager
        self.settings = settings
        self.sources = SourceRepository(session)
        self.audit = AuditService(session)

    async def _require_session(self) -> None:
        ok, err = await self.client_manager.verify_authorized()
        if not ok:
            raise TelegramSearchError(code="NOT_AUTHENTICATED", message=err or "not authenticated", http_status=401, retryable=False)

    # -- global search ------------------------------------------------------ #

    async def global_search(self, query: str, **params) -> global_search.GlobalSearchOutcome:
        await self._require_session()
        started = time.monotonic()
        cache_key = rate_policy.make_search_cache_key("global_search", q=query, **{k: v for k, v in params.items() if v is not None})

        try:
            outcome = await rate_policy.run_bounded_search(
                cache_key, lambda: global_search.search_global(self.client_manager.client, query, **params)
            )
        except TelegramSearchError:
            await self._audit_search(audit_events.SEARCH_GLOBAL, query, success=False)
            raise
        except Exception as exc:  # noqa: BLE001 - translated into a stable error, then re-raised
            translated = translate_telegram_error(exc)
            await self._audit_search(audit_events.SEARCH_GLOBAL, query, success=False, details={"error": translated.code})
            raise translated from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        await self._audit_search(audit_events.SEARCH_GLOBAL, query, success=True, details={"result_count": len(outcome.items), "method": outcome.method_used})
        log_structured(
            logger, "search_completed", operation="global_search", query_length=len(query),
            method=outcome.method_used, result_count=len(outcome.items), duration_ms=duration_ms,
        )
        return outcome

    # -- channel search ------------------------------------------------------ #

    async def channel_search_by_source(self, source_id: int, query: str, **params) -> channel_search.ChannelSearchOutcome:
        """Search within an already-registered source - trusts its stored,
        previously-verified access_status rather than re-probing Telegram on
        every call, but only ever a *positive* stored status; anything else
        is refused immediately without contacting Telegram at all."""
        source = await self.sources.get(source_id)
        if source is None:
            raise TelegramSearchError(code="NOT_FOUND", message=f"source {source_id} not found", http_status=404)
        self._reject_if_bot(source.source_type)

        status = SourceAccessStatus(source.access_status)
        if status not in SEARCHABLE_STATUSES:
            self._raise_access_denied(status)

        await self._require_session()
        normalized = normalize_identifier(source.identifier)
        try:
            entity = await access_manager.resolve_entity(self.client_manager.client, normalized)
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc

        return await self._run_channel_search(
            entity, query, channel_title=source.title, channel_username=source.username, source_id=source_id, **params
        )

    async def channel_search_by_identifier(self, identifier: str, query: str, **params) -> channel_search.ChannelSearchOutcome:
        """Search an arbitrary (not necessarily registered) channel by
        identifier - always does a fresh, live access probe first since
        there is no stored state to trust. Never silently returns an empty
        result for a channel this account cannot actually read."""
        await self._require_session()
        normalized = normalize_identifier(identifier)
        access_result = await access_manager.check_access(self.client_manager.client, normalized)
        if access_result.session_invalid:
            self.client_manager.mark_session_invalid(access_result.reason or "session invalidated during access check")
            raise TelegramSearchError(code="TELEGRAM_SESSION_INVALID", message=access_result.reason, http_status=401)
        self._reject_if_bot(access_result.source_type)

        if access_result.status not in SEARCHABLE_STATUSES:
            self._raise_access_denied(access_result.status, reason=access_result.reason)

        return await self._run_channel_search(
            access_result.resolved_entity, query,
            channel_title=access_result.title, channel_username=access_result.username, source_id=None, **params
        )

    async def _run_channel_search(self, entity, query, *, channel_title, channel_username, source_id, **params):
        started = time.monotonic()
        try:
            outcome = await channel_search.search_channel(
                self.client_manager.client, entity, query, channel_title=channel_title, channel_username=channel_username, **params
            )
        except TelegramSearchError:
            await self._audit_search(audit_events.SEARCH_CHANNEL, query, source_id=source_id, success=False)
            raise
        except Exception as exc:  # noqa: BLE001
            translated = translate_telegram_error(exc)
            await self._audit_search(audit_events.SEARCH_CHANNEL, query, source_id=source_id, success=False, details={"error": translated.code})
            raise translated from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        await self._audit_search(audit_events.SEARCH_CHANNEL, query, source_id=source_id, success=True, details={"result_count": len(outcome.items)})
        log_structured(
            logger, "search_completed", operation="channel_search", source_id=source_id,
            query_length=len(query), result_count=len(outcome.items), duration_ms=duration_ms,
        )
        return outcome

    # -- multi-source scoped search -------------------------------------------- #

    async def search_selected_sources(self, source_ids: list[int] | None, query: str, **params) -> MultiSourceSearchOutcome:
        """Searches only the requested sources (or, if `source_ids` is
        omitted, every monitoring-enabled source) - never every source this
        account can see. A source whose stored status isn't currently
        SEARCHABLE_STATUSES (e.g. still JOIN_REQUEST_PENDING, or
        ACCESS_DENIED) is skipped and reported in `skipped`, not queried and
        never causes the whole call to fail. Reuses channel_search_by_source
        for every source actually searched - identical access rules,
        identical per-source audit logging - under the same bounded-
        concurrency/de-dup policy global search and discovery already use."""
        await self._require_session()

        skipped: list[SkippedSource] = []
        sources: list[TelegramSource] = []

        if source_ids:
            for sid in source_ids:
                source = await self.sources.get(sid)
                if source is None:
                    skipped.append(SkippedSource(source_id=sid, status=None, reason="source not found"))
                else:
                    sources.append(source)
        else:
            sources = await self.sources.list_monitoring_enabled()

        to_search: list[TelegramSource] = []
        for source in sources:
            status = SourceAccessStatus(source.access_status)
            if status not in SEARCHABLE_STATUSES:
                skipped.append(
                    SkippedSource(source_id=source.id, status=status.value, reason=f"source is {status.value}, not currently searchable")
                )
            else:
                to_search.append(source)

        outcome = MultiSourceSearchOutcome(skipped=skipped)
        if not to_search:
            return outcome

        # Sequential, not asyncio.gather: every search here shares this one
        # AsyncSession (for the access check + per-source audit log), and
        # SQLAlchemy's AsyncSession is not safe for concurrent use from
        # multiple coroutines - running these in parallel silently corrupts
        # session state instead of raising a clean error. This is still a
        # bounded operation (bounded to this request's own source list, and
        # reusing rate_policy's de-dup so identical concurrent HTTP calls to
        # this endpoint share one in-flight search per source) - it just
        # isn't internally parallel.
        for source in to_search:
            cache_key = rate_policy.make_search_cache_key(
                "multi_source_search", source_id=source.id, q=query, **{k: v for k, v in params.items() if v is not None}
            )
            try:
                result = await rate_policy.run_bounded_search(
                    cache_key, lambda source=source: self.channel_search_by_source(source.id, query, **params)
                )
            except TelegramSearchError as exc:
                outcome.skipped.append(SkippedSource(source_id=source.id, status=source.access_status, reason=f"{exc.code}: {exc.message}"))
            else:
                outcome.items.extend(result.items)
                outcome.searched_source_ids.append(source.id)

        return outcome

    def _raise_access_denied(self, status: SourceAccessStatus, *, reason: str | None = None) -> None:
        code, http_status, default_message = _ACCESS_DENIAL_CODES.get(
            status, ("ACCESS_DENIED", 403, "This source cannot be searched by the authenticated account.")
        )
        raise TelegramSearchError(code=code, message=reason or default_message, http_status=http_status, retryable=False)

    def _reject_if_bot(self, source_type: SourceType | str | None) -> None:
        """A bot is never a channel-search source - checked before any
        access-status/searchability check and before any Telegram call, so
        a bot never silently returns 200 + [] the way it did before this
        check existed (it was being treated as an empty, accessible User)."""
        value = source_type.value if isinstance(source_type, SourceType) else source_type
        if value == SourceType.BOT.value:
            raise TelegramSearchError(
                code="NOT_A_CHANNEL",
                message=(
                    "This Telegram entity is a bot, not a channel/group - bots have no message-search "
                    "source. Use the bot-start workflow (POST /api/telegram/bots/{id}/start) instead."
                ),
                http_status=422,
                retryable=False,
            )

    # -- channel discovery ---------------------------------------------------- #

    async def discover_channels(self, query: str, **params) -> channel_discovery.ChannelDiscoveryOutcome:
        await self._require_session()
        started = time.monotonic()
        cache_key = rate_policy.make_search_cache_key("channel_discovery", q=query, **params)

        try:
            outcome = await rate_policy.run_bounded_search(
                cache_key, lambda: channel_discovery.discover_channels(self.client_manager.client, query, **params)
            )
        except TelegramSearchError:
            await self._audit_search(audit_events.CHANNEL_DISCOVERY, query, success=False)
            raise
        except Exception as exc:  # noqa: BLE001
            translated = translate_telegram_error(exc)
            await self._audit_search(audit_events.CHANNEL_DISCOVERY, query, success=False, details={"error": translated.code})
            raise translated from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        await self._audit_search(audit_events.CHANNEL_DISCOVERY, query, success=True, details={"result_count": len(outcome.items)})
        log_structured(
            logger, "search_completed", operation="channel_discovery",
            query_length=len(query), result_count=len(outcome.items), duration_ms=duration_ms,
        )
        return outcome

    # -- avatars ---------------------------------------------------------------- #

    async def get_channel_photo(self, identifier: str) -> bytes | None:
        """Real, full-resolution profile photo for `identifier` - the
        on-demand counterpart to the free `photo_thumb_data_uri` stripped
        thumbnail already included in every discover_channels/search result
        (see app/telegram/avatars.py). Caches to disk keyed by the resolved
        entity's own Telegram id, so a repeat request - even under a
        different identifier spelling (username vs numeric id) for the same
        entity - costs no further Telegram RPC. Returns None when the
        entity genuinely has no photo (never cached, so a channel that adds
        a photo later starts resolving it on the next request).
        """
        await self._require_session()
        normalized = normalize_identifier(identifier)
        try:
            entity = await access_manager.resolve_entity(self.client_manager.client, normalized)
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc

        cache_path = self._avatar_cache_path(entity.id)
        if cache_path.exists():
            return cache_path.read_bytes()

        try:
            photo = await avatars.download_avatar(self.client_manager.client, entity)
        except avatars.AvatarDownloadError as exc:
            if exc.session_invalid:
                self.client_manager.mark_session_invalid(exc.reason)
                raise TelegramSearchError(
                    code="TELEGRAM_SESSION_INVALID", message=exc.reason, http_status=401, retryable=False,
                ) from exc
            raise TelegramSearchError(
                code="AVATAR_DOWNLOAD_FAILED", message=exc.reason, http_status=502, retryable=True,
            ) from exc

        if photo is None:
            return None

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(photo)
        return photo

    def _avatar_cache_path(self, entity_id: int) -> Path:
        return Path(self.settings.avatar_cache_path) / f"{entity_id}.jpg"

    # -- save (promote + persist) ---------------------------------------------- #

    async def save_search_result(
        self,
        identifier: str,
        telegram_message_id: int,
        *,
        collection_method: str = CollectionMethod.MANUAL_COLLECTION.value,
        monitoring_enabled: bool = False,
    ) -> tuple[TelegramSource, TelegramMessage | None, bool]:
        """Promotes one search result into a registered source and persists
        the exact message it pointed at, by re-fetching it live from
        Telegram (the client only resubmits the source identifier + message
        id - see SaveSearchResultRequest - never the full message body) and
        writing it through the same evidence-preserving path monitoring/
        backfill use (MessageService._persist_one), so a saved result is
        indistinguishable from any other collected message other than its
        `collection_method`.
        """
        source_service = SourceService(self.session, self.client_manager)
        try:
            source = await source_service.register_source(identifier, monitoring_enabled=monitoring_enabled)
        except SourceAlreadyExistsError as exc:
            # The duplicate may be registered under a different identifier
            # spelling (e.g. numeric id vs @username), so trust the id the
            # error carries and only fall back to an identifier lookup.
            source = None
            if exc.existing_source_id is not None:
                source = await self.sources.get(exc.existing_source_id)
            if source is None:
                normalized_id = normalize_identifier(identifier)
                source = await self.sources.get_by_identifier(normalized_id.normalized)
            if source is None:
                raise TelegramSearchError(
                    code="NOT_FOUND", message="source already exists but could not be located", http_status=409
                ) from None

        status = SourceAccessStatus(source.access_status)
        if status not in SEARCHABLE_STATUSES:
            self._raise_access_denied(status)

        await self._require_session()
        normalized = normalize_identifier(source.identifier)
        try:
            entity = await access_manager.resolve_entity(self.client_manager.client, normalized)
            message = await self.client_manager.client.get_messages(entity, ids=telegram_message_id)
        except Exception as exc:  # noqa: BLE001
            translated = translate_telegram_error(exc)
            await self._audit_save(source.id, telegram_message_id, success=False, details={"error": translated.code})
            raise translated from exc

        if message is None:
            await self._audit_save(source.id, telegram_message_id, success=False, details={"error": "NOT_FOUND"})
            raise TelegramSearchError(
                code="NOT_FOUND", message=f"message {telegram_message_id} was not found on this source", http_status=404
            )

        normalized_message = await collector.normalize_message(message, source_username=source.username)
        message_service = MessageService(self.session, self.settings)
        inserted = await message_service.persist_single_message(source.id, normalized_message, collection_method)
        message_row = await message_service.messages.get_by_source_and_telegram_id(source.id, telegram_message_id)

        await self._audit_save(source.id, telegram_message_id, success=True, details={"inserted": inserted, "collection_method": collection_method})
        return source, message_row, inserted

    async def _audit_save(self, source_id: int, telegram_message_id: int, *, success: bool, details: dict) -> None:
        await self.audit.log(
            audit_events.SEARCH_RESULT_SAVED,
            source_id=source_id,
            success=success,
            details={"telegram_message_id": telegram_message_id, **details},
        )
        await self.session.commit()

    # -- audit ----------------------------------------------------------------- #

    async def _audit_search(self, event_type: str, query: str, *, source_id: int | None = None, success: bool = True, details: dict | None = None) -> None:
        payload = {"query_length": len(query), **(details or {})}
        await self.audit.log(event_type, source_id=source_id, success=success, details=payload)
        await self.session.commit()
