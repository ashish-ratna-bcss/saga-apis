from __future__ import annotations

import asyncio
import time

import pytest

from reddit_app.reddit.rate_limiter import TokenBucket


async def test_first_acquire_is_immediate() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=1.0)
    started = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - started < 0.05


async def test_second_acquire_waits_for_refill() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=1 / 0.1)  # refills 1 token per 0.1s
    await bucket.acquire()
    started = time.monotonic()
    await bucket.acquire()
    assert time.monotonic() - started >= 0.08


async def test_refill_caps_at_capacity() -> None:
    bucket = TokenBucket(capacity=2, refill_rate=1000.0)
    await asyncio.sleep(0.05)  # would refill far past 2 tokens without the min() cap
    await bucket.acquire()  # forces a refill computation
    assert bucket.tokens <= 1.0  # started at capacity(2), one consumed, never exceeds cap


async def test_concurrent_acquires_are_serialized_no_double_spend() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=1 / 0.05)
    order: list[int] = []

    async def worker(i: int) -> None:
        await bucket.acquire()
        order.append(i)

    started = time.monotonic()
    await asyncio.gather(*(worker(i) for i in range(5)))
    elapsed = time.monotonic() - started
    assert len(order) == 5
    # 5 acquires at 1 token / 0.05s, minus the immediate first one, is >= 0.2s
    assert elapsed >= 0.15


async def test_acquire_raises_timeout_when_wait_exceeds_max_wait() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=1 / 60)  # next token 60s away
    await bucket.acquire()
    with pytest.raises(TimeoutError):
        await bucket.acquire(max_wait_seconds=0.01)


async def test_acquire_without_max_wait_does_not_raise_immediately() -> None:
    bucket = TokenBucket(capacity=1, refill_rate=1 / 0.05)
    await bucket.acquire()
    await bucket.acquire()  # just waits, no exception
