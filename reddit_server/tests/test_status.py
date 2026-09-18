from __future__ import annotations

from reddit_app.core.config import Settings


def test_status_connected_application_only(app_client) -> None:
    response = app_client.get("/api/reddit/status")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["authorized"] is True
    # client_credentials grant has no specific Reddit account.
    assert body["account"] is None


def test_status_connected_with_account(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    settings = Settings(
        _env_file=None,
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_username="analyst_bot",
        reddit_password="hunter2",
        reddit_user_agent="ua",
    )
    app = create_app(
        settings, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        response = client.get("/api/reddit/status")
    body = response.json()
    assert body["connected"] is True
    assert body["account"]["username"] != "analyst_bot"  # never the raw username
    assert body["account"]["username"].startswith("a")
    assert body["account"]["username"].endswith("t")


def test_status_not_configured(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    settings = Settings(_env_file=None, reddit_client_id="", reddit_client_secret="", reddit_user_agent="")
    app = create_app(
        settings, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        response = client.get("/api/reddit/status")
    body = response.json()
    assert body == {"connected": False, "authorized": False, "account": None}


def test_status_reports_disconnected_when_reddit_rejects_credentials(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    fake_reddit.reject_auth = True
    settings = Settings(_env_file=None, reddit_client_id="id", reddit_client_secret="bad", reddit_user_agent="ua")
    app = create_app(
        settings, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        response = client.get("/api/reddit/status")
    body = response.json()
    assert body["connected"] is False
    assert body["authorized"] is False


def test_status_never_exposes_secrets(app_client, settings: Settings) -> None:
    response = app_client.get("/api/reddit/status")
    text = response.text
    assert settings.client_secret not in text
