"""Telegram MTProto client lifecycle management (Telethon).

This is the ONLY module that is allowed to construct a `TelegramClient`.
Everything else in `app/telegram/*` receives a connected client instance;
nothing above this layer (services, routes) ever imports Telethon directly.

Connection/auth-error handling here mirrors the patterns already proven out
in `poc/main.py::connect_and_authenticate` - reused rather
than re-invented, adapted for a long-lived server process instead of a
one-shot CLI run (no blocking interactive prompts; see authentication.py for
the non-interactive send-code/verify-code/verify-password flow).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    ActiveUserRequiredError,
    ApiIdInvalidError,
    AuthKeyError,
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    AuthKeyUnregisteredError,
    RPCError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from telegram_app.config import Settings, get_settings

logger = logging.getLogger("telegram_service.telegram.client")

# RPC errors that mean the *account/session itself* is no longer valid on
# Telegram's servers (revoked, logged out elsewhere, deactivated, ...) - as
# opposed to a per-operation failure like "channel is private". Deliberately
# excludes SessionPasswordNeededError (that's a normal step of the sign-in
# flow, not an invalidated session) and the unrelated AuthKeyError siblings
# (e.g. AuthKeyDuplicatedError, FilerefUpgradeNeededError) that don't mean
# re-authentication is required.
SESSION_INVALID_ERRORS: tuple[type[Exception], ...] = (
    AuthKeyUnregisteredError,
    AuthKeyInvalidError,
    AuthKeyPermEmptyError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
    ActiveUserRequiredError,
)

SESSION_INVALID_MESSAGE = (
    "The Telegram authorization session is no longer valid. Re-authentication is required."
)


def is_session_invalid_error(exc: BaseException) -> bool:
    return isinstance(exc, SESSION_INVALID_ERRORS)


class TelegramNotConfiguredError(RuntimeError):
    """Raised when TELEGRAM_API_ID / TELEGRAM_API_HASH are not set."""


class TelegramClientManager:
    """Owns a single Telethon client for the whole process.

    Holds only connection/session state - no business logic, no DB access.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: TelegramClient | None = None
        self._connected_at: datetime | None = None
        self._last_error: str | None = None
        # Latched once a SESSION_INVALID_ERRORS exception is observed anywhere
        # (connect, ResolveUsernameRequest, access checks, monitoring, ...).
        # While set, every Telegram-requiring operation short-circuits without
        # making another RPC - this is what stops the app from repeatedly
        # hammering Telegram with a key it already knows is unregistered.
        # Only re-authentication (verify_code/verify_password succeeding)
        # clears it.
        self._session_invalid: bool = False
        self._session_invalid_reason: str | None = None
        # Guards against concurrent first-time client construction/connect
        # (e.g. several requests arriving during startup at once).
        self._init_lock: asyncio.Lock = asyncio.Lock()
        # Transient state for the non-interactive login handshake (never persisted).
        self.pending_phone: str | None = None
        self.pending_phone_code_hash: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.telegram_api_id and self._settings.telegram_api_hash)

    @property
    def client(self) -> TelegramClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> TelegramClient:
        if not self.is_configured:
            raise TelegramNotConfiguredError(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH are not configured. Set them in .env."
            )
        Path(self._settings.telegram_session_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        # api_hash is passed straight through to Telethon and is never logged/printed anywhere in this codebase.
        return TelegramClient(
            self._settings.telegram_session_path,
            self._settings.telegram_api_id,
            self._settings.telegram_api_hash,
        )

    async def connect(self) -> tuple[bool, str | None]:
        """Connects (or reconnects) the client. Returns (ok, error_reason).

        Never raises - callers (auth service, health-check job) always get a
        clean result to persist/log, matching the PoC's philosophy of
        reporting connection failures honestly instead of crashing. Guarded
        by `_init_lock` so concurrent callers (e.g. several API requests
        arriving before the first connect completes) don't race to build/
        connect the shared client at once.
        """
        if not self.is_configured:
            return False, "TELEGRAM_API_ID / TELEGRAM_API_HASH not configured"
        async with self._init_lock:
            try:
                await self.client.connect()
            except ApiIdInvalidError:
                self._last_error = "invalid TELEGRAM_API_ID/TELEGRAM_API_HASH"
                return False, self._last_error
            except (ConnectionError, OSError) as exc:
                self._last_error = f"network error reaching Telegram: {exc}"
                return False, self._last_error
            except SESSION_INVALID_ERRORS as exc:
                self.mark_session_invalid(f"{exc.__class__.__name__}: {exc}")
                return False, self._session_invalid_reason
            except AuthKeyError:
                self._last_error = "session auth key is invalid/revoked - re-authentication required"
                return False, self._last_error
            except RPCError as exc:
                self._last_error = f"Telegram RPC error: {exc.__class__.__name__}: {exc}"
                return False, self._last_error

            self._connected_at = datetime.now(timezone.utc)
            self._last_error = None
            return True, None

    async def disconnect(self) -> None:
        if self._client is not None and self._client.is_connected():
            await self._client.disconnect()

    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    @property
    def session_invalid(self) -> bool:
        return self._session_invalid

    @property
    def session_invalid_reason(self) -> str | None:
        return self._session_invalid_reason

    def mark_session_invalid(self, reason: str) -> None:
        """Latches the session as invalid so nothing keeps retrying it.

        `reason` must only ever be an exception class name + Telegram's own
        public error text (the same pattern used everywhere else in this
        module) - never api_hash, session contents, phone, code, or password.
        """
        self._session_invalid = True
        self._session_invalid_reason = reason
        logger.warning("telegram session marked invalid: %s", reason)

    def clear_session_invalid(self) -> None:
        if self._session_invalid:
            logger.info("telegram session re-validated - clearing invalid state")
        self._session_invalid = False
        self._session_invalid_reason = None

    async def is_authenticated(self) -> bool:
        if self._session_invalid:
            return False
        if not self.is_connected():
            return False
        try:
            return await self.client.is_user_authorized()
        except SESSION_INVALID_ERRORS as exc:
            self.mark_session_invalid(f"{exc.__class__.__name__}: {exc}")
            return False
        except RPCError:
            return False

    async def verify_authorized(self) -> tuple[bool, str | None]:
        """Pre-flight check required before any operation that needs an
        authenticated session (ResolveUsernameRequest / entity resolution,
        channel discovery, search, access checking, monitoring).

        Never issues the caller's actual RPC itself - only confirms the
        client is connected and the account is authorized first, so a known-
        bad session is reported as TELEGRAM_SESSION_INVALID instead of the
        underlying operation being attempted (and misclassified) again.
        """
        if self._session_invalid:
            return False, self._session_invalid_reason or SESSION_INVALID_MESSAGE
        ok, err = await self.connect()
        if not ok:
            return False, err
        authorized = await self.is_authenticated()
        if not authorized:
            if not self._session_invalid:
                self.mark_session_invalid("is_user_authorized() returned False")
            return False, self._session_invalid_reason
        return True, None

    async def ensure_connected(self) -> tuple[bool, str | None]:
        """Reconnects only if needed - used by the connection-health job.

        A single attempt per call; the scheduler's interval provides the
        retry cadence, so this never loops/sleeps internally (no aggressive
        retry behavior).
        """
        if self.is_connected():
            return True, None
        logger.info("telegram client disconnected - attempting reconnect")
        return await self.connect()

    def connection_snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.is_connected(),
            "connected_at": self._connected_at.isoformat() if self._connected_at else None,
            "last_error": self._last_error,
            "session_invalid": self._session_invalid,
        }


_manager: TelegramClientManager | None = None


def get_client_manager() -> TelegramClientManager:
    """Process-wide singleton - one Telegram MTProto connection per service instance."""
    global _manager
    if _manager is None:
        _manager = TelegramClientManager(get_settings())
    return _manager
