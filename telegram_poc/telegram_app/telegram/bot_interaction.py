"""Explicit Telegram bot-start interaction.

Sends exactly one `/start` message via the existing authenticated Telethon
client and waits, bounded, for the bot's reply - the API equivalent of
pressing "Start Bot" in the Telegram app. Nothing beyond that one message is
ever sent (no arbitrary button clicks, no callback execution), and the wait
is bounded by `timeout_seconds` - never an unbounded/indefinite poll.

Uses a plain bounded poll over `iter_messages` rather than Telethon's
`conversation()` helper, deliberately, to stay consistent with the rest of
`app/telegram/*` (`collector.py`, and `event_handler.py`'s own explicit
"best-effort, not source of truth" framing): every Telegram-facing operation
here is a plain async function exercisable against a fake client in tests,
not a hidden event-driven state machine.

Only the bot's one reply message is inspected here (text + button URLs);
nothing about the wider chat history is fetched or persisted - see
`app/services/bot_service.py` for what happens to that reply next.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from telethon.errors import FloodWaitError, RPCError

from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE


@dataclass
class BotButton:
    text: str
    url: str | None = None


@dataclass
class BotStartOutcome:
    sent: bool
    response_text: str | None = None
    response_message_id: int | None = None
    response_date: datetime | None = None
    buttons: list[BotButton] = field(default_factory=list)
    timed_out: bool = False
    error: str | None = None
    session_invalid: bool = False


def _extract_buttons(message) -> list[BotButton]:
    """Reads inline-keyboard URL buttons off a message's `reply_markup`.
    Callback buttons (no `url`) are still surfaced (text only, url=None) so
    a caller can see they exist, but this module never executes one - see
    the module docstring's "no callback execution" rule."""
    markup = getattr(message, "reply_markup", None)
    if markup is None:
        return []
    buttons: list[BotButton] = []
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            buttons.append(BotButton(text=getattr(button, "text", "") or "", url=getattr(button, "url", None)))
    return buttons


async def start_bot(client, entity, *, timeout_seconds: float, poll_interval_seconds: float = 1.0) -> BotStartOutcome:
    """Sends `/start` to `entity`, then polls (bounded by `timeout_seconds`)
    for the bot's first reply newer than the sent message. Returns as soon
    as a reply is seen - never waits out the full timeout unnecessarily."""
    try:
        sent = await client.send_message(entity, "/start")
    except SESSION_INVALID_ERRORS as exc:
        return BotStartOutcome(sent=False, error=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True)
    except FloodWaitError as exc:
        return BotStartOutcome(sent=False, error=f"FloodWaitError - wait {exc.seconds}s before retrying")
    except RPCError as exc:
        return BotStartOutcome(sent=False, error=f"{exc.__class__.__name__}: {exc}")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    while True:
        try:
            # reverse=False (newest-first) explicitly - the bot's reply, if
            # any, is the newest message in this chat, so this finds it in
            # one page without depending on iter_messages' own default.
            async for message in client.iter_messages(entity, limit=10, reverse=False):
                if message.id > sent.id and not getattr(message, "out", False):
                    return BotStartOutcome(
                        sent=True,
                        response_text=message.message or None,
                        response_message_id=message.id,
                        response_date=message.date.astimezone(timezone.utc) if message.date else None,
                        buttons=_extract_buttons(message),
                    )
        except SESSION_INVALID_ERRORS as exc:
            return BotStartOutcome(sent=True, error=f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True)
        except FloodWaitError as exc:
            return BotStartOutcome(sent=True, error=f"FloodWaitError - wait {exc.seconds}s before retrying")
        except RPCError as exc:
            return BotStartOutcome(sent=True, error=f"{exc.__class__.__name__}: {exc}")

        remaining = deadline - loop.time()
        if remaining <= 0:
            return BotStartOutcome(sent=True, timed_out=True, error="no response from bot within timeout")
        await asyncio.sleep(min(poll_interval_seconds, remaining))
