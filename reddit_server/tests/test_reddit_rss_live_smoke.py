"""Optional live smoke test against the real Reddit RSS host.

Skipped by default -- the normal unit test suite must never depend on live
Reddit availability (Reddit's own RSS rate limits make this flaky by nature, see
INTEGRATION.md's RSS limitations). Enable explicitly:

    RUN_REDDIT_RSS_INTEGRATION=1 pytest tests/test_reddit_rss_live_smoke.py
"""

from __future__ import annotations

import os

import pytest

from reddit_app.core.config import Settings
from reddit_app.reddit.feed_cache import FeedCache
from reddit_app.reddit.rss_client import RedditRssClient
from reddit_app.services.reddit_rss_service import RedditRssService

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REDDIT_RSS_INTEGRATION") != "1",
    reason="live network test; set RUN_REDDIT_RSS_INTEGRATION=1 to enable",
)


async def test_live_reddit_rss_search_with_zero_configuration() -> None:
    settings = Settings(_env_file=None)  # the whole point: no config at all is needed

    client = RedditRssClient(settings)
    feed_cache = FeedCache(
        ttl_seconds=settings.reddit_rss_cache_ttl_seconds,
        max_entries=settings.reddit_rss_cache_max_entries,
    )
    service = RedditRssService(client, feed_cache, event_threshold=settings.reddit_rss_event_threshold)
    await client.start()
    try:
        result = await service.monitor(query="python", limit=5)
    finally:
        await client.close()

    assert result["authenticated"] is False
    assert isinstance(result["posts"], list)
