"""Non-interactive Telegram login flow + status reporting.

A server process cannot block on stdin the way the original PoC's
`client.start()` did, so login is split into explicit steps driven by the
API (send-code -> verify-code -> [verify-password if 2FA is enabled]). Once
a session file is authorized, none of this is needed again - the client
just reconnects and reuses it, exactly like the PoC.

Nothing here ever logs api_hash, the verification code, or the 2FA password.
"""
from __future__ import annotations

from dataclasses import dataclass

from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

from telegram_app.telegram.client import TelegramClientManager, TelegramNotConfiguredError


@dataclass
class AuthStepResult:
    ok: bool
    requires_password: bool = False
    error: str | None = None


async def send_code(manager: TelegramClientManager, phone: str) -> AuthStepResult:
    ok, err = await manager.connect()
    if not ok:
        return AuthStepResult(ok=False, error=err)
    try:
        sent = await manager.client.send_code_request(phone)
    except ApiIdInvalidError:
        return AuthStepResult(ok=False, error="invalid TELEGRAM_API_ID/TELEGRAM_API_HASH")
    except PhoneNumberInvalidError:
        return AuthStepResult(ok=False, error="invalid phone number")
    except FloodWaitError as exc:
        return AuthStepResult(ok=False, error=f"rate-limited by Telegram, wait {exc.seconds}s")
    except RPCError as exc:
        return AuthStepResult(ok=False, error=f"{exc.__class__.__name__}: {exc}")

    manager.pending_phone = phone
    manager.pending_phone_code_hash = sent.phone_code_hash
    return AuthStepResult(ok=True)


async def verify_code(manager: TelegramClientManager, phone: str, code: str) -> AuthStepResult:
    if not manager.pending_phone_code_hash or manager.pending_phone != phone:
        return AuthStepResult(ok=False, error="no pending code request for this phone - call send-code first")
    try:
        await manager.client.sign_in(
            phone=phone, code=code, phone_code_hash=manager.pending_phone_code_hash
        )
    except SessionPasswordNeededError:
        return AuthStepResult(ok=False, requires_password=True, error="2FA password required")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
        return AuthStepResult(ok=False, error=exc.__class__.__name__)
    except RPCError as exc:
        return AuthStepResult(ok=False, error=f"{exc.__class__.__name__}: {exc}")

    manager.pending_phone_code_hash = None
    # Re-authentication succeeded - clear any latched invalid-session state so
    # ResolveUsernameRequest/access-checking/monitoring resume immediately.
    manager.clear_session_invalid()
    return AuthStepResult(ok=True)


async def verify_password(manager: TelegramClientManager, password: str) -> AuthStepResult:
    try:
        await manager.client.sign_in(password=password)
    except RPCError as exc:
        return AuthStepResult(ok=False, error=f"{exc.__class__.__name__}: {exc}")
    manager.pending_phone_code_hash = None
    manager.pending_phone = None
    manager.clear_session_invalid()
    return AuthStepResult(ok=True)


async def get_status(manager: TelegramClientManager) -> dict:
    """Backs GET /api/telegram/status. Never returns session data or api_hash."""
    if not manager.is_configured:
        return {"connected": False, "authenticated": False, "account": None, "error": "not configured", "session_invalid": False}

    try:
        ok, err = await manager.connect()
    except TelegramNotConfiguredError as exc:
        return {"connected": False, "authenticated": False, "account": None, "error": str(exc), "session_invalid": False}

    if not ok:
        return {"connected": False, "authenticated": False, "account": None, "error": err, "session_invalid": manager.session_invalid}

    authenticated = await manager.is_authenticated()
    account = None
    if authenticated:
        me = await manager.client.get_me()
        account = {
            "id": str(me.id) if me else None,
            "username": getattr(me, "username", None) if me else None,
            "phone": _mask_phone(getattr(me, "phone", None)) if me else None,
        }
    return {
        "connected": manager.is_connected(),
        "authenticated": authenticated,
        "account": account,
        "error": manager.session_invalid_reason if manager.session_invalid else None,
        "session_invalid": manager.session_invalid,
    }


def _mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    if len(phone) <= 4:
        return "*" * len(phone)
    return phone[:2] + "*" * (len(phone) - 4) + phone[-2:]
