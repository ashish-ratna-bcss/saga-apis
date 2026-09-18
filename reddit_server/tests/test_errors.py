from __future__ import annotations

import pytest

from reddit_app.core.config import Settings
from reddit_app.core.exceptions import RedditRateLimitError, RedditServerError
from tests.fake_reddit import rate_limited, server_error


def test_error_envelope_shape(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit", json={"name": "doesnotexist"})
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) >= {"code", "message"}


def test_not_configured_returns_service_unavailable(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    settings = Settings(_env_file=None, reddit_client_id="", reddit_client_secret="", reddit_user_agent="")
    app = create_app(
        settings, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        response = client.post("/api/reddit/subreddit", json={"name": "india"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REDDIT_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_rest_client_retries_after_rate_limit(clients, fake_reddit) -> None:
    fake_reddit.scripted["/r/india/about"] = [rate_limited(retry_after=0.01)]
    result = await clients.rest.get_subreddit_about("india")
    assert result["data"]["display_name"] == "india"


@pytest.mark.asyncio
async def test_rest_client_retries_after_server_error(clients, fake_reddit) -> None:
    fake_reddit.scripted["/r/india/about"] = [server_error()]
    result = await clients.rest.get_subreddit_about("india")
    assert result["data"]["display_name"] == "india"


@pytest.mark.asyncio
async def test_rest_client_raises_after_exhausting_retries(clients, fake_reddit) -> None:
    fake_reddit.scripted["/r/india/about"] = [server_error(), server_error(), server_error()]
    with pytest.raises(RedditServerError):
        await clients.rest.get_subreddit_about("india")


@pytest.mark.asyncio
async def test_rest_client_raises_rate_limit_error_when_wait_too_long(clients, fake_reddit) -> None:
    fake_reddit.scripted["/r/india/about"] = [rate_limited(retry_after=999)]
    with pytest.raises(RedditRateLimitError):
        await clients.rest.get_subreddit_about("india")


@pytest.mark.asyncio
async def test_rest_client_reauthenticates_once_on_401(clients, fake_reddit) -> None:
    """A 401 mid-flight (e.g. the token was invalidated Reddit-side) triggers exactly
    one silent re-authentication and retry, not an error surfaced to the caller."""

    from tests.fake_reddit import _json

    fake_reddit.scripted["/r/india/about"] = [_json(401, {"message": "Unauthorized"})]
    result = await clients.rest.get_subreddit_about("india")
    assert result["data"]["display_name"] == "india"
    assert len(fake_reddit.token_calls) == 2  # the initial token + the forced re-auth


def test_api_key_required_when_configured(fake_reddit) -> None:
    from fastapi.testclient import TestClient

    from reddit_app.main import create_app

    settings = Settings(
        _env_file=None,
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_user_agent="ua",
        api_keys="correct-key",
    )
    app = create_app(
        settings, transport=fake_reddit.api_transport(), token_transport=fake_reddit.token_transport()
    )
    with TestClient(app) as client:
        missing = client.post("/api/reddit/subreddit", json={"name": "india"})
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "UNAUTHORIZED"

        wrong = client.post(
            "/api/reddit/subreddit", json={"name": "india"}, headers={"X-API-Key": "wrong"}
        )
        assert wrong.status_code == 401

        right = client.post(
            "/api/reddit/subreddit", json={"name": "india"}, headers={"X-API-Key": "correct-key"}
        )
        assert right.status_code == 200


def test_api_key_never_required_by_default(app_client) -> None:
    response = app_client.post("/api/reddit/subreddit", json={"name": "india"})
    assert response.status_code == 200
