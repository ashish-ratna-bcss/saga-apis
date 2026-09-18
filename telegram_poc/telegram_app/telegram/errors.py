"""Stable, machine-readable error translation for the search/discovery API.

The rest of the codebase (access_manager, join_request_manager, collector)
already handles Telegram errors by mapping them into domain state
(SourceAccessStatus, CollectionOutcome.error, ...) - this module exists
specifically for the new search/discovery/backfill endpoints, which don't
have a source-record state machine to fall back on and need to hand the API
caller a stable `{code, message, retryable}` shape instead of leaking raw
Telethon exception types.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from telethon.errors import (
    AuthKeyError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    PremiumAccountRequiredError,
    RPCError,
    SessionPasswordNeededError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)

from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE


@dataclass
class TelegramSearchError(Exception):
    """Carries everything routes need to render a stable error response."""

    code: str
    message: str
    http_status: int = 502
    retryable: bool = False
    retry_after_seconds: int | None = None

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.code}: {self.message}"

    def to_payload(self) -> dict:
        payload = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        return {"error": payload}


def translate_telegram_error(exc: Exception) -> TelegramSearchError:
    """Maps a raised exception (Telethon or generic) to a stable TelegramSearchError.

    Never includes session data, api_hash, or other credential material -
    only the exception class name and Telegram's own (public) error text.
    """
    if isinstance(exc, SESSION_INVALID_ERRORS):
        # Must be checked before the generic AuthKeyError/RPCError branches
        # below: this means the Telegram *session* is invalid (e.g.
        # AuthKeyUnregisteredError from ResolveUsernameRequest), never a
        # per-channel outcome like "not found" or "private".
        return TelegramSearchError(
            code="TELEGRAM_SESSION_INVALID",
            message=SESSION_INVALID_MESSAGE,
            http_status=401,
            retryable=False,
        )
    if isinstance(exc, FloodWaitError):
        return TelegramSearchError(
            code="FLOOD_WAIT",
            message=f"Telegram is rate-limiting this account - wait {exc.seconds}s before retrying.",
            http_status=429,
            retryable=True,
            retry_after_seconds=exc.seconds,
        )
    if isinstance(exc, ChannelPrivateError):
        return TelegramSearchError(
            code="CHANNEL_PRIVATE",
            message="The Telegram channel is private or inaccessible to the authenticated account.",
            http_status=403,
            retryable=False,
        )
    if isinstance(exc, ChatAdminRequiredError):
        return TelegramSearchError(
            code="CHAT_ADMIN_REQUIRED",
            message="This operation requires administrator privileges the authenticated account does not have.",
            http_status=403,
            retryable=False,
        )
    if isinstance(exc, UserNotParticipantError):
        return TelegramSearchError(
            code="USER_NOT_PARTICIPANT",
            message="The authenticated account is not a participant of this chat.",
            http_status=403,
            retryable=False,
        )
    if isinstance(exc, (UsernameNotOccupiedError, ChannelInvalidError)):
        return TelegramSearchError(
            code="NOT_FOUND",
            message="No Telegram entity was found for the given identifier.",
            http_status=404,
            retryable=False,
        )
    if isinstance(exc, UsernameInvalidError):
        return TelegramSearchError(
            code="USERNAME_INVALID", message="The given username is not a valid Telegram username.",
            http_status=422, retryable=False,
        )
    if isinstance(exc, InviteHashExpiredError):
        return TelegramSearchError(
            code="INVITE_HASH_EXPIRED", message="This invite link has expired.", http_status=410, retryable=False,
        )
    if isinstance(exc, InviteHashInvalidError):
        return TelegramSearchError(
            code="INVITE_HASH_INVALID", message="This invite link is not valid.", http_status=422, retryable=False,
        )
    if isinstance(exc, PremiumAccountRequiredError):
        return TelegramSearchError(
            code="PREMIUM_OR_QUOTA_REQUIRED",
            message="This operation requires Telegram Premium or remaining free quota on the authenticated account.",
            http_status=402,
            retryable=False,
        )
    if isinstance(exc, (AuthKeyError, SessionPasswordNeededError)):
        return TelegramSearchError(
            code="AUTHENTICATION_ERROR",
            message="The Telegram session is not authenticated or has been invalidated.",
            http_status=401,
            retryable=False,
        )
    if isinstance(exc, (ConnectionError, OSError, asyncio.TimeoutError)):
        return TelegramSearchError(
            code="NETWORK_ERROR",
            message="A network error occurred while contacting Telegram.",
            http_status=503,
            retryable=True,
        )
    if isinstance(exc, RPCError):
        return TelegramSearchError(
            code="TELEGRAM_RPC_ERROR",
            message=f"Telegram rejected the request ({exc.__class__.__name__}).",
            http_status=502,
            retryable=False,
        )
    if isinstance(exc, ValueError):
        return TelegramSearchError(code="INVALID_REQUEST", message=str(exc), http_status=422, retryable=False)

    return TelegramSearchError(
        code="UNKNOWN_ERROR", message=f"An unexpected error occurred ({exc.__class__.__name__}).",
        http_status=500, retryable=False,
    )
