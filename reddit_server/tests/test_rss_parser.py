from __future__ import annotations

import pytest

from reddit_app.core.exceptions import RedditRssParseError
from reddit_app.reddit.rss_parser import parse_feed
from tests.fake_reddit_rss import atom_entry, atom_feed


def test_parse_feed_single_post() -> None:
    posts = parse_feed(atom_feed([atom_entry()]))
    assert len(posts) == 1
    post = posts[0]
    assert post["id"] == "abc123"
    assert post["guid"] == "t3_abc123"
    assert post["title"] == "Example title"
    assert post["author"] == "someuser"
    assert post["subreddit"] == "india"
    assert post["url"] == "https://www.reddit.com/r/india/comments/abc123/example/"
    assert post["published_at"] == "2026-09-10T08:20:00Z"
    assert post["updated_at"] == "2026-09-10T08:20:00Z"
    assert post["source"] == "reddit"
    assert post["source_type"] == "rss"
    assert post["flair"] is None
    assert post["matched_keywords"] == []
    assert "submitted by" in (post["content"] or "")


def test_parse_feed_multiple_posts() -> None:
    posts = parse_feed(
        atom_feed(
            [
                atom_entry(post_id="abc123", subreddit="india"),
                atom_entry(post_id="def456", subreddit="worldnews", title="Second title"),
            ]
        )
    )
    assert [p["id"] for p in posts] == ["abc123", "def456"]
    assert [p["subreddit"] for p in posts] == ["india", "worldnews"]


def test_parse_feed_missing_author_is_null_not_a_crash() -> None:
    posts = parse_feed(atom_feed([atom_entry(author=None)]))
    assert len(posts) == 1
    assert posts[0]["author"] is None


def test_parse_feed_missing_description_is_null() -> None:
    posts = parse_feed(atom_feed([atom_entry(content="")]))
    assert posts[0]["content"] is None


def test_parse_feed_malformed_timestamp_is_null_not_a_crash() -> None:
    posts = parse_feed(
        atom_feed([atom_entry(published="not-a-real-timestamp", updated="also-not-one")])
    )
    assert posts[0]["published_at"] is None
    assert posts[0]["updated_at"] is None


def test_parse_feed_duplicate_guid_both_entries_still_parse() -> None:
    """Parsing never deduplicates -- that's the service layer's job (see
    reddit_rss_service._dedupe) -- so both entries must still come back."""

    posts = parse_feed(
        atom_feed(
            [
                atom_entry(post_id="abc123", guid="t3_abc123"),
                atom_entry(post_id="abc123", guid="t3_abc123"),
            ]
        )
    )
    assert len(posts) == 2
    assert posts[0]["id"] == posts[1]["id"] == "abc123"


def test_parse_feed_entry_missing_link_and_category_still_parses() -> None:
    posts = parse_feed(atom_feed([atom_entry(link="", include_category=False)]))
    assert len(posts) == 1
    assert posts[0]["url"] is None
    # falls back to the fullname guid since the link couldn't be classified
    assert posts[0]["id"] == "abc123"
    assert posts[0]["subreddit"] is None


def test_parse_feed_empty_feed_is_valid_empty_result() -> None:
    assert parse_feed(atom_feed([])) == []


def test_parse_feed_empty_bytes_is_valid_empty_result() -> None:
    assert parse_feed(b"") == []


def test_parse_feed_malformed_xml_raises_parse_error() -> None:
    with pytest.raises(RedditRssParseError):
        parse_feed(b"<feed><entry><title>unclosed")


def test_parse_feed_non_xml_raises_parse_error() -> None:
    with pytest.raises(RedditRssParseError):
        parse_feed(b"this is not xml at all")
