"""TelegramClientManager: session-invalid latch, singleton, and client
caching - the machinery behind "do not continuously retry the same invalid
authorization key" and "one shared TelegramClient instance".

These construct TelegramClientManager directly (not via StubClientManager)
since the latch/caching behavior under test is the manager's own
implementation. No test here calls .connect() in a way that would reach the
network - once mark_session_invalid() is set, verify_authorized()/
is_authenticated() return before ever touching the (unconnected) Telethon
client, and the concurrency test monkeypatches the underlying connect().
"""
from __future__ import annotations

import asyncio

from telegram_app.telegram.client import TelegramClientManager, get_client_manager


def test_mark_session_invalid_latches_state(settings):
    manager = TelegramClientManager(settings)
    assert manager.session_invalid is False
    assert manager.session_invalid_reason is None

    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")

    assert manager.session_invalid is True
    assert "AuthKeyUnregisteredError" in manager.session_invalid_reason


async def test_is_authenticated_short_circuits_without_network_once_invalid(settings):
    manager = TelegramClientManager(settings)
    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")

    # Never connected, yet this must return False immediately rather than
    # attempting to reach Telegram.
    assert await manager.is_authenticated() is False


async def test_verify_authorized_short_circuits_once_session_invalid(settings):
    manager = TelegramClientManager(settings)
    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")

    ok, reason = await manager.verify_authorized()

    assert ok is False
    assert "AuthKeyUnregisteredError" in reason


def test_clear_session_invalid_resets_state(settings):
    manager = TelegramClientManager(settings)
    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")
    assert manager.session_invalid is True

    manager.clear_session_invalid()

    assert manager.session_invalid is False
    assert manager.session_invalid_reason is None


def test_client_property_builds_once_and_is_cached(settings):
    manager = TelegramClientManager(settings)
    first = manager.client
    second = manager.client
    assert first is second


def test_get_client_manager_is_a_process_wide_singleton():
    """The FastAPI app and the scheduler both call get_client_manager() -
    this is what guarantees they share one TelegramClient instead of each
    opening its own Telegram session."""
    first = get_client_manager()
    second = get_client_manager()
    assert first is second


async def test_connect_serializes_concurrent_callers(settings, monkeypatch):
    """Several callers racing to connect() at once (e.g. requests arriving
    during startup) must not race to build/connect the client concurrently."""
    manager = TelegramClientManager(settings)

    concurrent = 0
    max_concurrent = 0

    async def fake_connect():
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1

    # Real (but unconnected/local-only) Telethon client - constructing it
    # touches only the local session file, never the network.
    monkeypatch.setattr(manager.client, "connect", fake_connect)

    results = await asyncio.gather(manager.connect(), manager.connect(), manager.connect())

    assert max_concurrent == 1
    assert all(ok for ok, _ in results)
