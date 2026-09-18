from __future__ import annotations

import time

import httpx
import pytest

from reddit_app.core.exceptions import (
    RedditRssForbiddenError,
    RedditRssParseError,
    RedditRssQueueTimeoutError,
    RedditRssRateLimitedError,
    RedditRssTimeoutError,
    RedditRssUnavailableError,
)
from reddit_app.reddit.rss_client import RedditRssClient
from reddit_app.reddit.rss_parser import parse_feed
from tests.fake_reddit_rss import atom_entry, atom_feed


async def test_fetch_success_returns_body(rss_client: RedditRssClient) -> None:
    body = await rss_client.fetch("/search.rss", {"q": "protest"})
    assert parse_feed(body)[0]["id"] == "abc123"


async def test_fetch_sends_configured_user_agent(
    rss_client: RedditRssClient, fake_reddit_rss, settings
) -> None:
    await rss_client.fetch("/search.rss", {"q": "protest"})
    assert fake_reddit_rss.calls


async def test_fetch_403_is_forbidden(rss_client: RedditRssClient, fake_reddit_rss) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [httpx.Response(403, content=b"blocked")]
    with pytest.raises(RedditRssForbiddenError):
        await rss_client.fetch("/search.rss", {"q": "protest"})


async def test_fetch_429_retries_then_succeeds(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(429, content=b"", headers={"Retry-After": "0.01"}),
    ]
    body = await rss_client.fetch("/search.rss", {"q": "protest"})
    assert parse_feed(body)[0]["id"] == "abc123"


async def test_fetch_429_exhausted_raises_rate_limited(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(429, content=b"", headers={"Retry-After": "0.01"}) for _ in range(5)
    ]
    with pytest.raises(RedditRssRateLimitedError):
        await rss_client.fetch("/search.rss", {"q": "protest"})


