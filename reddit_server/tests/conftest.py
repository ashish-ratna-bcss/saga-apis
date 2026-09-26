"""Shared pytest fixtures.

Every test runs against the :mod:`tests.fake_reddit_rss` stand-in. No real
Reddit credentials, server or network access are required -- this service has
no Reddit OAuth transport to fake in the first place.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from reddit_app.core.config import Settings
from reddit_app.reddit.feed_cache import FeedCache
from reddit_app.reddit.rss_client import RedditRssClient
from reddit_app.services.reddit_rss_service import RedditRssService
from tests.fake_reddit_rss import FakeRedditRss


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's real .env from leaking into the test run."""

    for key in list(os.environ):
        if key.upper().startswith(("REDDIT_", "API_KEYS", "CORS_")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_name="reddit-service-test",
        environment="test",
        reddit_rss_max_retries=2,
        reddit_rss_retry_base_delay_seconds=0.01,
        # Fast/effectively-unthrottled by default so existing tests aren't paced
        # by the shared-gateway limiter; tests of that limiter override these.
        reddit_rss_global_rate=1000.0,
        reddit_rss_global_burst=1000,
        reddit_rss_cache_ttl_seconds=60.0,
        log_level="WARNING",
    )


@pytest.fixture
def fake_reddit_rss() -> FakeRedditRss:
    return FakeRedditRss()


@pytest_asyncio.fixture
async def rss_client(
    settings: Settings, fake_reddit_rss: FakeRedditRss
) -> AsyncIterator[RedditRssClient]:
    client = RedditRssClient(settings, transport=fake_reddit_rss.transport())
    await client.start()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def feed_cache(settings: Settings) -> FeedCache:
    return FeedCache(
        ttl_seconds=settings.reddit_rss_cache_ttl_seconds,
        max_entries=settings.reddit_rss_cache_max_entries,
    )


@pytest.fixture
def rss_service(
    rss_client: RedditRssClient, feed_cache: FeedCache, settings: Settings
) -> RedditRssService:
    return RedditRssService(
        rss_client, feed_cache, event_threshold=settings.reddit_rss_event_threshold
    )


@pytest.fixture
def app_client(settings: Settings, fake_reddit_rss: FakeRedditRss) -> Iterator:
    """A TestClient whose lifespan (startup/shutdown) actually runs."""

    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    application = create_app(settings, rss_transport=fake_reddit_rss.transport())
    with TestClient(application) as client:
        yield client
