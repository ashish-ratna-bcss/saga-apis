"""The source-access state machine, and the logic that determines a source's
real, current access status by asking Telegram - never by inference.

Two responsibilities live here on purpose:
  1. A pure, dependency-free state machine (`transition`) so state changes
     everywhere in the app go through one validated choke point.
  2. `check_access`, which probes Telegram (a single lightweight read call,
     or CheckChatInviteRequest for invite links) to tell DISCOVERED apart
     from actually-accessible, exactly per the mandatory distinction in the
     project requirements.
"""
from __future__ import annotations

from dataclasses import dataclass

from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, User

from telegram_app.database.models import SourceAccessStatus, SourceType
from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE
from telegram_app.telegram.discovery import IdentifierKind, NormalizedIdentifier

logger = __import__("logging").getLogger("telegram_service.telegram.access_manager")

# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

S = SourceAccessStatus

ALLOWED_TRANSITIONS: dict[SourceAccessStatus, set[SourceAccessStatus]] = {
    S.DISCOVERED: {S.PUBLIC_ACCESSIBLE, S.ACCESSIBLE, S.JOIN_REQUEST_REQUIRED, S.ACCESS_DENIED, S.NOT_FOUND, S.ERROR},
    S.PUBLIC_ACCESSIBLE: {S.MONITORING, S.ERROR, S.DISABLED, S.PUBLIC_ACCESSIBLE},
    S.ACCESSIBLE: {S.MONITORING, S.ERROR, S.DISABLED, S.ACCESSIBLE},
    S.JOIN_REQUEST_REQUIRED: {S.JOIN_REQUEST_PENDING, S.JOINED, S.ERROR, S.DISABLED},
    S.JOIN_REQUEST_PENDING: {S.JOIN_REQUEST_PENDING, S.JOIN_REQUEST_APPROVED, S.JOIN_REQUEST_REJECTED, S.ERROR},
    S.JOIN_REQUEST_APPROVED: {S.JOINED, S.ERROR},
    S.JOIN_REQUEST_REJECTED: {S.DISABLED, S.ERROR},
    S.JOINED: {S.MONITORING, S.ERROR},
    S.MONITORING: {S.MONITORING, S.ERROR, S.DISABLED},
    S.ACCESS_DENIED: {S.DISABLED, S.ERROR},
    S.NOT_FOUND: {S.DISABLED, S.ERROR, S.DISCOVERED},
    # ERROR is recoverable: an explicit re-check (POST /check-access) may retry
    # discovery from scratch, so ERROR can reach anything DISCOVERED can.
    S.ERROR: {S.DISCOVERED, S.PUBLIC_ACCESSIBLE, S.ACCESSIBLE, S.JOIN_REQUEST_REQUIRED, S.ACCESS_DENIED, S.NOT_FOUND, S.ERROR, S.DISABLED},
    S.DISABLED: {S.DISCOVERED},
}

# Hard invariant (never access_status in this set + monitoring_enabled=True):
# these are all the "not positively verified / blocked" states - ERROR
# (including the TELEGRAM_SESSION_INVALID case), ACCESS_DENIED, NOT_FOUND,
# JOIN_REQUEST_REQUIRED ("private, needs join"), JOIN_REQUEST_PENDING,
# JOIN_REQUEST_REJECTED, and DISABLED. Monitoring may only be true once a
# source is (or returns to) MONITORING via the explicit start_monitoring
# flow. Enforced centrally in SourceService._apply_status.
STATUSES_REQUIRING_MONITORING_DISABLED: set[SourceAccessStatus] = {
    S.ERROR,
    S.ACCESS_DENIED,
    S.NOT_FOUND,
    S.JOIN_REQUEST_REQUIRED,
    S.JOIN_REQUEST_PENDING,
    S.JOIN_REQUEST_REJECTED,
    S.DISABLED,
}


class InvalidStateTransition(Exception):
    def __init__(self, current: SourceAccessStatus, new: SourceAccessStatus) -> None:
        super().__init__(f"cannot transition source access status from {current} to {new}")
        self.current = current
        self.new = new


