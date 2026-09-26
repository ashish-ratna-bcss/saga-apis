from __future__ import annotations

import pytest

from reddit_app.core.exceptions import (
    RedditRssInvalidQueryError,
    RedditRssInvalidSubredditError,
    RedditRssInvalidUsernameError,
)
from reddit_app.reddit.rss_urls import (
    build_listing_url,
    build_search_url,
    build_user_url,
    normalize_rss_subreddits,
    normalize_rss_username,
)


def test_normalize_rss_subreddits_none() -> None:
    assert normalize_rss_subreddits(None) == (None, [])


def test_normalize_rss_subreddits_empty_string() -> None:
    assert normalize_rss_subreddits("  ") == (None, [])


def test_normalize_rss_subreddits_single_string() -> None:
    assert normalize_rss_subreddits("india") == ("india", ["india"])


def test_normalize_rss_subreddits_combined_string() -> None:
    assert normalize_rss_subreddits("India+worldnews+politics") == (
        "India+worldnews+politics",
        ["India", "worldnews", "politics"],
    )


def test_normalize_rss_subreddits_list() -> None:
    assert normalize_rss_subreddits(["India", "worldnews"]) == (
        "India+worldnews",
        ["India", "worldnews"],
    )


def test_normalize_rss_subreddits_rejects_invalid_name() -> None:
    with pytest.raises(RedditRssInvalidSubredditError):
        normalize_rss_subreddits("has a space")


def test_normalize_rss_subreddits_rejects_too_many() -> None:
    with pytest.raises(RedditRssInvalidSubredditError):
        normalize_rss_subreddits([f"sub{i}" for i in range(30)])


def test_build_search_url_global() -> None:
    path, params = build_search_url(
        query="protest", subreddits=None, sort="new", time_range="day", limit=25
    )
    assert path == "/search.rss"
    assert params == {"q": "protest", "sort": "new", "t": "day", "limit": "25"}
    assert "restrict_sr" not in params


def test_build_search_url_single_subreddit() -> None:
    path, _ = build_search_url(
        query="protest", subreddits="india", sort="new", time_range="day", limit=25
    )
    assert path == "/r/india/search.rss"


def test_build_search_url_multi_subreddit_plus_stays_literal() -> None:
    """ "+" is Reddit's multireddit separator -- it must survive in the path, not
    become %2B."""

    path, params = build_search_url(
        query="protest",
        subreddits="India+worldnews+politics",
        sort="new",
        time_range="day",
        limit=50,
    )
    assert path == "/r/India+worldnews+politics/search.rss"
    assert "%2B" not in path
    assert params["restrict_sr"] == "1"


def test_build_search_url_rejects_empty_query() -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        build_search_url(query="", subreddits=None, sort="new", time_range="day", limit=25)


def test_build_search_url_rejects_bad_sort() -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        build_search_url(query="protest", subreddits=None, sort="bogus", time_range="day", limit=25)


def test_build_search_url_rejects_bad_time_range() -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        build_search_url(query="protest", subreddits=None, sort="new", time_range="bogus", limit=25)


def test_build_search_url_clamps_limit() -> None:
    _, params = build_search_url(
        query="protest", subreddits=None, sort="new", time_range="day", limit=99999
    )
    assert params["limit"] == "100"


def test_build_listing_url() -> None:
    path, params = build_listing_url(subreddits="worldnews+politics+India", sort="new", limit=50)
    assert path == "/r/worldnews+politics+India/new.rss"
    assert "%2B" not in path
    assert params == {"limit": "50"}


def test_build_listing_url_rejects_bad_sort() -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        build_listing_url(subreddits="india", sort="relevance", limit=25)


def test_normalize_rss_username_accepts_bare_and_prefixed() -> None:
    assert normalize_rss_username("someuser") == "someuser"
    assert normalize_rss_username("u/someuser") == "someuser"
    assert normalize_rss_username("/u/someuser/") == "someuser"


def test_normalize_rss_username_rejects_invalid() -> None:
    with pytest.raises(RedditRssInvalidUsernameError):
        normalize_rss_username("has a space")


def test_build_user_url_overview() -> None:
    path, params = build_user_url(
        username="spez", kind="overview", sort="new", time_range="all", limit=25
    )
    assert path == "/user/spez/.rss"
    assert params == {"sort": "new", "t": "all", "limit": "25"}


def test_build_user_url_submitted() -> None:
    path, _ = build_user_url(
        username="spez", kind="submitted", sort="top", time_range="year", limit=10
    )
    assert path == "/user/spez/submitted.rss"


def test_build_user_url_comments() -> None:
    path, _ = build_user_url(
        username="spez", kind="comments", sort="new", time_range="all", limit=10
    )
    assert path == "/user/spez/comments.rss"


def test_build_user_url_rejects_bad_kind() -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        build_user_url(username="spez", kind="bogus", sort="new", time_range="all", limit=25)


def test_build_user_url_rejects_bad_sort() -> None:
    with pytest.raises(RedditRssInvalidQueryError):
        build_user_url(
            username="spez", kind="overview", sort="relevance", time_range="all", limit=25
        )


def test_build_user_url_rejects_empty_username() -> None:
    with pytest.raises(RedditRssInvalidUsernameError):
        build_user_url(username="", kind="overview", sort="new", time_range="all", limit=25)


def test_build_user_url_clamps_limit() -> None:
    _, params = build_user_url(
        username="spez", kind="overview", sort="new", time_range="all", limit=99999
    )
    assert params["limit"] == "100"
