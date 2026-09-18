from __future__ import annotations

import pytest

from reddit_app.core.exceptions import RedditRssInvalidQueryError, RedditRssInvalidSubredditError
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
