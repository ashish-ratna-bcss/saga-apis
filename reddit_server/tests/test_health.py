from __future__ import annotations

from fastapi.testclient import TestClient

from reddit_app.core.config import Settings
from reddit_app.main import create_app


def test_health_never_requires_configuration(app_client) -> None:
    response = app_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_ok(app_client) -> None:
    response = app_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_and_ready_never_require_api_key(fake_reddit_rss) -> None:
    settings = Settings(_env_file=None, api_keys="secret-key")
    app = create_app(settings, rss_transport=fake_reddit_rss.transport())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
