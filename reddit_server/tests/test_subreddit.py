from __future__ import annotations


def test_subreddit_info(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit", json={"name": "india"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "t5_india"
    assert body["name"] == "india"
    assert body["display_name"] == "r/india"
    assert body["subscribers"] == 1_000_000
    assert body["public"] is True
    assert body["over18"] is False


def test_subreddit_info_accepts_r_prefix(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit", json={"name": "r/india"})
    assert response.status_code == 200
    assert response.json()["name"] == "india"


def test_subreddit_info_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit", json={"name": "doesnotexist"})
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "SUBREDDIT_NOT_FOUND"


def test_subreddit_posts(app_client) -> None:
    response = app_client.post(
        "/api/reddit/subreddit/posts", json={"subreddit": "india", "sort": "new", "limit": 50}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cursor"] is None
    assert len(body["items"]) == 1
    post = body["items"][0]
    assert post["id"] == "abc123"
    assert post["subreddit"] == "india"
    assert post["author"] == {"id": "t2_user1", "username": "user1"}
    assert post["url"] == "https://www.reddit.com/r/india/comments/abc123/example/"


def test_subreddit_posts_combined_subreddits(app_client) -> None:
    """"+"-joined names poll several 'event subreddits' as one listing."""

    response = app_client.post(
        "/api/reddit/subreddit/posts",
        json={"subreddit": "india+cybersecurity", "sort": "new"},
    )
    assert response.status_code == 200
    body = response.json()
    assert {post["id"] for post in body["items"]} == {"abc123", "def456"}


def test_subreddit_posts_rejects_bad_sort(app_client) -> None:
    response = app_client.post(
        "/api/reddit/subreddit/posts", json={"subreddit": "india", "sort": "bogus"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_subreddit_posts_private_is_forbidden(app_client) -> None:
    response = app_client.post(
        "/api/reddit/subreddit/posts", json={"subreddit": "privatesub", "sort": "new"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SUBREDDIT_PRIVATE"


def test_subreddit_access_public(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit/access", json={"subreddit": "india"})
    assert response.status_code == 200
    assert response.json() == {"accessible": True, "state": "public", "detail": "readable"}


def test_subreddit_access_private(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit/access", json={"subreddit": "privatesub"})
    body = response.json()
    assert body["accessible"] is False
    assert body["state"] == "private"


def test_subreddit_access_restricted(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit/access", json={"subreddit": "restrictedsub"})
    body = response.json()
    assert body["accessible"] is True
    assert body["state"] == "restricted"


def test_subreddit_access_quarantined(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit/access", json={"subreddit": "quarantinedsub"})
    body = response.json()
    assert body["accessible"] is False
    assert body["state"] == "quarantined"


def test_subreddit_access_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit/access", json={"subreddit": "doesnotexist"})
    assert response.status_code == 200
    body = response.json()
    assert body["accessible"] is False
    assert body["state"] == "not_found"
