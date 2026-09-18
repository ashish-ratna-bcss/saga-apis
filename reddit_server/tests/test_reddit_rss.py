"""Route-level tests for the unauthenticated Reddit RSS transport.

Includes the mandatory proof (AC-02/AC-15) that ``/api/reddit/rss/*`` works when
Reddit OAuth credentials are completely absent -- see
``test_rss_works_without_any_reddit_oauth_credentials`` below.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from reddit_app.core.config import Settings
from reddit_app.main import create_app
from tests.fake_reddit import FakeReddit
from tests.fake_reddit_rss import FakeRedditRss, atom_entry, atom_feed


def test_rss_monitor_with_query(app_client) -> None:
    response = app_client.post("/api/reddit/rss/monitor", json={"query": "protest"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "reddit"
    assert body["transport"] == "rss"
    assert body["authenticated"] is False
    assert body["count"] == 1
    assert body["posts"][0]["id"] == "abc123"


def test_rss_monitor_date_filter(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [
            atom_entry(post_id="early", published="2026-07-01T10:00:00+00:00"),
            atom_entry(post_id="inrange", published="2026-08-15T10:00:00+00:00"),
        ]
    )
    response = app_client.post(
        "/api/reddit/rss/monitor",
        json={"query": "protest", "from_date": "2026-08-01", "to_date": "2026-08-31"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["posts"][0]["id"] == "inrange"
    assert body["from_date"] == "2026-08-01"
    assert body["to_date"] == "2026-08-31"


def test_rss_search_get_date_filter(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(post_id="early", published="2026-07-01T10:00:00+00:00")]
    )
    response = app_client.get(
        "/api/reddit/rss/search",
        params={"q": "protest", "from_date": "2026-08-01"},
    )
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_rss_monitor_invalid_date_rejected(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/monitor", json={"query": "protest", "from_date": "not-a-date"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REDDIT_RSS_INVALID_QUERY"


def test_rss_monitor_requires_query_or_keywords(app_client) -> None:
    response = app_client.post("/api/reddit/rss/monitor", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REDDIT_RSS_INVALID_QUERY"


def test_rss_monitor_keywords_ored_and_scoped(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/monitor",
        json={
            "keywords": ["protest", "strike", "curfew"],
            "subreddits": ["India", "worldnews"],
            "sort": "new",
            "time_range": "day",
            "limit": 100,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "protest OR strike OR curfew"
    assert body["subreddits"] == ["India", "worldnews"]


def test_rss_monitor_invalid_subreddit(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/monitor", json={"query": "protest", "subreddits": ["has a space"]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REDDIT_RSS_INVALID_SUBREDDIT"


def test_rss_search_get_convenience_matches_monitor(app_client) -> None:
    monitor_body = app_client.post(
        "/api/reddit/rss/monitor",
        json={
            "keywords": ["protest", "strike"],
            "subreddits": ["India"],
            "sort": "new",
            "time_range": "day",
        },
    ).json()
    search_body = app_client.get(
        "/api/reddit/rss/search",
        params=[
            ("keywords", "protest"),
            ("keywords", "strike"),
            ("subreddit", "India"),
            ("sort", "new"),
            ("time", "day"),
        ],
    ).json()
    assert monitor_body["query"] == search_body["query"] == "protest OR strike"
    assert monitor_body["subreddits"] == search_body["subreddits"] == ["India"]
    assert monitor_body["posts"] == search_body["posts"]


def test_rss_search_get_requires_q_or_keywords(app_client) -> None:
    response = app_client.get("/api/reddit/rss/search")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REDDIT_RSS_INVALID_QUERY"


def test_rss_search_combined_subreddit_string(app_client) -> None:
    response = app_client.get(
        "/api/reddit/rss/search", params={"q": "protest", "subreddit": "India+worldnews"}
    )
    assert response.status_code == 200
    assert response.json()["subreddits"] == ["India", "worldnews"]


def test_rss_event_uses_default_strategy(app_client) -> None:
    response = app_client.post("/api/reddit/rss/event", json={})
    assert response.status_code == 200
    body = response.json()
    assert "event_signal" in body
    assert set(body["event_signal"].keys()) == {
        "detected",
        "post_count",
        "unique_subreddits",
        "matched_keywords",
        "threshold",
    }


def test_rss_event_overrides(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/event",
        json={"subreddits": ["india"], "keywords": ["example"], "limit": 10},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subreddits"] == ["india"]
    assert body["query"] == "example"


def test_rss_endpoints_require_this_services_own_api_key(
    fake_reddit: FakeReddit, fake_reddit_rss: FakeRedditRss
) -> None:
    """RSS routes carry the same X-API-Key gate as every other /api/reddit/* route
    -- Reddit auth is skipped, this service's own auth policy is not."""

    settings = Settings(
        _env_file=None,
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_user_agent="ua",
        api_keys="test-key",
    )
    app = create_app(
        settings,
        transport=fake_reddit.api_transport(),
        token_transport=fake_reddit.token_transport(),
        rss_transport=fake_reddit_rss.transport(),
    )
    with TestClient(app) as client:
        response = client.post("/api/reddit/rss/monitor", json={"query": "protest"})
        assert response.status_code == 401

        response = client.post(
            "/api/reddit/rss/monitor",
            json={"query": "protest"},
            headers={"X-API-Key": "test-key"},
        )
        assert response.status_code == 200


def test_rss_appears_in_openapi_with_reddit_rss_tag(app_client) -> None:
    schema = app_client.get("/openapi.json").json()
    assert "/api/reddit/rss/monitor" in schema["paths"]
    assert "/api/reddit/rss/search" in schema["paths"]
    assert "/api/reddit/rss/event" in schema["paths"]
    tag_names = {tag["name"] for tag in schema.get("tags", [])}
    assert "reddit-rss" in tag_names


def test_rss_error_mapping_403_forbidden(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [httpx.Response(403, content=b"blocked")]
    response = app_client.post("/api/reddit/rss/monitor", json={"query": "protest"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REDDIT_RSS_FORBIDDEN"


def test_rss_error_mapping_rate_limited(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(429, content=b"", headers={"Retry-After": "0.01"}) for _ in range(5)
    ]
    response = app_client.post("/api/reddit/rss/monitor", json={"query": "protest"})
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "REDDIT_RSS_RATE_LIMITED"
    assert "retry_after" in response.json()["error"]


def test_rss_error_mapping_upstream_failure(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(502, content=b"boom") for _ in range(5)
    ]
    response = app_client.post("/api/reddit/rss/monitor", json={"query": "protest"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "REDDIT_RSS_UNAVAILABLE"


def test_rss_error_mapping_malformed_xml(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [
        httpx.Response(200, content=b"<feed><entry><title>unclosed")
    ]
    response = app_client.post("/api/reddit/rss/monitor", json={"query": "protest"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "REDDIT_RSS_PARSE_ERROR"


def test_rss_empty_feed_is_a_valid_empty_result_not_an_error(
    app_client, fake_reddit_rss: FakeRedditRss
) -> None:
    fake_reddit_rss.scripted["/search.rss"] = [httpx.Response(200, content=atom_feed([]))]
    response = app_client.post("/api/reddit/rss/monitor", json={"query": "protest"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["posts"] == []


def test_rss_works_without_any_reddit_oauth_credentials(fake_reddit_rss: FakeRedditRss) -> None:
    """AC-02 / AC-15: the RSS path must reach Reddit RSS without OAuth token
    acquisition, proving RSS path != OAuth path. No FakeReddit (OAuth transport)
    is even constructed here -- only the RSS transport is wired up, and Reddit
    OAuth credentials are left entirely absent."""

    settings = Settings(
        _env_file=None,
        reddit_client_id="",
        reddit_client_secret="",
        reddit_user_agent="",
    )
    assert not settings.reddit_configured

    def oauth_should_never_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"OAuth transport was called unexpectedly: {request.url}")

    app = create_app(
        settings,
        transport=httpx.MockTransport(oauth_should_never_be_called),
        token_transport=httpx.MockTransport(oauth_should_never_be_called),
        rss_transport=fake_reddit_rss.transport(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/reddit/rss/monitor",
            json={
                "keywords": ["protest"],
                "subreddits": ["India"],
                "time_range": "day",
                "limit": 10,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["count"] == 1
    assert fake_reddit_rss.calls, "the RSS transport should have been hit"


def test_rss_monitor_matches_oauth_style_error_envelope(app_client) -> None:
    """Same {"error": {"code": ..., "message": ...}} shape as the OAuth transport
    -- one error model across both, per the RSS integration contract."""

    response = app_client.post("/api/reddit/rss/monitor", json={})
    body = response.json()
    assert set(body.keys()) == {"error"}
    assert "code" in body["error"] and "message" in body["error"]


def test_rss_shares_one_fetch_across_callers_of_the_same_feed(
    settings: Settings, fake_reddit: FakeReddit, fake_reddit_rss: FakeRedditRss
) -> None:
    """Layer 2 (feed cache): two callers hitting the identical feed within the
    TTL must only cause one outbound Reddit request -- not the whole app's
    other client-limiter/global-limiter machinery, just the cache itself."""

    app = create_app(
        settings,
        transport=fake_reddit.api_transport(),
        token_transport=fake_reddit.token_transport(),
        rss_transport=fake_reddit_rss.transport(),
    )
    with TestClient(app) as client:
        first = client.post("/api/reddit/rss/monitor", json={"query": "protest"})
        second = client.post("/api/reddit/rss/monitor", json={"query": "protest"})

    assert first.status_code == second.status_code == 200
    assert first.json()["posts"] == second.json()["posts"]
    assert len(fake_reddit_rss.calls_to("/search.rss")) == 1


def test_rss_different_feeds_each_get_their_own_fetch(
    settings: Settings, fake_reddit: FakeReddit, fake_reddit_rss: FakeRedditRss
) -> None:
    app = create_app(
        settings,
        transport=fake_reddit.api_transport(),
        token_transport=fake_reddit.token_transport(),
        rss_transport=fake_reddit_rss.transport(),
    )
    with TestClient(app) as client:
        client.post(
            "/api/reddit/rss/monitor", json={"query": "protest", "subreddits": ["india"]}
        )
        client.post(
            "/api/reddit/rss/monitor", json={"query": "protest", "subreddits": ["hyderabad"]}
        )

    assert len(fake_reddit_rss.calls_to("/search.rss")) == 2


def test_rss_client_limiter_returns_429_with_retry_after(fake_reddit_rss: FakeRedditRss) -> None:
    settings = Settings(
        _env_file=None,
        reddit_client_id="",
        reddit_client_secret="",
        reddit_user_agent="",
        reddit_rss_client_limit=3,
        reddit_rss_client_window_seconds=60.0,
    )
    app = create_app(settings, rss_transport=fake_reddit_rss.transport())
    with TestClient(app) as client:
        for _ in range(3):
            response = client.post("/api/reddit/rss/monitor", json={"query": "protest"})
            assert response.status_code == 200
        blocked = client.post("/api/reddit/rss/monitor", json={"query": "protest"})

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "REDDIT_RSS_CLIENT_RATE_LIMITED"
    assert blocked.json()["error"]["retry_after"] > 0


def test_rss_client_limiter_does_not_affect_oauth_routes(fake_reddit: FakeReddit) -> None:
    """Section 13: the per-caller RSS limiter must not leak onto unrelated
    /api/reddit/* routes."""

    settings = Settings(
        _env_file=None,
        reddit_client_id="id",
        reddit_client_secret="secret",
        reddit_user_agent="ua",
        reddit_rss_client_limit=1,
        reddit_rss_client_window_seconds=60.0,
    )
    app = create_app(
        settings,
        transport=fake_reddit.api_transport(),
        token_transport=fake_reddit.token_transport(),
    )
    with TestClient(app) as client:
        for _ in range(5):
            response = client.post("/api/reddit/subreddit", json={"name": "india"})
            assert response.status_code == 200


def test_rss_client_limiter_isolates_different_callers(fake_reddit_rss: FakeRedditRss) -> None:
    settings = Settings(
        _env_file=None,
        reddit_client_id="",
        reddit_client_secret="",
        reddit_user_agent="",
        reddit_rss_client_limit=1,
        reddit_rss_client_window_seconds=60.0,
    )
    app = create_app(settings, rss_transport=fake_reddit_rss.transport())
    client_a = TestClient(app, client=("1.2.3.4", 12345))
    client_b = TestClient(app, client=("5.6.7.8", 54321))

    assert client_a.post("/api/reddit/rss/monitor", json={"query": "protest"}).status_code == 200
    assert client_a.post("/api/reddit/rss/monitor", json={"query": "protest"}).status_code == 429
    # a different caller is unaffected by a's usage
    assert client_b.post("/api/reddit/rss/monitor", json={"query": "protest"}).status_code == 200


def test_rss_strong_keywords_force_signal_regardless_of_min_matches(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/monitor",
        json={"query": "protest", "strong_keywords": ["example"], "min_matches": 5},
    )
    body = response.json()
    assert body["posts"][0]["signal"] is True
    assert "example" in body["posts"][0]["matched_keywords"]


def test_rss_min_matches_requires_enough_keyword_hits(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/monitor",
        json={"query": "protest", "keywords": ["example"], "min_matches": 2},
    )
    body = response.json()
    assert body["posts"][0]["signal"] is False  # only 1 of the 2 required keywords hits


def test_rss_exclude_drops_matching_posts_entirely(app_client) -> None:
    response = app_client.post(
        "/api/reddit/rss/monitor", json={"query": "protest", "exclude": ["example"]}
    )
    body = response.json()
    assert body["count"] == 0
    assert body["posts"] == []


def test_rss_match_field_title_only(app_client, fake_reddit_rss: FakeRedditRss) -> None:
    fake_reddit_rss.default_feed = atom_feed(
        [atom_entry(title="Nothing relevant here", content="protest happening now")]
    )
    title_only = app_client.post(
        "/api/reddit/rss/monitor",
        json={"query": "irrelevant", "keywords": ["protest"], "match_field": "title"},
    ).json()
    full_text = app_client.post(
        "/api/reddit/rss/monitor",
        json={"query": "irrelevant", "keywords": ["protest"], "match_field": "full"},
    ).json()
    assert title_only["posts"][0]["matched_keywords"] == []
    assert full_text["posts"][0]["matched_keywords"] == ["protest"]


def test_rss_request_isolation_across_concurrent_callers(
    settings: Settings, fake_reddit: FakeReddit, fake_reddit_rss: FakeRedditRss
) -> None:
    """Section 23/24: two callers sharing one cached feed must each get their
    own, independently-computed filtering result."""

    app = create_app(
        settings,
        transport=fake_reddit.api_transport(),
        token_transport=fake_reddit.token_transport(),
        rss_transport=fake_reddit_rss.transport(),
    )
    with TestClient(app) as client:
        protest_view = client.post(
            "/api/reddit/rss/monitor", json={"query": "x", "keywords": ["example"]}
        ).json()
        election_view = client.post(
            "/api/reddit/rss/monitor", json={"query": "x", "keywords": ["does-not-exist"]}
        ).json()

    assert protest_view["posts"][0]["matched_keywords"] == ["example"]
    assert protest_view["posts"][0]["signal"] is True
    assert election_view["posts"][0]["matched_keywords"] == []
    assert election_view["posts"][0]["signal"] is False
