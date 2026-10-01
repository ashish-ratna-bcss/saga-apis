from __future__ import annotations

import asyncio

import httpx
import pytest

from reddit_app.core.exceptions import (
    RedditRssInvalidQueryError,
    RedditRssInvalidSubredditError,
    RedditRssInvalidUsernameError,
)
from reddit_app.services.reddit_rss_service import (
    DEFAULT_EVENT_KEYWORDS,
    DEFAULT_EVENT_SUBREDDITS,
    RedditRssService,
)
from tests.fake_reddit_rss import FakeRedditRss, atom_entry, atom_feed


async def test_monitor_with_query(rss_service: RedditRssService) -> None:
    result = await rss_service.monitor(query="protest")
    assert result["query"] == "protest"
    assert result["source"] == "reddit"
    assert result["transport"] == "rss"
    assert result["authenticated"] is False
    assert result["count"] == 1
    assert result["posts"][0]["id"] == "abc123"


async def test_monitor_keywords_are_ored_together(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    result = await rss_service.monitor(keywords=["protest", "strike", "curfew"])
    assert result["query"] == "protest OR strike OR curfew"
    _, query = fake_reddit_rss.calls[-1]
    assert query["q"] == "protest OR strike OR curfew"


async def test_monitor_query_wins_over_keywords_when_both_given(
    rss_service: RedditRssService,
) -> None:
    result = await rss_service.monitor(query="explicit query", keywords=["ignored"])
    assert result["query"] == "explicit query"


async def test_monitor_requires_query_or_keywords(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.monitor()


async def test_monitor_rejects_blank_keywords_only(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.monitor(keywords=["", "   "])


async def test_monitor_matches_keywords_against_result_posts(rss_service: RedditRssService) -> None:
    result = await rss_service.monitor(keywords=["example", "does-not-appear"])
    assert result["posts"][0]["matched_keywords"] == ["example"]


async def test_monitor_matched_keywords_empty_when_only_query_given(
    rss_service: RedditRssService,
) -> None:
    result = await rss_service.monitor(query="protest")
    assert result["posts"][0]["matched_keywords"] == []


async def test_monitor_multi_subreddit_string(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    result = await rss_service.monitor(query="protest", subreddits="India+worldnews")
    assert result["subreddits"] == ["India", "worldnews"]
    path, _ = fake_reddit_rss.calls[-1]
    assert path == "/r/India+worldnews/search.rss"


async def test_monitor_multi_subreddit_list(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    result = await rss_service.monitor(query="protest", subreddits=["India", "worldnews"])
    assert result["subreddits"] == ["India", "worldnews"]


async def test_monitor_invalid_subreddit_raises(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidSubredditError):
        await rss_service.monitor(query="protest", subreddits="has a space")


async def test_monitor_dedupes_within_response(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(post_id="abc123"), atom_entry(post_id="abc123")]
    )
    result = await rss_service.monitor(query="protest")
    assert result["count"] == 1


async def test_monitor_subreddit_without_query_uses_listing(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    result = await rss_service.monitor(subreddits=["Odisha"], sort="new", limit=5)
    assert result["count"] >= 1
    calls = fake_reddit_rss.calls_to("/new.rss")
    assert calls
    assert calls[0][0].endswith("/r/Odisha/new.rss")


async def test_monitor_date_filter_excludes_posts_outside_range(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [
            atom_entry(post_id="early", published="2026-07-01T10:00:00+00:00"),
            atom_entry(post_id="inrange", published="2026-08-15T10:00:00+00:00"),
            atom_entry(post_id="late", published="2026-09-01T10:00:00+00:00"),
        ]
    )
    result = await rss_service.monitor(
        query="protest", from_date="2026-08-01", to_date="2026-08-31"
    )
    assert result["count"] == 1
    assert result["posts"][0]["id"] == "inrange"
    assert result["from_date"] == "2026-08-01"
    assert result["to_date"] == "2026-08-31"


async def test_monitor_to_date_bare_date_includes_whole_day(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    """A bare to_date="2026-08-31" must include posts published *during* that
    day, not just its first instant."""

    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(post_id="end_of_day", published="2026-08-31T23:59:00+00:00")]
    )
    result = await rss_service.monitor(query="protest", to_date="2026-08-31")
    assert result["count"] == 1


async def test_monitor_date_filter_excludes_posts_with_unparseable_date(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    """Can't verify it's in range -> excluded, never assumed in-range."""

    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(post_id="bad_date", published="not-a-real-timestamp", updated="also-bad")]
    )
    result = await rss_service.monitor(query="protest", from_date="2026-01-01")
    assert result["count"] == 0


async def test_monitor_no_date_filter_keeps_everything(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(post_id="bad_date", published="not-a-real-timestamp", updated="also-bad")]
    )
    result = await rss_service.monitor(query="protest")
    assert result["count"] == 1


async def test_monitor_rejects_from_date_after_to_date(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.monitor(query="protest", from_date="2026-08-31", to_date="2026-08-01")


async def test_monitor_rejects_invalid_date_format(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.monitor(query="protest", from_date="not-a-date")


async def test_monitor_shares_cached_feed_across_calls_with_identical_params(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    await rss_service.monitor(query="protest", subreddits="india")
    await rss_service.monitor(query="protest", subreddits="india")
    assert len(fake_reddit_rss.calls_to("/search.rss")) == 1


async def test_monitor_does_not_share_cache_across_different_feeds(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    await rss_service.monitor(query="protest", subreddits="india")
    await rss_service.monitor(query="protest", subreddits="hyderabad")
    assert len(fake_reddit_rss.calls_to("/search.rss")) == 2


async def test_monitor_strong_keywords_signal_ignores_min_matches(
    rss_service: RedditRssService,
) -> None:
    result = await rss_service.monitor(query="x", strong_keywords=["example"], min_matches=99)
    assert result["posts"][0]["signal"] is True
    assert "example" in result["posts"][0]["matched_keywords"]


async def test_monitor_min_matches_gates_signal(rss_service: RedditRssService) -> None:
    result = await rss_service.monitor(query="x", keywords=["example"], min_matches=2)
    assert result["posts"][0]["matched_keywords"] == ["example"]
    assert result["posts"][0]["signal"] is False


async def test_monitor_exclude_drops_matching_posts(rss_service: RedditRssService) -> None:
    result = await rss_service.monitor(query="x", exclude=["example"])
    assert result["count"] == 0


async def test_monitor_match_field_title_scopes_matching(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(title="nothing here", content="protest in the streets")]
    )
    title_only = await rss_service.monitor(query="x", keywords=["protest"], match_field="title")
    full_text = await rss_service.monitor(query="x", keywords=["protest"], match_field="full")
    assert title_only["posts"][0]["matched_keywords"] == []
    assert full_text["posts"][0]["matched_keywords"] == ["protest"]


async def test_monitor_rejects_bad_match_field(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.monitor(query="x", match_field="bogus")


async def test_monitor_rejects_min_matches_below_one(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.monitor(query="x", min_matches=0)


async def test_monitor_two_callers_different_keywords_do_not_contaminate_each_other(
    rss_service: RedditRssService,
) -> None:
    a = await rss_service.monitor(query="x", keywords=["example"])
    b = await rss_service.monitor(query="x", keywords=["does-not-exist"])
    assert a["posts"][0]["matched_keywords"] == ["example"]
    assert b["posts"][0]["matched_keywords"] == []


async def test_event_uses_predefined_strategy_by_default(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    result = await rss_service.event()
    assert result["subreddits"] == DEFAULT_EVENT_SUBREDDITS
    assert result["query"] == " OR ".join(DEFAULT_EVENT_KEYWORDS)
    path, query = fake_reddit_rss.calls[-1]
    assert path.startswith("/r/")
    assert "restrict_sr" in query


async def test_event_overrides_defaults(rss_service: RedditRssService) -> None:
    result = await rss_service.event(subreddits=["india"], keywords=["example"])
    assert result["subreddits"] == ["india"]
    assert result["query"] == "example"


async def test_event_signal_detected_when_over_threshold(rss_client, feed_cache) -> None:
    service = RedditRssService(rss_client, feed_cache, event_threshold=1)
    result = await service.event(subreddits=["india"], keywords=["example"])
    assert result["event_signal"]["detected"] is True
    assert result["event_signal"]["post_count"] == 1
    assert result["event_signal"]["threshold"] == 1
    assert result["event_signal"]["unique_subreddits"] == 1
    assert result["event_signal"]["matched_keywords"] == ["example"]


async def test_event_signal_not_detected_under_threshold(rss_client, feed_cache) -> None:
    service = RedditRssService(rss_client, feed_cache, event_threshold=1000)
    result = await service.event(subreddits=["india"], keywords=["example"])
    assert result["event_signal"]["detected"] is False


async def test_event_signal_never_claims_a_confirmed_event(rss_service: RedditRssService) -> None:
    result = await rss_service.event(subreddits=["india"], keywords=["example"])
    assert set(result["event_signal"].keys()) == {
        "detected",
        "post_count",
        "unique_subreddits",
        "matched_keywords",
        "threshold",
    }


async def test_user_overview_default(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    result = await rss_service.user(username="spez")
    assert result["source"] == "reddit"
    assert result["transport"] == "rss"
    assert result["authenticated"] is False
    assert result["username"] == "spez"
    assert result["kind"] == "overview"
    assert result["count"] == 1
    path, _ = fake_reddit_rss.calls[-1]
    assert path == "/user/spez/.rss"


async def test_user_accepts_u_prefixed_username(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    await rss_service.user(username="u/spez")
    path, _ = fake_reddit_rss.calls[-1]
    assert path == "/user/spez/.rss"


async def test_user_submitted_kind_builds_submitted_path(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    await rss_service.user(username="spez", kind="submitted")
    path, _ = fake_reddit_rss.calls[-1]
    assert path == "/user/spez/submitted.rss"


async def test_user_rejects_invalid_username(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidUsernameError):
        await rss_service.user(username="has a space")


async def test_user_rejects_bad_kind(rss_service: RedditRssService) -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        await rss_service.user(username="spez", kind="bogus")


async def test_user_keywords_annotate_without_narrowing_the_feed(
    rss_service: RedditRssService,
) -> None:
    """Unlike monitor(), keywords on user() never become the Reddit query --
    the username alone already selects the feed; keywords only annotate."""

    result = await rss_service.user(username="spez", keywords=["example"])
    assert result["count"] == 1
    assert result["posts"][0]["matched_keywords"] == ["example"]

    no_keywords = await rss_service.user(username="spez")
    assert no_keywords["count"] == 1
    assert no_keywords["posts"][0]["matched_keywords"] == []


async def test_user_exclude_drops_matching_posts(rss_service: RedditRssService) -> None:
    result = await rss_service.user(username="spez", exclude=["example"])
    assert result["count"] == 0


async def test_user_strong_keywords_force_signal(rss_service: RedditRssService) -> None:
    result = await rss_service.user(username="spez", strong_keywords=["example"], min_matches=99)
    assert result["posts"][0]["signal"] is True


async def test_user_date_filter_excludes_posts_outside_range(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [
            atom_entry(post_id="early", published="2026-07-01T10:00:00+00:00"),
            atom_entry(post_id="inrange", published="2026-08-15T10:00:00+00:00"),
        ]
    )
    result = await rss_service.user(username="spez", from_date="2026-08-01", to_date="2026-08-31")
    assert result["count"] == 1
    assert result["posts"][0]["id"] == "inrange"


async def test_user_fetches_the_latest_feed_on_every_hit(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    first = atom_feed([atom_entry(post_id="older", published="2026-09-01T00:00:00+00:00")])
    second = atom_feed([atom_entry(post_id="newer", published="2026-09-02T00:00:00+00:00")])
    fake_reddit_rss.scripted["/user/spez/.rss"] = [
        httpx.Response(200, content=first),
        httpx.Response(200, content=second),
    ]
    first_result = await rss_service.user(username="spez")
    second_result = await rss_service.user(username="spez")
    assert first_result["posts"][0]["id"] == "older"
    assert second_result["posts"][0]["id"] == "newer"
    assert len(fake_reddit_rss.calls_to("/user/spez/.rss")) == 2


async def test_user_different_kinds_each_get_their_own_fetch(
    rss_service: RedditRssService, fake_reddit_rss: FakeRedditRss
) -> None:
    await rss_service.user(username="spez", kind="overview")
    await rss_service.user(username="spez", kind="submitted")
    assert len(fake_reddit_rss.calls_to("/user/spez/.rss")) == 1
    assert len(fake_reddit_rss.calls_to("/user/spez/submitted.rss")) == 1


async def test_monitor_splits_keywords_across_rss_accounts_and_merges() -> None:
    """Several keywords and several RSS accounts: one response, same query string."""

    import httpx

    from reddit_app.core.config import Settings
    from reddit_app.reddit.feed_cache import FeedCache
    from reddit_app.reddit.rss_client import RedditRssClient

    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["q"]
        user = request.url.params["user"]
        calls.append((query, user))
        post_id = "post-protest" if "protest" in query else "post-strike"
        return httpx.Response(200, content=atom_feed([atom_entry(post_id=post_id, title=query)]))

    configured = Settings(
        _env_file=None,
        environment="test",
        reddit_rss_accounts="alice:feedtokenalice,bob:feedtokenbob",
        reddit_rss_max_retries=0,
        log_level="WARNING",
    )
    client = RedditRssClient(configured, transport=httpx.MockTransport(handler))
    service = RedditRssService(
        client,
        FeedCache(ttl_seconds=60, max_entries=16),
        event_threshold=configured.reddit_rss_event_threshold,
    )
    await client.start()
    try:
        result = await service.monitor(keywords=["protest", "strike"])
    finally:
        await client.close()

    assert result["query"] == "protest OR strike"
    assert result["count"] == 2
    assert {post["id"] for post in result["posts"]} == {"post-protest", "post-strike"}
    assert {user for _, user in calls} == {"alice", "bob"}


async def test_concurrent_profile_hits_use_different_rss_accounts() -> None:
    from reddit_app.core.config import Settings
    from reddit_app.reddit.feed_cache import FeedCache
    from reddit_app.reddit.rss_client import RedditRssClient

    users: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        users.append(request.url.params["user"])
        return httpx.Response(
            200, content=atom_feed([atom_entry(post_id=request.url.params["user"])])
        )

    configured = Settings(
        _env_file=None,
        environment="test",
        reddit_rss_accounts="alice:feedtokenalice,bob:feedtokenbob",
        reddit_rss_max_retries=0,
        log_level="WARNING",
    )
    client = RedditRssClient(configured, transport=httpx.MockTransport(handler))
    service = RedditRssService(
        client,
        FeedCache(ttl_seconds=60, max_entries=16),
        event_threshold=configured.reddit_rss_event_threshold,
    )
    await client.start()
    try:
        first, second = await asyncio.gather(
            service.user(username="one"),
            service.user(username="two"),
        )
    finally:
        await client.close()

    assert {post["id"] for post in first["posts"] + second["posts"]} <= {"alice", "bob"}
    assert set(users) == {"alice", "bob"}
