from __future__ import annotations

import asyncio

import pytest

from reddit_app.reddit.feed_cache import FeedCache, cache_key


def test_cache_key_sorts_params_and_ignores_order() -> None:
    a = cache_key("/search.rss", {"q": "protest", "sort": "new"})
    b = cache_key("/search.rss", {"sort": "new", "q": "protest"})
    assert a == b


def test_cache_key_distinguishes_different_feeds() -> None:
    a = cache_key("/r/india/search.rss", {"q": "protest"})
    b = cache_key("/r/hyderabad/search.rss", {"q": "protest"})
    assert a != b


async def test_fresh_hit_does_not_refetch() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=10)
    calls = 0

    async def fetch() -> bytes:
        nonlocal calls
        calls += 1
        return b"feed-data"

    first = await cache.get_or_fetch("k", fetch)
    second = await cache.get_or_fetch("k", fetch)
    assert first == second == b"feed-data"
    assert calls == 1


async def test_stale_entry_is_refetched() -> None:
    cache = FeedCache(ttl_seconds=0.05, max_entries=10)
    calls = 0

    async def fetch() -> bytes:
        nonlocal calls
        calls += 1
        return f"feed-{calls}".encode()

    first = await cache.get_or_fetch("k", fetch)
    await asyncio.sleep(0.08)
    second = await cache.get_or_fetch("k", fetch)
    assert first != second
    assert calls == 2


async def test_concurrent_requests_for_same_key_coalesce_to_one_fetch() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=10)
    calls = 0

    async def slow_fetch() -> bytes:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return b"shared-feed"

    results = await asyncio.gather(*(cache.get_or_fetch("k", slow_fetch) for _ in range(10)))
    assert calls == 1
    assert all(result == b"shared-feed" for result in results)


async def test_different_keys_never_coalesce() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=10)
    calls: list[str] = []

    async def fetch_for(key: str):
        async def _fetch() -> bytes:
            calls.append(key)
            return key.encode()

        return await cache.get_or_fetch(key, _fetch)

    results = await asyncio.gather(fetch_for("a"), fetch_for("b"), fetch_for("c"))
    assert sorted(calls) == ["a", "b", "c"]
    assert set(results) == {b"a", b"b", b"c"}


async def test_failed_fetch_is_not_cached_and_can_be_retried() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=10)
    attempts = 0

    async def flaky_fetch() -> bytes:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return b"ok-on-retry"

    with pytest.raises(RuntimeError):
        await cache.get_or_fetch("k", flaky_fetch)
    result = await cache.get_or_fetch("k", flaky_fetch)
    assert result == b"ok-on-retry"
    assert attempts == 2


async def test_coalesced_waiters_all_see_the_failure_and_can_retry() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=10)
    attempts = 0

    async def fails_once_then_succeeds() -> bytes:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.02)
        if attempts == 1:
            raise RuntimeError("boom")
        return b"ok"

    results = await asyncio.gather(
        *(cache.get_or_fetch("k", fails_once_then_succeeds) for _ in range(5)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, RuntimeError)]
    successes = [r for r in results if r == b"ok"]
    assert len(failures) + len(successes) == 5
    assert len(failures) >= 1
    assert len(successes) >= 1


async def test_cancellation_of_fetcher_frees_waiters() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=10)

    async def hangs_forever() -> bytes:
        await asyncio.sleep(10)
        return b"never"

    fetcher_task = asyncio.create_task(cache.get_or_fetch("k", hangs_forever))
    await asyncio.sleep(0.01)  # let it become the fetcher and register inflight

    async def waiter() -> bytes | BaseException:
        try:
            return await cache.get_or_fetch("k", hangs_forever)
        except BaseException as exc:  # noqa: BLE001 - test wants to see cancellation-driven retry
            return exc

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    fetcher_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fetcher_task

    # The waiter should not hang forever -- it wakes once the fetcher's inflight
    # event is set on cancellation and becomes the new fetcher itself.
    result = await asyncio.wait_for(waiter_task, timeout=1.0)
    assert result != b"never"


async def test_max_entries_evicts_oldest() -> None:
    cache = FeedCache(ttl_seconds=60.0, max_entries=2)

    async def fetch_for(key: str) -> bytes:
        return key.encode()

    await cache.get_or_fetch("a", lambda: fetch_for("a"))
    await cache.get_or_fetch("b", lambda: fetch_for("b"))
    await cache.get_or_fetch("c", lambda: fetch_for("c"))
    assert len(cache._store) <= 2
    assert "a" not in cache._store


