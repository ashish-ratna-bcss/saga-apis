"""A process-wide, proactive Reddit RSS outbound rate limiter.

This is a **hard pacing gate**, distinct from ``RedditRssClient``'s existing
*reactive* throttle (``_Bucket``/``_wait_for_slot`` in ``rss_client.py``, which
only backs off after Reddit's own ``X-Ratelimit-*`` response headers say to).
That reactive throttle does nothing for the very first burst of concurrent
requests -- if ten callers hit this service at once, all ten would fire before
any response header ever comes back telling them to stop. This bucket paces
every outbound attempt *before* it leaves the process, retries included, which
is what actually keeps a many-callers-one-process deployment under Reddit's
~1 request/minute unauthenticated ceiling (see INTEGRATION.md).

One instance lives on ``RedditRssClient`` (constructed once from ``Settings``).
Since exactly one ``RedditRssClient`` exists per process (``app.state.clients.rss``,
shared by every request through ``RssServiceDep`` -- see ``app/api/deps.py``),
that single instance already *is* the process-wide budget. No bare
module-level singleton is used here: this codebase always threads shared state
through dependency injection (``RedditClientManager`` on ``app.state``, per-
request service wrappers around it), and a bare global would also leak across
tests, which construct a fresh ``Settings``/``RedditRssClient`` per test.

Multi-process/multi-instance deployment note: this bucket is process-local.
Behind N worker processes or N instances sharing one Reddit-facing IP, each
gets its own budget, so the aggregate could still exceed Reddit's tolerance.
A distributed backend (e.g. Redis) would be needed to share one true budget
across processes -- deliberately not added here, since this repo has no such
dependency today and none is required for a single-process deployment.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """A single-process asyncio token bucket. ``acquire()`` waits until a
    token is available, refilling continuously at ``refill_rate`` tokens/sec
    up to ``capacity``. The lock serializes waiters so token accounting never
    races -- correct for a hard gate where only one caller should be consuming
    the refill clock at a time."""

    capacity: float
    refill_rate: float
    tokens: float = field(init=False)
    _last_refill: float = field(init=False, repr=False)
    _lock: asyncio.Lock = field(init=False, repr=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    async def acquire(self, *, max_wait_seconds: float | None = None) -> None:
        """Block until a token is available. If ``max_wait_seconds`` is given
        and the projected wait would exceed it, raise ``TimeoutError``
        immediately instead of sleeping -- protects against a request queueing
        forever behind a starved budget (see ``RedditRssQueueTimeoutError``,
        which ``RedditRssClient`` translates this into)."""

        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
                self._last_refill = now

                if self.tokens >= 1:
                    self.tokens -= 1
                    return

                wait = (1 - self.tokens) / self.refill_rate
                if max_wait_seconds is not None and wait > max_wait_seconds:
                    raise TimeoutError(
                        f"would need to wait {wait:.1f}s for the shared Reddit RSS "
                        f"budget, exceeds max_wait_seconds={max_wait_seconds}"
                    )
                await asyncio.sleep(wait)
