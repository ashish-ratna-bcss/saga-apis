from __future__ import annotations


def test_comment_details(app_client) -> None:
    response = app_client.post("/api/reddit/comment", json={"comment_id": "comment123"})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "comment123"
    assert body["post_id"] == "abc123"
    assert body["parent_id"] is None
    assert body["text"] == "Comment text"
    assert body["author"] == {"id": "t2_user2", "username": "user2"}


def test_comment_details_reply_has_parent(app_client) -> None:
    response = app_client.post("/api/reddit/comment", json={"comment_id": "comment456"})
    body = response.json()
    assert body["parent_id"] == "comment123"


def test_comment_details_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/comment", json={"comment_id": "missing"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "COMMENT_NOT_FOUND"
