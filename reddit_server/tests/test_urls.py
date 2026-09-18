from __future__ import annotations

import pytest

from reddit_app.core.exceptions import InvalidRedditUrlError, ValidationError
from reddit_app.reddit.urls import (
    classify_reddit_url,
    normalize_subreddit_name,
    normalize_subreddit_names,
    normalize_username,
)


@pytest.mark.parametrize("raw,expected", [("india", "india"), ("r/india", "india"), ("/r/india/", "india"), ("R/India", "India")])
def test_normalize_subreddit_name(raw: str, expected: str) -> None:
    assert normalize_subreddit_name(raw) == expected


def test_normalize_subreddit_name_rejects_invalid() -> None:
    with pytest.raises(ValidationError):
        normalize_subreddit_name("has a space")


def test_normalize_subreddit_names_single() -> None:
    assert normalize_subreddit_names("r/india") == "india"


def test_normalize_subreddit_names_combined() -> None:
    assert normalize_subreddit_names("india+r/worldnews+POLITICS") == "india+worldnews+POLITICS"


def test_normalize_subreddit_names_rejects_empty_segment() -> None:
    with pytest.raises(ValidationError):
        normalize_subreddit_names("india++worldnews")


def test_normalize_subreddit_names_rejects_invalid_segment() -> None:
    with pytest.raises(ValidationError):
        normalize_subreddit_names("india+has a space")


@pytest.mark.parametrize("raw,expected", [("someuser", "someuser"), ("u/someuser", "someuser"), ("/u/someuser/", "someuser")])
def test_normalize_username(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


def test_classify_subreddit_url() -> None:
    ref = classify_reddit_url("https://www.reddit.com/r/india/")
    assert ref.kind == "subreddit"
    assert ref.subreddit == "india"


def test_classify_post_url() -> None:
    ref = classify_reddit_url("https://www.reddit.com/r/india/comments/abc123/some_title/")
    assert ref.kind == "post"
    assert ref.subreddit == "india"
    assert ref.post_id == "abc123"


def test_classify_post_url_without_scheme() -> None:
    ref = classify_reddit_url("old.reddit.com/r/india/comments/abc123/some_title/")
    assert ref.kind == "post"
    assert ref.post_id == "abc123"


def test_classify_comment_url() -> None:
    ref = classify_reddit_url(
        "https://www.reddit.com/r/india/comments/abc123/some_title/comment456/"
    )
    assert ref.kind == "comment"
    assert ref.subreddit == "india"
    assert ref.post_id == "abc123"
    assert ref.comment_id == "comment456"


def test_classify_shortlink() -> None:
    ref = classify_reddit_url("https://redd.it/abc123")
    assert ref.kind == "post"
    assert ref.post_id == "abc123"


def test_classify_rejects_non_reddit_host() -> None:
    with pytest.raises(InvalidRedditUrlError):
        classify_reddit_url("https://example.com/r/india/")


def test_classify_rejects_empty() -> None:
    with pytest.raises(InvalidRedditUrlError):
        classify_reddit_url("")
