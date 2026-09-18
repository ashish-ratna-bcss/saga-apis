from __future__ import annotations

from reddit_app.reddit.normalize import (
    normalize_author,
    normalize_comment,
    normalize_post,
    normalize_subreddit,
    normalize_user,
    to_iso8601,
)


def test_to_iso8601() -> None:
    assert to_iso8601(1_756_000_000) == "2025-08-24T01:46:40Z"
    assert to_iso8601(None) is None


def test_normalize_author_deleted() -> None:
    assert normalize_author({"author": "[deleted]"}) == {"id": None, "username": "[deleted]"}


def test_normalize_author_missing() -> None:
    assert normalize_author({}) is None


def test_normalize_post_prefers_permalink_over_external_url() -> None:
    raw = {
        "id": "abc123",
        "subreddit": "india",
        "title": "T",
        "selftext": "",
        "author": "user1",
        "author_fullname": "t2_1",
        "created_utc": 1_756_000_000,
        "permalink": "/r/india/comments/abc123/t/",
        "url": "https://external.example.com/article",
        "score": 5,
        "upvote_ratio": 0.8,
        "num_comments": 1,
    }
    normalized = normalize_post(raw)
    assert normalized["url"] == "https://www.reddit.com/r/india/comments/abc123/t/"


def test_normalize_comment_top_level_has_no_parent() -> None:
    raw = {
        "id": "c1",
        "link_id": "t3_abc123",
        "parent_id": "t3_abc123",
        "body": "hi",
        "author": "user2",
        "author_fullname": "t2_2",
        "created_utc": 1_756_000_000,
        "score": 1,
        "permalink": "/r/india/comments/abc123/t/c1/",
    }
    normalized = normalize_comment(raw)
    assert normalized["parent_id"] is None
    assert normalized["post_id"] == "abc123"


def test_normalize_comment_reply_has_parent() -> None:
    raw = {
        "id": "c2",
        "link_id": "t3_abc123",
        "parent_id": "t1_c1",
        "body": "reply",
        "author": "user3",
        "created_utc": 1_756_000_000,
        "score": 1,
    }
    normalized = normalize_comment(raw)
    assert normalized["parent_id"] == "c1"
    assert normalized["post_id"] == "abc123"


def test_normalize_subreddit() -> None:
    raw = {
        "name": "t5_2qh1e",
        "display_name": "india",
        "title": "India",
        "public_description": "desc",
        "subscribers": 100,
        "url": "/r/india/",
        "subreddit_type": "public",
        "over18": False,
    }
    normalized = normalize_subreddit(raw)
    assert normalized == {
        "id": "t5_2qh1e",
        "name": "india",
        "display_name": "r/india",
        "title": "India",
        "description": "desc",
        "subscribers": 100,
        "url": "https://www.reddit.com/r/india/",
        "public": True,
        "over18": False,
    }


def test_normalize_user() -> None:
    raw = {"id": "8xwlg", "name": "someuser", "created_utc": 1_577_836_800, "link_karma": 1, "comment_karma": 2, "is_mod": True}
    normalized = normalize_user(raw)
    assert normalized["id"] == "t2_8xwlg"
    assert normalized["username"] == "someuser"
    assert normalized["is_mod"] is True
