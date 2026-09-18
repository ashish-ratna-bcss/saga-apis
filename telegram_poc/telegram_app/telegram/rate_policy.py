"""Central request policy for the search/discovery layer.

Two concerns, deliberately kept separate from FloodWait handling itself
(which each Telegram-calling function still does on its own, close to the
actual RPC - see access_manager/join_request_manager/collector):

1. Bound how many expensive search RPCs run concurrently, so many parallel
   API callers cannot hammer Telegram at once.
2. Coalesce identical concurrent requests (same operation + same params) so
   a burst of duplicate calls (e.g. a retried client, or several analysts
   running the same search) shares one in-flight Telegram call instead of
   issuing it N times.

This module never retries anything itself and never sleeps - it only bounds
concurrency and de-duplicates. Respecting FloodWait's actual wait period is
still the caller's job (surfaced via app.telegram.errors.TelegramSearchError).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger("telegram_service.telegram.rate_policy")

T = TypeVar("T")

# How many Telegram search RPCs (global search, channel search, discovery)
# may be in flight at once, across all API callers.
SEARCH_CONCURRENCY_LIMIT = 3

_search_semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY_LIMIT)


class InFlightDeduplicator:
    """Coalesces concurrent calls that share the same key into one execution."""

    def __init__(self) -> None:
        self._in_flight: dict[str, asyncio.Future] = {}

    async def run_or_join(self, key: str, factory: Callable[[], Awaitable[T]], semaphore: asyncio.Semaphore) -> T:
        """A joiner only awaits someone else's already-in-flight result - it
        never acquires `semaphore` itself, since it isn't starting a new
        Telegram RPC. Only the one caller that actually becomes the producer
        (the first to see no in-flight entry for `key`) acquires a permit,
        and only around the `factory()` call itself. This keeps "how many
        callers can share one batch" independent of the concurrency limit."""
        existing = self._in_flight.get(key)
        if existing is not None:
            logger.info("joining in-flight request for key=%s", key)
            return await existing

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._in_flight[key] = future
        try:
            async with semaphore:
                result = await factory()
        except BaseException as exc:  # noqa: BLE001 - propagate to every waiter, then re-raise here too
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._in_flight.pop(key, None)


search_deduplicator = InFlightDeduplicator()


async def run_bounded_search(key: str, factory: Callable[[], Awaitable[T]]) -> T:
    """Runs `factory()` under the shared concurrency limit, de-duplicating
    identical concurrent calls by `key`."""
    return await search_deduplicator.run_or_join(key, factory, _search_semaphore)


def make_search_cache_key(operation: str, **params) -> str:
    parts = [operation] + [f"{k}={params[k]}" for k in sorted(params)]
    return "|".join(parts)
