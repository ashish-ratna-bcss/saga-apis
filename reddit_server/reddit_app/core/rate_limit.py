"""Per-caller sliding-window rate limiting for the shared Reddit RSS gateway
(``/api/reddit/rss/*``). Protects the shared, process-wide Reddit RSS budget
(``app/reddit/rate_limiter.py``) from any single caller monopolizing it --
a different concern from that module, which protects Reddit itself.

Scoped to the RSS router only (see ``app/main.py``): other ``/api/reddit/*``
routes are unaffected, since this exists to protect one shared, scarce
resource (Reddit's RSS budget), not as a blanket policy on the whole service.

Implemented the same way ``require_api_key`` (``app/core/security.py``) is:
a FastAPI dependency, not ASGI middleware -- this codebase has no middleware
layer beyond CORS, and a dependency composes with the existing per-router
``dependencies=[...]`` mechanism already used for the API-key gate.

Client identity: ``request.client.host``, the immediate TCP peer. Behind a
reverse proxy that doesn't rewrite the connecting address, every caller behind
it shares one bucket -- this module does not parse ``X-Forwarded-For`` itself
(spoofable without a trusted-proxy allowlist, which this repo does not have),
so that degraded-but-safe behavior is deliberate, not an oversight.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request

from reddit_app.core.exceptions import RedditRssClientRateLimitedError


class ClientRateLimiter:
    """One sliding window of recent hit timestamps per client key. Unbounded
    growth across many distinct callers is bounded by evicting keys whose
    window has gone fully idle once ``max_tracked_clients`` is exceeded --
    simplest approach that keeps memory bounded without a background task."""

    def __init__(
        self, *, max_requests: int, window_seconds: float, max_tracked_clients: int
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_tracked_clients = max_tracked_clients
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, client_key: str) -> float | None:
        """Returns ``None`` and records the hit if the caller is within
        budget, or the number of seconds until it would be if not."""

        now = time.monotonic()
        hits = self._hits[client_key]
        while hits and now - hits[0] >= self.window_seconds:
            hits.popleft()

        if len(hits) >= self.max_requests:
            return self.window_seconds - (now - hits[0])

        hits.append(now)
        if len(self._hits) > self.max_tracked_clients:
            self._evict_idle(now)
        return None

    def _evict_idle(self, now: float) -> None:
        idle = [
            key
            for key, hits in self._hits.items()
            if not hits or now - hits[-1] >= self.window_seconds
        ]
        for key in idle:
            del self._hits[key]


def get_rss_client_limiter(request: Request) -> ClientRateLimiter:
    return request.app.state.rss_client_limiter


async def rate_limit_rss_client(request: Request) -> None:
    limiter = get_rss_client_limiter(request)
    client_key = request.client.host if request.client else "unknown"
    retry_after = limiter.check(client_key)
    if retry_after is not None:
        raise RedditRssClientRateLimitedError(retry_after=max(1.0, retry_after))
