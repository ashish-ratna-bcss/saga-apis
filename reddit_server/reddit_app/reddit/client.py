"""Composition root for Reddit connectivity.

Owns the RSS transport's lifecycle (create at startup, close at shutdown) so
``app/main.py`` and ``app/api/deps.py`` never touch ``httpx`` directly:

- ``.rss``  -- the unauthenticated public RSS/Atom transport (``www.reddit.com``'s
  ``.rss`` endpoints). No OAuth, no credentials.
- ``.feed_cache`` -- the shared RSS response cache/coalescer (``feed_cache.py``),
  sitting in front of ``.rss`` in the request path. Lives here, not on
  ``RedditRssService``, because that service is constructed fresh per request
  (see ``app/api/deps.py``) while this manager -- and therefore this cache -- is
  a true process-wide singleton, shared by every caller.
"""

from __future__ import annotations

import httpx

from reddit_app.core.config import Settings
from reddit_app.reddit.feed_cache import FeedCache
from reddit_app.reddit.rss_client import RedditRssClient


class RedditClientManager:
    def __init__(
        self,
        settings: Settings,
        *,
        rss_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.rss = RedditRssClient(settings, transport=rss_transport)
        self.feed_cache = FeedCache(
            ttl_seconds=settings.reddit_rss_cache_ttl_seconds,
            max_entries=settings.reddit_rss_cache_max_entries,
        )

    async def start(self) -> None:
        await self.rss.start()

    async def close(self) -> None:
        await self.rss.close()
