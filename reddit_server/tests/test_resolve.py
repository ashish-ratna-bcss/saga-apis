from __future__ import annotations


def test_resolve_post_url(app_client) -> None:
    response = app_client.post(
        "/api/reddit/resolve",
        json={"url": "https://www.reddit.com/r/india/comments/abc123/example/"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "post"
    assert body["post"]["id"] == "abc123"
    assert body["subreddit"]["name"] == "india"
    assert body["comment"] is None


def test_resolve_comment_url(app_client) -> None:
    response = app_client.post(
        "/api/reddit/resolve",
        json={
            "url": "https://www.reddit.com/r/india/comments/abc123/example/comment123/"
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "comment"
    assert body["comment"]["id"] == "comment123"
    assert body["post"]["id"] == "abc123"
    assert body["subreddit"]["name"] == "india"


def test_resolve_subreddit_url(app_client) -> None:
    response = app_client.post("/api/reddit/resolve", json={"url": "https://www.reddit.com/r/india/"})
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "subreddit"
    assert body["subreddit"]["name"] == "india"


def test_resolve_shortlink(app_client) -> None:
    response = app_client.post("/api/reddit/resolve", json={"url": "https://redd.it/abc123"})
    assert response.status_code == 200
    assert response.json()["kind"] == "post"


def test_resolve_invalid_url(app_client) -> None:
    response = app_client.post("/api/reddit/resolve", json={"url": "https://example.com/not-reddit"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REDDIT_URL"
