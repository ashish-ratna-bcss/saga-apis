from __future__ import annotations


def test_search_posts(app_client) -> None:
    response = app_client.get("/api/reddit/search/posts", params={"q": "Example"})
    assert response.status_code == 200
    body = response.json()
    assert any(p["id"] == "abc123" for p in body["items"])


def test_search_posts_requires_q(app_client) -> None:
    response = app_client.get("/api/reddit/search/posts")
    assert response.status_code == 422


def test_search_posts_scoped_to_subreddit(app_client) -> None:
    response = app_client.get(
        "/api/reddit/search/posts", params={"q": "Example", "subreddit": "india"}
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_search_posts_scoped_to_combined_subreddits(app_client) -> None:
    """"+"-joined subreddits scope keyword/event monitoring to several subs at once."""

    response = app_client.get(
        "/api/reddit/search/posts",
        params={"q": "Example", "subreddit": "india+cybersecurity"},
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["id"] == "abc123"


def test_search_posts_keywords_are_ored_together(app_client) -> None:
    """The ``keywords`` convenience matches the pasted RSS strategy's OR syntax."""

    response = app_client.get(
        "/api/reddit/search/posts",
        params=[("keywords", "ransomware"), ("keywords", "does-not-exist-anywhere")],
    )
    assert response.status_code == 200
    ids = {post["id"] for post in response.json()["items"]}
    assert ids == {"def456"}


def test_search_posts_requires_q_or_keywords(app_client) -> None:
    response = app_client.get("/api/reddit/search/posts", params={"keywords": [""]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_posts_scoped_to_missing_subreddit(app_client) -> None:
    response = app_client.get(
        "/api/reddit/search/posts", params={"q": "Example", "subreddit": "doesnotexist"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SUBREDDIT_NOT_FOUND"


def test_search_subreddits(app_client) -> None:
    response = app_client.get("/api/reddit/search/subreddits", params={"q": "cyber"})
    assert response.status_code == 200
    body = response.json()
    assert any(s["name"] == "cybersecurity" for s in body["items"])


def test_unified_search_posts(app_client) -> None:
    response = app_client.get("/api/reddit/search", params={"q": "Example", "type": "posts"})
    assert response.status_code == 200
    assert any(p["id"] == "abc123" for p in response.json()["items"])


def test_unified_search_subreddits(app_client) -> None:
    response = app_client.get("/api/reddit/search", params={"q": "cyber", "type": "subreddits"})
    assert response.status_code == 200
    assert any(s["name"] == "cybersecurity" for s in response.json()["items"])


def test_unified_search_comments_is_unsupported_not_fabricated(app_client) -> None:
    response = app_client.get("/api/reddit/search", params={"q": "anything", "type": "comments"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "UNSUPPORTED_SEARCH"
