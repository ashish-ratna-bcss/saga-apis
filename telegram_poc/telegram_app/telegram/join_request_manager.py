"""Submitting and polling legitimate Telegram join requests.

Hard rules enforced here, per the project's non-negotiable requirements:
  - Exactly one join request is ever submitted per source while a PENDING
    request already exists (enforced by the caller checking
    AccessRequestRepository.get_active_for_source before calling submit()).
  - No alternative access mechanism is attempted after a rejection.
  - Approval is only ever reported after Telegram itself confirms membership
    (never inferred from the mere act of submitting the request).

Known Telegram-side limitation (documented in README, not worked around
here): a public join-request-enabled channel does not expose an explicit
"your request was declined" signal to the requesting account through
ordinary polling. This module can positively detect PENDING -> APPROVED
(membership becomes visible) and, for invite-hash based requests, can infer
REJECTED/EXPIRED from the invite itself becoming invalid. For username-based
join requests that are silently declined, the request legitimately stays
PENDING - this is an accurate reflection of what the API exposes, not a bug.
"""
from __future__ import annotations

from dataclasses import dataclass

from telethon.errors import (
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    RPCError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInvite, ChatInviteAlready

from telegram_app.database.models import SourceAccessStatus as S
from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE


@dataclass
class JoinAttemptResult:
    status: S  # JOINED (instant) or JOIN_REQUEST_PENDING, or ERROR
    reason: str | None = None
    session_invalid: bool = False


@dataclass
class PendingCheckResult:
    status: S  # JOIN_REQUEST_PENDING, JOIN_REQUEST_APPROVED, JOIN_REQUEST_REJECTED, or ERROR
    reason: str | None = None
    telegram_entity_id: str | None = None
    session_invalid: bool = False


async def submit_join_request(client, *, username: str | None, invite_hash: str | None) -> JoinAttemptResult:
    """Submits exactly one join attempt. Callers must have already verified
    no PENDING request exists for this source."""
    try:
        if invite_hash:
            await client(ImportChatInviteRequest(invite_hash))
            return JoinAttemptResult(status=S.JOINED, reason="joined immediately via invite link")
        if username:
            await client(JoinChannelRequest(username))
            return JoinAttemptResult(status=S.JOINED, reason="joined immediately")
        return JoinAttemptResult(status=S.ERROR, reason="no username or invite hash available to join with")
    except SESSION_INVALID_ERRORS as exc:
        return JoinAttemptResult(
            status=S.ERROR, reason=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True
        )
    except InviteRequestSentError:
        return JoinAttemptResult(status=S.JOIN_REQUEST_PENDING, reason="join request sent - awaiting admin approval")
    except UserAlreadyParticipantError:
        return JoinAttemptResult(status=S.JOINED, reason="already a participant")
    except (InviteHashInvalidError, InviteHashExpiredError) as exc:
        return JoinAttemptResult(status=S.ERROR, reason=exc.__class__.__name__)
    except FloodWaitError as exc:
        return JoinAttemptResult(status=S.ERROR, reason=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return JoinAttemptResult(status=S.ERROR, reason=f"{exc.__class__.__name__}: {exc}")


async def check_pending_status(client, *, username: str | None, invite_hash: str | None) -> PendingCheckResult:
    """Re-checks Telegram for whether a previously-submitted request has resolved."""
    if invite_hash:
        return await _check_invite_pending(client, invite_hash)
    if username:
        return await _check_username_pending(client, username)
    return PendingCheckResult(status=S.ERROR, reason="no username or invite hash recorded for this request")


async def _check_invite_pending(client, invite_hash: str) -> PendingCheckResult:
    try:
        invite = await client(CheckChatInviteRequest(invite_hash))
    except SESSION_INVALID_ERRORS as exc:
        return PendingCheckResult(
            status=S.ERROR, reason=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True
        )
    except InviteHashExpiredError:
        return PendingCheckResult(status=S.JOIN_REQUEST_REJECTED, reason="invite link expired while request was pending")
    except InviteHashInvalidError:
        return PendingCheckResult(status=S.JOIN_REQUEST_REJECTED, reason="invite no longer valid (declined or revoked)")
    except FloodWaitError as exc:
        return PendingCheckResult(status=S.ERROR, reason=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return PendingCheckResult(status=S.ERROR, reason=f"{exc.__class__.__name__}: {exc}")

    if isinstance(invite, ChatInviteAlready):
        return PendingCheckResult(status=S.JOIN_REQUEST_APPROVED, telegram_entity_id=str(invite.chat.id))
    if isinstance(invite, ChatInvite):
        return PendingCheckResult(status=S.JOIN_REQUEST_PENDING, reason="still awaiting admin approval")
    return PendingCheckResult(status=S.ERROR, reason="unrecognized invite response from Telegram")


async def _check_username_pending(client, username: str) -> PendingCheckResult:
    try:
        entity = await client.get_entity(username)
    except SESSION_INVALID_ERRORS as exc:
        return PendingCheckResult(
            status=S.ERROR, reason=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True
        )
    except FloodWaitError as exc:
        return PendingCheckResult(status=S.ERROR, reason=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return PendingCheckResult(status=S.ERROR, reason=f"{exc.__class__.__name__}: {exc}")

    if getattr(entity, "left", None) is False:
        return PendingCheckResult(status=S.JOIN_REQUEST_APPROVED, telegram_entity_id=str(entity.id))
    return PendingCheckResult(
        status=S.JOIN_REQUEST_PENDING,
        reason="still pending - Telegram does not expose an explicit decline signal for this request type",
    )