def transition(current: SourceAccessStatus | str, new: SourceAccessStatus | str) -> SourceAccessStatus:
    """Validates and returns the new status, or raises InvalidStateTransition."""
    current_s = S(current)
    new_s = S(new)
    if new_s not in ALLOWED_TRANSITIONS.get(current_s, set()):
        raise InvalidStateTransition(current_s, new_s)
    return new_s


# --------------------------------------------------------------------------- #
# Access probing
# --------------------------------------------------------------------------- #


@dataclass
class AccessCheckResult:
    status: SourceAccessStatus
    telegram_entity_id: str | None = None
    username: str | None = None
    title: str | None = None
    source_type: SourceType = SourceType.UNKNOWN
    reason: str | None = None
    invite_hash: str | None = None
    resolved_entity: object | None = None  # the raw Telethon entity, if any - callers may reuse it
    # True only when the failure means the Telegram *session* is no longer
    # valid (AuthKeyUnregisteredError and siblings) - never for a per-channel
    # outcome like private/not-found. Callers must not treat this the same
    # as a normal ERROR: it means stop and require re-authentication, not
    # "channel not found" / "private" / "access denied".
    session_invalid: bool = False


def classify_entity_type(entity) -> SourceType:
    if isinstance(entity, Channel):
        return SourceType.CHANNEL if getattr(entity, "broadcast", False) else SourceType.SUPERGROUP
    if isinstance(entity, Chat):
        return SourceType.GROUP
    if isinstance(entity, User):
        # A bot is a User with bot=True on the real Telegram entity - it must
        # never be classified as (or treated downstream as) a channel/group
        # message source. See app/services/search_service.py's explicit
        # NOT_A_CHANNEL rejection and app/services/bot_service.py for the
        # bot-specific interaction this type routes callers toward instead.
        return SourceType.BOT if getattr(entity, "bot", False) else SourceType.USER
    return SourceType.UNKNOWN


async def check_access(client, normalized: NormalizedIdentifier) -> AccessCheckResult:
    if normalized.kind == IdentifierKind.INVITE_HASH:
        return await _check_invite_hash_access(client, normalized)
    return await _check_username_or_id_access(client, normalized)


async def resolve_entity(client, normalized: NormalizedIdentifier):
    """Resolve a NormalizedIdentifier to a Telegram entity.

    For numeric IDs: converts to ``int`` (Telethon treats strings as
    usernames, so passing ``"123456"`` would search for @123456 instead
    of peer-ID 123456).  If the bare numeric ID fails, retries with the
    ``-100`` "marked channel" prefix (``-100<id>``) because users often
    copy channel IDs without the prefix.

    Raises the same exceptions as ``client.get_entity()`` on final failure.
    """
    if normalized.kind != IdentifierKind.NUMERIC_ID:
        return await client.get_entity(normalized.value)

    numeric = int(normalized.value)
    try:
        return await client.get_entity(numeric)
    except (UsernameNotOccupiedError, UsernameInvalidError,
            ChannelInvalidError, ValueError):
        # Bare positive ID failed — try -100 "marked channel" form before
        # giving up.  Negative IDs already encode the entity type, so no
        # fallback is appropriate for them.
        if numeric > 0:
            marked_id = int(f"-100{normalized.value}")
            logger.debug(
                "bare numeric ID %s not found — retrying as marked channel ID %s",
                numeric, marked_id,
            )
            return await client.get_entity(marked_id)
        raise


