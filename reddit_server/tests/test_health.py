from __future__ import annotations

from reddit_app.core.config import Settings


def test_health_never_requires_configuration(app_client) -> None:
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_configured(app_client) -> None:
    response = app_client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["reddit_configured"] is True


def test_ready_reports_not_configured(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    unconfigured = Settings(_env_file=None, reddit_client_id="", reddit_client_secret="", reddit_user_agent="")
    app = create_app(
        unconfigured, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["reddit_configured"] is False


def test_health_and_ready_never_require_api_key(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    protected = Settings(
        _env_file=None,
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_user_agent="ua",
        api_keys="secret-key",
    )
    app = create_app(
        protected, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
