"""Pluggable dispatch for persisted notifications to external channels.

Notifications are always persisted to SQLite first (notification_service) -
this manager is purely about *optionally* pushing an already-persisted
notification onward (websocket/webhook/email/dashboard). No provider is
hard-coded; register callables and they will be invoked for every
notification the notification-processing scheduler job picks up.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("telegram_service.notifications.manager")

Dispatcher = Callable[[dict], Awaitable[None]]


class NotificationManager:
    def __init__(self) -> None:
        self._dispatchers: list[Dispatcher] = []

    def register(self, dispatcher: Dispatcher) -> None:
        self._dispatchers.append(dispatcher)

    async def dispatch(self, notification_payload: dict) -> None:
        for dispatcher in self._dispatchers:
            try:
                await dispatcher(notification_payload)
            except Exception:  # noqa: BLE001 - one failing provider must not block others or the job
                logger.exception("notification dispatcher failed")


notification_manager = NotificationManager()
