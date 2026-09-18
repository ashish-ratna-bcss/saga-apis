"""Source registration and access-status management.

This is the orchestration layer between the API and the Telegram access
layer: routes call this, this calls app/telegram/* and the repositories.
Telethon is never imported here directly - only the already-abstracted
access_manager / join_request_manager / discovery modules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.database.models import AccessRequestStatus, SourceAccessStatus, SourceType, TelegramSource
from telegram_app.database.repositories.access_request_repository import AccessRequestRepository
from telegram_app.database.repositories.entity_repository import EntityRepository
from telegram_app.database.repositories.source_repository import SourceRepository
from telegram_app.services import audit_service as audit_events
from telegram_app.services import notification_service as notif_events
from telegram_app.services.audit_service import AuditService
from telegram_app.services.notification_service import NotificationService
from telegram_app.telegram import access_manager, join_request_manager
from telegram_app.telegram.client import TelegramClientManager, TelegramNotConfiguredError
from telegram_app.telegram.discovery import IdentifierKind, extract_telegram_urls, normalize_identifier

logger = logging.getLogger("telegram_service.services.source")


class SourceAlreadyExistsError(Exception):
    """Raised when a registration would duplicate a source that already exists.

    Carries `existing_source_id` because the duplicate is not always findable
    by identifier: the same channel can be registered as `@name` and again as
    its numeric id, which normalize to different dedup keys but resolve to one
    `telegram_entity_id`. Callers must use this id rather than re-querying by
    identifier, which would come up empty for that case.
    """

    def __init__(self, message: str, *, existing_source_id: int | None = None) -> None:
        super().__init__(message)
        self.existing_source_id = existing_source_id


class SourceNotFoundError(Exception):
    pass


class InvalidAccessRequestStateError(Exception):
    pass


@dataclass
class LinkedChannelCandidate:
    """One Telegram link found in a text blob (e.g. a collected message
    body), with its live access state - a read-only preview, never a
    registered source. Discovery is not monitoring: turning a candidate into
    a tracked source is always a separate, explicit `POST /api/sources` call
    the operator makes themselves (see SourceService.discover_linked_channels)."""

    raw_url: str
    identifier: str | None
    access_status: str | None
    title: str | None
    username: str | None
    already_registered_source_id: int | None
    reason: str | None


@dataclass
class JoinByInviteOutcome:
    source: TelegramSource
    status: str  # "joined" | "already_member" | "pending_approval" | "already_pending" | "failed"
    reason: str | None


class SourceService:
    def __init__(self, session: AsyncSession, client_manager: TelegramClientManager) -> None:
        self.session = session
        self.client_manager = client_manager
        self.sources = SourceRepository(session)
        self.access_requests = AccessRequestRepository(session)
        self.entities = EntityRepository(session)
        self.audit = AuditService(session)
        self.notifications = NotificationService(session)

    # -- registration ---------------------------------------------------- #

    async def register_source(
        self,
        identifier: str,
        monitoring_enabled: bool = True,
        *,
        discovered_from_source_id: int | None = None,
        discovery_type: str | None = None,
    ) -> TelegramSource:
        """`discovered_from_source_id`/`discovery_type` are optional
        provenance fields for sources surfaced by an automated discovery
        path (currently only BotService's bot-response link scanning) - both
        default to None for every other caller (manual `POST /api/sources`,
        search-result promotion, ...), which is unchanged behavior."""
        normalized = normalize_identifier(identifier)
        existing = await self.sources.get_by_identifier(normalized.normalized)
        if existing is not None:
            raise SourceAlreadyExistsError(
                f"source already registered as id={existing.id}", existing_source_id=existing.id
            )

        source = await self.sources.create(
            identifier=normalized.normalized,
            access_status=SourceAccessStatus.DISCOVERED.value,
            monitoring_enabled=monitoring_enabled,
            discovered_from_source_id=discovered_from_source_id,
            discovery_type=discovery_type,
        )
        await self.audit.log(
            audit_events.SOURCE_REGISTERED, source_id=source.id, details={"identifier": normalized.normalized}
        )
        await self.session.commit()

        # Best-effort immediate resolution so the caller sees a real status
        # right away; if Telegram isn't reachable yet this just leaves the
        # source at DISCOVERED for a later explicit check-access call.
        try:
            await self.check_access(source.id)
        except TelegramNotConfiguredError:
            logger.info("telegram not configured yet - source %s left at DISCOVERED", source.id)

        # Identifier-level dedup above cannot catch the same channel arriving
        # under two spellings that normalize differently - "@name" vs its
        # numeric id, which is exactly what channel discovery hands back. Only
        # resolution reveals they are one channel, so the second dedup pass
        # has to happen here, once check_access has filled in
        # telegram_entity_id. Without it the channel gets two source rows and
        # is collected, monitored and rate-limited against twice.
        await self._reject_if_duplicate_entity(source)

        return await self.get_source(source.id)  # type: ignore[return-value]

    async def _reject_if_duplicate_entity(self, source: TelegramSource) -> None:
        """Undoes `source` and raises if resolution proved it is a channel some
        other source already tracks. Unresolved sources (Telegram unreachable,
        invite hash not yet accepted) are left alone - there is nothing to
        compare yet, and the reconciliation job re-checks them later."""
        await self.session.refresh(source)
        if not source.telegram_entity_id:
            return

        duplicate_of = await self.sources.get_first_other_by_entity_id(
            source.telegram_entity_id, excluding_source_id=source.id
        )
        if duplicate_of is None:
            return

        # Keep the audit trail, but detach it from the row about to disappear:
        # SQLite is not running with foreign_keys=ON, so its ON DELETE SET NULL
        # would not fire and these rows would keep a dangling source_id.
        await self.audit.detach_source(source.id)
        await self.sources.delete(source)
        await self.audit.log(
            audit_events.SOURCE_REGISTERED,
            source_id=duplicate_of.id,
            success=False,
            details={
                "rejected_identifier": source.identifier,
                "reason": "already registered under a different identifier",
                "telegram_entity_id": duplicate_of.telegram_entity_id,
            },
        )
        await self.session.commit()
        raise SourceAlreadyExistsError(
            f"source already registered as id={duplicate_of.id} "
            f"(identifier {duplicate_of.identifier!r}, same Telegram entity {duplicate_of.telegram_entity_id})",
            existing_source_id=duplicate_of.id,
        )

    async def get_source(self, source_id: int) -> TelegramSource | None:
        return await self.sources.get(source_id)

    async def list_sources(self, limit: int = 100, offset: int = 0) -> list[TelegramSource]:
        return await self.sources.list_all(limit=limit, offset=offset)

    async def update_source(self, source_id: int, **fields) -> TelegramSource:
        source = await self._require_source(source_id)
        clean = {k: v for k, v in fields.items() if v is not None}
        return await self.sources.update(source, **clean)

    async def delete_source(self, source_id: int) -> None:
        source = await self._require_source(source_id)
        await self.sources.delete(source)
        await self.session.commit()

    # -- access checking --------------------------------------------------- #

    async def check_access(self, source_id: int) -> TelegramSource:
        source = await self._require_source(source_id)
        # Verify the session BEFORE attempting resolution (ResolveUsernameRequest)
        # - a known-bad/unauthorized session must never reach Telegram and get
        # misclassified as NOT_FOUND/ACCESS_DENIED.
        ok, err = await self.client_manager.verify_authorized()
        now = datetime.now(timezone.utc)
        if not ok:
            await self._apply_status(source, SourceAccessStatus.ERROR, reason=err)
            await self.sources.update(
                source,
                last_probe_status=SourceAccessStatus.ERROR.value,
                last_probe_reason=err,
                last_probe_at=now,
            )
            # Distinct from a generic ACCESS_CHECKED failure when the specific
            # cause is a known-invalid session, not just a transient network hiccup.
            event = (
                audit_events.TELEGRAM_ACCOUNT_UNAUTHORIZED
                if self.client_manager.session_invalid
                else audit_events.ACCESS_CHECKED
            )
            await self.audit.log(event, source_id=source.id, success=False, details={"error": err})
            await self.session.commit()
            return source

        normalized = normalize_identifier(source.identifier)
        result = await access_manager.check_access(self.client_manager.client, normalized)
        if result.session_invalid:
            # Discovered mid-call (e.g. a cached-authorized session that was
            # revoked since) rather than by the pre-flight check above -
            # latch it now so every subsequent operation short-circuits too.
            self.client_manager.mark_session_invalid(result.reason or "session invalidated during access check")

        was_unresolved = source.telegram_entity_id is None
        # Recorded unconditionally, every check - independent of whether the
        # state machine below actually lets this probe result move the
        # OPERATIONAL access_status. A live probe reporting PUBLIC_ACCESSIBLE
        # while the source is operationally MONITORING must never look like
        # "nothing happened": it's visible here even though _apply_status
        # (below) will correctly refuse to downgrade MONITORING for it.
        update_fields: dict = {
            "last_status_check_at": now,
            "last_probe_status": result.status.value,
            "last_probe_reason": result.reason,
            "last_probe_at": now,
        }
        if result.telegram_entity_id:
            update_fields["telegram_entity_id"] = result.telegram_entity_id
        if result.username:
            update_fields["username"] = result.username
        if result.title:
            update_fields["title"] = result.title
        if result.source_type:
            update_fields["source_type"] = result.source_type.value
        await self.sources.update(source, **update_fields)

        await self._apply_status(source, result.status, reason=result.reason)
        await self.audit.log(
            audit_events.ACCESS_CHECKED,
            source_id=source.id,
            details={"status": result.status.value, "reason": result.reason},
        )
        if was_unresolved and result.telegram_entity_id:
            await self.audit.log(audit_events.SOURCE_RESOLVED, source_id=source.id, details={"telegram_entity_id": result.telegram_entity_id})
            if result.source_type == SourceType.BOT:
                await self.audit.log(
                    audit_events.TELEGRAM_BOT_DETECTED, source_id=source.id, details={"telegram_entity_id": result.telegram_entity_id}
                )
        await self.session.commit()
        return source

    async def _apply_status(self, source: TelegramSource, new_status: SourceAccessStatus, *, reason: str | None) -> None:
        current = SourceAccessStatus(source.access_status)
        # Hard invariant: access_status must never be a not-positively-verified
        # status (ERROR, ACCESS_DENIED, JOIN_REQUEST_REQUIRED/PENDING/REJECTED,
        # NOT_FOUND, DISABLED) while monitoring_enabled stays True. This is the
        # single choke point every access-status write goes through, so it is
        # enforced here regardless of which caller/path triggered the change -
        # but only when access_status is actually being set (or confirmed) to
        # that status; a *blocked* transition leaves access_status (and thus
        # monitoring_enabled) untouched below.
        def _monitoring_override() -> dict:
            return {"monitoring_enabled": False} if new_status in access_manager.STATUSES_REQUIRING_MONITORING_DISABLED else {}

        if current == new_status:
            await self.sources.update(source, status_reason=reason, **_monitoring_override())
            return
        try:
            access_manager.transition(current, new_status)
        except access_manager.InvalidStateTransition:
            logger.warning("blocked invalid transition %s -> %s for source %s", current, new_status, source.id)
            await self.sources.update(source, status_reason=f"blocked invalid transition to {new_status.value}: {reason or ''}".strip())
            return
        await self.sources.update(source, access_status=new_status.value, status_reason=reason, **_monitoring_override())

    # -- linked-channel discovery (read-only, never auto-registers) -------- #

    # Hard cap on how many links from one text blob are ever resolved against
    # Telegram in a single call - bounded, controlled discovery, never an
    # open-ended crawl of an arbitrarily large input.
    MAX_LINKS_PER_EXTRACTION = 20

    async def discover_linked_channels(self, text: str) -> list[LinkedChannelCandidate]:
        """Extracts Telegram links from `text` (e.g. one collected message's
        body) and reports each one's live access state. Read-only: never
        creates a source row, never submits a join request, never enables
        monitoring. Discovery != automatic monitoring - turning a candidate
        into a tracked source is always a separate, explicit `register_source`
        call the operator makes themselves after reviewing this list. Non-
        Telegram URLs found in the text are ignored entirely (never fetched -
        see app/telegram/discovery/url_classifier.py)."""
        found = [f for f in extract_telegram_urls(text) if f.is_telegram][: self.MAX_LINKS_PER_EXTRACTION]
        candidates: list[LinkedChannelCandidate] = []

        for classification in found:
            if classification.identifier is None:
                candidates.append(
                    LinkedChannelCandidate(
                        raw_url=classification.raw_url, identifier=None, access_status=None,
                        title=None, username=None, already_registered_source_id=None,
                        reason=classification.reason,
                    )
                )
                continue

            existing = await self.sources.get_by_identifier(classification.identifier.normalized)
            ok, err = await self.client_manager.verify_authorized()
            if not ok:
                candidates.append(
                    LinkedChannelCandidate(
                        raw_url=classification.raw_url, identifier=classification.identifier.normalized,
                        access_status=None, title=None, username=None,
                        already_registered_source_id=existing.id if existing else None, reason=err,
                    )
                )
                continue

            result = await access_manager.check_access(self.client_manager.client, classification.identifier)
            await self.audit.log(
                audit_events.TELEGRAM_URL_CLASSIFIED,
                details={
                    "raw_url": classification.raw_url,
                    "identifier": classification.identifier.normalized,
                    "access_status": result.status.value,
                },
            )
            candidates.append(
                LinkedChannelCandidate(
                    raw_url=classification.raw_url, identifier=classification.identifier.normalized,
                    access_status=result.status.value, title=result.title, username=result.username,
                    already_registered_source_id=existing.id if existing else None, reason=result.reason,
                )
            )

        await self.session.commit()
        return candidates

    # -- join-request workflow --------------------------------------------- #

    async def request_access(self, source_id: int):
        source = await self._require_source(source_id)
        current = SourceAccessStatus(source.access_status)

        existing_pending = await self.access_requests.get_active_for_source(source.id)
        if existing_pending is not None:
            # Do NOT submit a duplicate request - this is a hard requirement.
            return existing_pending

        if current not in (SourceAccessStatus.JOIN_REQUEST_REQUIRED, SourceAccessStatus.ERROR):
            raise InvalidAccessRequestStateError(
                f"source {source.id} is in state {current.value}; access can only be requested from JOIN_REQUEST_REQUIRED"
            )

        ok, err = await self.client_manager.verify_authorized()
        if not ok:
            await self._apply_status(source, SourceAccessStatus.ERROR, reason=err)
            await self.session.commit()
            raise RuntimeError(f"cannot request access: {err}")

        normalized = normalize_identifier(source.identifier)
        result = await join_request_manager.submit_join_request(
            self.client_manager.client,
            username=normalized.value if normalized.kind == IdentifierKind.USERNAME else source.username,
            invite_hash=normalized.value if normalized.kind == IdentifierKind.INVITE_HASH else None,
        )
        if result.session_invalid:
            self.client_manager.mark_session_invalid(result.reason or "session invalidated during join request")

        now = datetime.now(timezone.utc)
        request = await self.access_requests.create(
            source_id=source.id,
            status=(
                AccessRequestStatus.APPROVED.value
                if result.status == SourceAccessStatus.JOINED
                else AccessRequestStatus.PENDING.value
                if result.status == SourceAccessStatus.JOIN_REQUEST_PENDING
                else AccessRequestStatus.ERROR.value
            ),
            requested_at=now,
            last_checked_at=now,
            next_check_at=now + timedelta(hours=12),
            attempt_count=1,
            approved_at=now if result.status == SourceAccessStatus.JOINED else None,
            error_message=result.reason if result.status == SourceAccessStatus.ERROR else None,
        )

        await self.audit.log(
            audit_events.JOIN_REQUEST_SUBMITTED,
            source_id=source.id,
            success=result.status != SourceAccessStatus.ERROR,
            details={"outcome": result.status.value, "reason": result.reason},
        )

        if result.status == SourceAccessStatus.ERROR:
            await self._apply_status(source, SourceAccessStatus.ERROR, reason=result.reason)
        elif result.status == SourceAccessStatus.JOINED:
            await self._apply_status(source, SourceAccessStatus.JOINED, reason=result.reason)
            await self.notifications.create(
                notif_events.EVENT_ACCESS_GRANTED,
                source=source,
                previous_status=SourceAccessStatus.JOIN_REQUEST_REQUIRED.value,
                new_status=SourceAccessStatus.JOINED.value,
            )
        else:
            await self._apply_status(source, SourceAccessStatus.JOIN_REQUEST_PENDING, reason=result.reason)

        await self.session.commit()
        return request

    # -- explicit invite-link join (POST /api/telegram/invites/join) ------- #

    async def join_by_invite(self, identifier: str) -> JoinByInviteOutcome:
        """Explicit, operator-initiated join by a raw Telegram invite link
        (t.me/+hash or t.me/joinchat/hash) - the API equivalent of a human
        tapping an invite link. Deliberately built entirely on top of the
        existing register_source (dedup + access probe) and request_access
        (idempotent join submission, no-duplicate-request guarantee, full
        FloodWait/invalid/expired/already-member handling) - no separate
        join or access-state logic lives here, and no new state is
        introduced beyond what SourceAccessStatus already has."""
        normalized = normalize_identifier(identifier)
        if normalized.kind != IdentifierKind.INVITE_HASH:
            raise ValueError(
                f"'{identifier}' is not a Telegram invite link (t.me/+hash or t.me/joinchat/hash) - "
                "use POST /api/sources for a channel/group username instead"
            )

        try:
            source = await self.register_source(identifier, monitoring_enabled=False)
        except SourceAlreadyExistsError as exc:
            # An invite link can resolve to a channel already registered under
            # its @username, in which case no row carries this invite's
            # identifier - so the id on the error is the only reliable handle.
            existing = None
            if exc.existing_source_id is not None:
                existing = await self.sources.get(exc.existing_source_id)
            if existing is None:
                existing = await self.sources.get_by_identifier(normalized.normalized)
            if existing is None:
                raise
            source = await self.check_access(existing.id)

        status = SourceAccessStatus(source.access_status)

        already_accessible = (
            SourceAccessStatus.ACCESSIBLE,
            SourceAccessStatus.JOINED,
            SourceAccessStatus.PUBLIC_ACCESSIBLE,
            SourceAccessStatus.MONITORING,
        )
        if status in already_accessible:
            await self.audit.log(audit_events.TELEGRAM_JOIN_ALREADY_MEMBER, source_id=source.id, details={"identifier": identifier})
            await self.session.commit()
            return JoinByInviteOutcome(source=source, status="already_member", reason=source.status_reason)

        if status == SourceAccessStatus.JOIN_REQUEST_PENDING:
            await self.audit.log(audit_events.TELEGRAM_JOIN_REQUEST_PENDING, source_id=source.id, details={"identifier": identifier})
            await self.session.commit()
            return JoinByInviteOutcome(source=source, status="already_pending", reason=source.status_reason)

        if status not in (SourceAccessStatus.JOIN_REQUEST_REQUIRED, SourceAccessStatus.ERROR):
            await self.audit.log(
                audit_events.TELEGRAM_JOIN_FAILED, source_id=source.id, success=False,
                details={"identifier": identifier, "status": status.value},
            )
            await self.session.commit()
            return JoinByInviteOutcome(source=source, status="failed", reason=f"source is in state {status.value}; cannot join from here")

        await self.audit.log(audit_events.TELEGRAM_JOIN_REQUESTED, source_id=source.id, details={"identifier": identifier})
        await self.session.commit()

        try:
            await self.request_access(source.id)
        except RuntimeError as exc:
            await self.audit.log(audit_events.TELEGRAM_JOIN_FAILED, source_id=source.id, success=False, details={"error": str(exc)})
            await self.session.commit()
            return JoinByInviteOutcome(source=await self.get_source(source.id), status="failed", reason=str(exc))

        refreshed = await self.get_source(source.id)
        final_status = SourceAccessStatus(refreshed.access_status)

        if final_status in (SourceAccessStatus.JOINED, SourceAccessStatus.ACCESSIBLE, SourceAccessStatus.MONITORING):
            await self.audit.log(audit_events.TELEGRAM_JOIN_COMPLETED, source_id=source.id, details={"identifier": identifier})
            result_status = "joined"
        elif final_status == SourceAccessStatus.JOIN_REQUEST_PENDING:
            await self.audit.log(audit_events.TELEGRAM_JOIN_REQUEST_PENDING, source_id=source.id, details={"identifier": identifier})
            result_status = "pending_approval"
        else:
            await self.audit.log(
                audit_events.TELEGRAM_JOIN_FAILED, source_id=source.id, success=False,
                details={"identifier": identifier, "status": final_status.value},
            )
            result_status = "failed"

        await self.session.commit()
        return JoinByInviteOutcome(source=refreshed, status=result_status, reason=refreshed.status_reason)

    async def get_access_status(self, source_id: int) -> TelegramSource:
        return await self._require_source(source_id)

    async def _require_source(self, source_id: int) -> TelegramSource:
        source = await self.sources.get(source_id)
        if source is None:
            raise SourceNotFoundError(f"source {source_id} not found")
        return source