async def _check_username_or_id_access(client, normalized: NormalizedIdentifier) -> AccessCheckResult:
    try:
        entity = await resolve_entity(client, normalized)
    except SESSION_INVALID_ERRORS as exc:
        # Not a channel-resolution outcome at all - the Telegram account's
        # session itself is unregistered/revoked. Must never be reported as
        # NOT_FOUND/private/access-denied (see AccessCheckResult.session_invalid).
        return AccessCheckResult(
            status=S.ERROR,
            reason=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})",
            session_invalid=True,
        )
    except (UsernameNotOccupiedError, UsernameInvalidError, ChannelInvalidError, ValueError):
        return AccessCheckResult(status=S.NOT_FOUND, reason="could not resolve identifier - invalid or does not exist")
    except ChannelPrivateError:
        return AccessCheckResult(
            status=S.ACCESS_DENIED,
            reason="private and this account has no visibility into it (no invite available)",
        )
    except FloodWaitError as exc:
        return AccessCheckResult(status=S.ERROR, reason=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return AccessCheckResult(status=S.ERROR, reason=f"{exc.__class__.__name__}: {exc}")

    source_type = classify_entity_type(entity)
    username = getattr(entity, "username", None)
    title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
    telegram_entity_id = str(entity.id)

    if isinstance(entity, User):
        return AccessCheckResult(
            status=S.ACCESSIBLE,
            telegram_entity_id=telegram_entity_id,
            username=username,
            title=title,
            source_type=source_type,
            resolved_entity=entity,
        )

    try:
        await client.get_messages(entity, limit=1)
    except SESSION_INVALID_ERRORS as exc:
        return AccessCheckResult(
            status=S.ERROR,
            telegram_entity_id=telegram_entity_id,
            username=username,
            title=title,
            source_type=source_type,
            reason=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})",
            session_invalid=True,
        )
    except ChannelPrivateError:
        if username:
            return AccessCheckResult(
                status=S.JOIN_REQUEST_REQUIRED,
                telegram_entity_id=telegram_entity_id,
                username=username,
                title=title,
                source_type=source_type,
                reason="membership required to read this source",
                resolved_entity=entity,
            )
        return AccessCheckResult(
            status=S.ACCESS_DENIED,
            telegram_entity_id=telegram_entity_id,
            username=username,
            title=title,
            source_type=source_type,
            reason="private source with no known invite - cannot access without an authorized invitation",
            resolved_entity=entity,
        )
    except ChatAdminRequiredError:
        return AccessCheckResult(
            status=S.ACCESS_DENIED,
            telegram_entity_id=telegram_entity_id,
            username=username,
            title=title,
            source_type=source_type,
            reason="administrator privileges required",
            resolved_entity=entity,
        )
    except FloodWaitError as exc:
        return AccessCheckResult(status=S.ERROR, reason=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return AccessCheckResult(status=S.ERROR, reason=f"{exc.__class__.__name__}: {exc}")

    already_member = getattr(entity, "left", None) is False
    status = S.ACCESSIBLE if already_member else (S.PUBLIC_ACCESSIBLE if username else S.ACCESSIBLE)
    return AccessCheckResult(
        status=status,
        telegram_entity_id=telegram_entity_id,
        username=username,
        title=title,
        source_type=source_type,
        resolved_entity=entity,
    )


async def _check_invite_hash_access(client, normalized: NormalizedIdentifier) -> AccessCheckResult:
    try:
        invite = await client(CheckChatInviteRequest(normalized.value))
    except SESSION_INVALID_ERRORS as exc:
        return AccessCheckResult(
            status=S.ERROR,
            reason=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})",
            invite_hash=normalized.value,
            session_invalid=True,
        )
    except InviteHashInvalidError:
        return AccessCheckResult(status=S.NOT_FOUND, reason="invalid invite link", invite_hash=normalized.value)
    except InviteHashExpiredError:
        return AccessCheckResult(status=S.ACCESS_DENIED, reason="invite link has expired", invite_hash=normalized.value)
    except FloodWaitError as exc:
        return AccessCheckResult(status=S.ERROR, reason=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return AccessCheckResult(status=S.ERROR, reason=f"{exc.__class__.__name__}: {exc}")

    if isinstance(invite, ChatInviteAlready):
        chat = invite.chat
        return AccessCheckResult(
            status=S.ACCESSIBLE,
            telegram_entity_id=str(chat.id),
            username=getattr(chat, "username", None),
            title=getattr(chat, "title", None),
            source_type=classify_entity_type(chat),
            invite_hash=normalized.value,
            resolved_entity=chat,
        )

    if isinstance(invite, ChatInvite):
        return AccessCheckResult(
            status=S.JOIN_REQUEST_REQUIRED,
            title=getattr(invite, "title", None),
            source_type=SourceType.SUPERGROUP if getattr(invite, "megagroup", False) else SourceType.CHANNEL,
            reason="approval required" if getattr(invite, "request_needed", False) else "invite join required",
            invite_hash=normalized.value,
        )

    return AccessCheckResult(status=S.ERROR, reason="unrecognized invite response from Telegram", invite_hash=normalized.value)
