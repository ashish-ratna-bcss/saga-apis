"""Best-effort event-driven detection of access changes.

Telegram does not guarantee a distinct "your join request was approved"
push event separate from ordinary membership updates, so this listens for
the closest reliable signal - a ChatAction update where the authenticated
account itself is the one who joined/was added - and asks the caller to
reconcile that one source immediately instead of waiting for the next
12-hour cycle.

This is explicitly a *fast path*, not the source of truth: the mandatory
12-hour reconciliation job (scheduler/jobs.py) still runs regardless and is
what the acceptance criteria rely on. If this event never fires (e.g. the
update type changes on Telegram's side, or the app was offline when it was
delivered), reconciliation still catches the change within 12 hours.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from telethon import events

logger = logging.getLogger("telegram_service.telegram.event_handler")

OnAccessChange = Callable[[int], Awaitable[None]]


def register_access_change_handlers(client, get_me_id: Callable[[], int | None], on_access_change: OnAccessChange) -> None:
    """Registers a ChatAction handler that triggers `on_access_change(chat_id)`
    when the authenticated account appears to have newly gained membership."""

    @client.on(events.ChatAction)
    async def _handler(event: events.ChatAction.Event) -> None:  # pragma: no cover - exercised via direct unit test of the callback
        try:
            if not (event.user_joined or event.user_added):
                return
            me_id = get_me_id()
            if me_id is None:
                return
            user = await event.get_user()
            if user is None or user.id != me_id:
                return
            chat = await event.get_chat()
            if chat is None:
                return
            logger.info("event-driven access change signal for chat_id=%s", chat.id)
            await on_access_change(chat.id)
        except Exception:  # noqa: BLE001 - a best-effort fast path must never crash the client's update loop
            logger.exception("error handling ChatAction event for access-change detection")
