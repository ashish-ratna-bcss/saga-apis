from __future__ import annotations


def test_post_details_by_id(app_client) -> None:
    response = app_client.post("/api/reddit/post", json={"post_id": "abc123"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "abc123"
    assert body["subreddit"] == "india"
    assert body["title"] == "Example title"
    assert body["text"] == "Post body"
    assert body["score"] == 123
    assert body["upvote_ratio"] == 0.95
    assert body["num_comments"] == 2


def test_post_details_by_url(app_client) -> None:
    response = app_client.post(
        "/api/reddit/post",
        json={"url": "https://www.reddit.com/r/india/comments/abc123/example/"},
    )
    assert response.status_code == 200
    assert response.json()["id"] == "abc123"


def test_post_details_requires_one_field(app_client) -> None:
    response = app_client.post("/api/reddit/post", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_post_details_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/post", json={"post_id": "missing"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "POST_NOT_FOUND"


def test_post_comments_preserve_parent_id_for_tree_reconstruction(app_client) -> None:
    response = app_client.post("/api/reddit/post/comments", json={"post_id": "abc123", "limit": 100})
    assert response.status_code == 200
    body = response.json()
    items = {c["id"]: c for c in body["items"]}
    assert items["comment123"]["parent_id"] is None
    assert items["comment123"]["post_id"] == "abc123"
    assert items["comment456"]["parent_id"] == "comment123"
    assert items["comment456"]["post_id"] == "abc123"


def test_post_comments_pagination_offset(app_client) -> None:
    first = app_client.post("/api/reddit/post/comments", json={"post_id": "abc123", "limit": 1})
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["cursor"] is not None

    second = app_client.post(
        "/api/reddit/post/comments", json={"post_id": "abc123", "limit": 1, "cursor": first_body["cursor"]}
    )
    second_body = second.json()
    assert len(second_body["items"]) == 1
    assert second_body["items"][0]["id"] != first_body["items"][0]["id"]


def test_post_comments_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/post/comments", json={"post_id": "missing"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "POST_NOT_FOUND"
