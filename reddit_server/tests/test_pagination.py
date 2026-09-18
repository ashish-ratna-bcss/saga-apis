from __future__ import annotations

import pytest

from reddit_app.core.exceptions import ValidationError
from reddit_app.reddit.pagination import clamp_limit, decode_cursor, encode_cursor
from tests.fake_reddit import listing, post_thing


def test_clamp_limit_defaults() -> None:
    assert clamp_limit(None) == 25


def test_clamp_limit_bounds() -> None:
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1
    assert clamp_limit(1000) == 100


def test_cursor_roundtrip() -> None:
    cursor = encode_cursor({"after": "t3_xyz", "offset": 3})
    assert decode_cursor(cursor) == {"after": "t3_xyz", "offset": 3}


def test_decode_cursor_empty() -> None:
    assert decode_cursor(None) == {}
    assert decode_cursor("") == {}


def test_decode_cursor_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        decode_cursor("not-valid-base64!!")


def test_subreddit_posts_cursor_forwards_reddit_after_token(app_client, fake_reddit) -> None:
    """Reddit's own `after` token round-trips through our opaque cursor and is sent
    back as the `after` query param on the next call -- proving SOC Eye's cursor isn't
    silently dropping Reddit's native pagination state."""

    fake_reddit.scripted["/r/india/new"] = [
        _listing_response([post_thing("abc123")], after="t3_nextpage")
    ]

    first = app_client.post(
        "/api/reddit/subreddit/posts", json={"subreddit": "india", "sort": "new", "limit": 1}
    )
    assert first.status_code == 200
    cursor = first.json()["cursor"]
    assert cursor is not None

    fake_reddit.scripted["/r/india/new"] = [_listing_response([post_thing("abc123")], after=None)]
    second = app_client.post(
        "/api/reddit/subreddit/posts",
        json={"subreddit": "india", "sort": "new", "limit": 1, "cursor": cursor},
    )
    assert second.status_code == 200
    assert second.json()["cursor"] is None

    calls = fake_reddit.calls_to("/r/india/new")
    assert calls[-1][2].get("after") == "t3_nextpage"


def _listing_response(children, *, after):
    from tests.fake_reddit import _json

    return _json(200, listing(children, after=after))
