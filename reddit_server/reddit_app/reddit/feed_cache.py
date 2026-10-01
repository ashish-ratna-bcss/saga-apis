"""Process-local cache + request-coalescing for raw Reddit RSS feed bytes.

Sits between ``RedditRssService`` and ``RedditRssClient`` in the request path
(``Service -> FeedCache -> RedditRssClient -> RSS host``). Keyed on the *normalized
feed URL* (path + sorted query params) the RSS client would fetch -- **not**
on the caller's request shape -- so N callers hitting the same feed with
different ``keywords``/``exclude``/``match_field`` still share one Reddit
fetch; filtering happens after the shared feed comes back, entirely inside
``reddit_rss_service.py``. This module never sees keywords, subreddits as a
list, or any caller-specific option -- only the already-built ``(path,
params)`` pair.

One instance lives on ``RedditClientManager`` (``app.state.clients.feed_cache``),
constructed once per process from ``Settings``. This codebase shares state via
dependency injection on an existing per-process singleton, not a bare module global.

Process-local: behind multiple workers/instances this cache (and the
coalescing it provides) is per-process.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


def cache_key(path: str, params: dict[str, str]) -> str:
    """Canonical cache key for one feed request. Query-param order never
    changes the feed Reddit returns, so it's sorted away; ``path`` (including
    the "+"-joined combined-subreddit segment) is already canonical from
    ``rss_urls.py``."""

    query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    return f"{path}?{query}" if query else path


@dataclass
class _Entry:
    data: bytes | None = None
    fetched_at: float = 0.0
    inflight: asyncio.Event | None = None


class FeedCache:
    """``get_or_fetch`` returns a fresh-enough cached feed immediately, joins
    an in-flight fetch for the same key if one is already running, or becomes
    the fetcher itself. A failed fetch is never cached and is retried by
    whichever waiter (if any) wakes up next -- naturally serialized through
    the same coalescing path, never a thundering herd."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._store: OrderedDict[str, _Entry] = OrderedDict()
        self._store_lock = asyncio.Lock()

    def _is_fresh(self, entry: _Entry | None, *, now: float) -> bool:
        return entry is not None and entry.data is not None and (now - entry.fetched_at) < self._ttl

    async def get_or_fetch(self, key: str, fetch_fn: Callable[[], Awaitable[bytes]]) -> bytes:
        while True:
            waiter: asyncio.Event | None = None
            am_fetcher = False
            async with self._store_lock:
                now = time.monotonic()
                entry = self._store.get(key)
                if self._is_fresh(entry, now=now):
                    assert entry is not None and entry.data is not None
                    self._store.move_to_end(key)
                    return entry.data
                if entry is not None and entry.inflight is not None:
                    waiter = entry.inflight
                else:
                    entry = _Entry(inflight=asyncio.Event())
                    self._store[key] = entry
                    self._evict_if_needed()
                    am_fetcher = True

            if not am_fetcher:
                assert waiter is not None
                await waiter.wait()
                continue  # re-check: now fresh (fetcher succeeded) or gone (it failed)

            try:
                data = await fetch_fn()
            except BaseException:
                async with self._store_lock:
                    # Never cache a failure; pop so the next waiter retries cleanly.
                    self._store.pop(key, None)
                assert entry.inflight is not None
                entry.inflight.set()
                raise
            else:
                async with self._store_lock:
                    entry.data = data
                    entry.fetched_at = time.monotonic()
                    self._store.move_to_end(key)
                assert entry.inflight is not None
                entry.inflight.set()
                entry.inflight = None
                return data

    def _evict_if_needed(self) -> None:
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)