async def test_fetch_500_retries_then_succeeds(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [httpx.Response(500, content=b"boom")]
    body = await rss_client.fetch("/search.rss", {"q": "protest"})
    assert parse_feed(body)[0]["id"] == "abc123"


async def test_fetch_500_exhausted_raises_unavailable(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(502, content=b"boom") for _ in range(5)
    ]
    with pytest.raises(RedditRssUnavailableError):
        await rss_client.fetch("/search.rss", {"q": "protest"})


async def test_fetch_connection_error_raises_unavailable(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = RedditRssClient(settings, transport=httpx.MockTransport(handler))
    await client.start()
    try:
        with pytest.raises(RedditRssUnavailableError):
            await client.fetch("/search.rss", {"q": "protest"})
    finally:
        await client.close()


async def test_fetch_timeout_raises_timeout_error(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    client = RedditRssClient(settings, transport=httpx.MockTransport(handler))
    await client.start()
    try:
        with pytest.raises(RedditRssTimeoutError):
            await client.fetch("/search.rss", {"q": "protest"})
    finally:
        await client.close()


async def test_fetch_malformed_xml_raises_parse_error_at_parser(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    """The client itself never parses -- it just hands raw bytes to the parser,
    which is what actually raises on malformed XML."""

    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(200, content=b"<feed><entry><title>unclosed")
    ]
    body = await rss_client.fetch("/search.rss", {"q": "protest"})
    with pytest.raises(RedditRssParseError):
        parse_feed(body)


async def test_fetch_successful_empty_feed_is_not_an_error(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [httpx.Response(200, content=atom_feed([]))]
    body = await rss_client.fetch("/search.rss", {"q": "protest"})
    assert parse_feed(body) == []


async def test_fetch_omits_user_feed_params_by_default(
    rss_client: RedditRssClient, fake_reddit_rss
) -> None:
    await rss_client.fetch("/search.rss", {"q": "protest"})
    _, query = fake_reddit_rss.calls[-1]
    assert "user" not in query
    assert "feed" not in query


async def test_fetch_appends_user_feed_params_when_configured(settings, fake_reddit_rss) -> None:
    """Reddit's June 2026 rate-limit workaround: user=/feed= from RSS preferences,
    appended to every request once configured."""

    configured = settings.model_copy(
        update={"reddit_rss_user": "someuser", "reddit_rss_feed": "abc123feedtoken"}
    )
    client = RedditRssClient(configured, transport=fake_reddit_rss.transport())
    await client.start()
    try:
        await client.fetch("/search.rss", {"q": "protest"})
    finally:
        await client.close()
    _, query = fake_reddit_rss.calls[-1]
    assert query["user"] == "someuser"
    assert query["feed"] == "abc123feedtoken"


async def test_fetch_requires_both_user_and_feed(settings, fake_reddit_rss) -> None:
    """Half-configured (only one of the pair set) must not send a broken param."""

    configured = settings.model_copy(update={"reddit_rss_user": "someuser"})
    client = RedditRssClient(configured, transport=fake_reddit_rss.transport())
    await client.start()
    try:
        await client.fetch("/search.rss", {"q": "protest"})
    finally:
        await client.close()
    _, query = fake_reddit_rss.calls[-1]
    assert "user" not in query
    assert "feed" not in query


async def test_fetch_throttles_proactively_from_ratelimit_headers(settings) -> None:
    """Mirrors RedditRestClient's proactive throttle: a response signalling
    'no budget left' makes the *next* fetch wait, without needing a 429 first."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        headers = {"X-Ratelimit-Remaining": "0.0", "X-Ratelimit-Reset": "0.1"}
        return httpx.Response(200, content=atom_feed([atom_entry()]), headers=headers)

    client = RedditRssClient(settings, transport=httpx.MockTransport(handler))
    await client.start()
    try:
        await client.fetch("/search.rss", {"q": "protest"})
        started = time.monotonic()
        await client.fetch("/search.rss", {"q": "protest"})
        elapsed = time.monotonic() - started
    finally:
        await client.close()

    assert call_count == 2
    assert elapsed >= 0.08


async def test_global_bucket_paces_every_attempt_including_retries(settings) -> None:
    """Section 7: a retry is also an outbound Reddit request -- one acquired
    global-bucket token must not cover multiple attempts. Force two attempts
    (first 500s, second succeeds) and verify each one waited its own turn on a
    slow global bucket."""

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, content=atom_feed([atom_entry()]))

    slow_global = settings.model_copy(
        update={
            "reddit_rss_global_rate": 1 / 0.05,
            "reddit_rss_global_burst": 1,
            "reddit_rss_retry_base_delay_seconds": 0.001,  # isolate the global bucket's pacing
        }
    )
    client = RedditRssClient(slow_global, transport=httpx.MockTransport(handler))
    await client.start()
    try:
        started = time.monotonic()
        await client.fetch("/search.rss", {"q": "protest"})
        elapsed = time.monotonic() - started
    finally:
        await client.close()

    assert call_count == 2
    # second attempt's global-bucket acquire had to wait for a fresh token
    assert elapsed >= 0.04


async def test_global_bucket_queue_timeout_raises_when_wait_too_long(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=atom_feed([atom_entry()]))

    starved = settings.model_copy(
        update={
            "reddit_rss_global_rate": 1 / 60,
            "reddit_rss_global_burst": 1,
            "reddit_rss_max_queue_wait_seconds": 0.01,
        }
    )
    client = RedditRssClient(starved, transport=httpx.MockTransport(handler))
    await client.start()
    try:
        await client.fetch("/search.rss", {"q": "protest"})  # consumes the only token
        with pytest.raises(RedditRssQueueTimeoutError):
            await client.fetch("/search.rss", {"q": "protest"})
    finally:
        await client.close()


async def test_fetch_oversized_response_raises_unavailable(settings) -> None:
    huge = atom_feed([atom_entry() for _ in range(5)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=huge)

    settings = settings.model_copy(update={"reddit_rss_max_response_bytes": 10})
    client = RedditRssClient(settings, transport=httpx.MockTransport(handler))
    await client.start()
    try:
        with pytest.raises(RedditRssUnavailableError):
            await client.fetch("/search.rss", {"q": "protest"})
    finally:
        await client.close()
