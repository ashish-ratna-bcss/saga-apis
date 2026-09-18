from __future__ import annotations


def test_user_info(app_client) -> None:
    response = app_client.post("/api/reddit/user", json={"username": "someuser"})
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "someuser"
    assert body["id"] == "t2_8xwlg"
    assert body["link_karma"] == 1234
    assert body["comment_karma"] == 5678
    assert body["is_mod"] is False


def test_user_info_accepts_u_prefix(app_client) -> None:
    response = app_client.post("/api/reddit/user", json={"username": "u/someuser"})
    assert response.status_code == 200
    assert response.json()["username"] == "someuser"


def test_user_info_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/user", json={"username": "nosuchuser"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


def test_user_posts(app_client) -> None:
    response = app_client.post("/api/reddit/user/posts", json={"username": "user1"})
    assert response.status_code == 200
    body = response.json()
    assert any(p["id"] == "abc123" for p in body["items"])


def test_user_comments(app_client) -> None:
    response = app_client.post("/api/reddit/user/comments", json={"username": "user2"})
    assert response.status_code == 200
    body = response.json()
    assert any(c["id"] == "comment123" for c in body["items"])


def test_user_posts_not_found(app_client) -> None:
    response = app_client.post("/api/reddit/user/posts", json={"username": "nosuchuser"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"
