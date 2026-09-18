from __future__ import annotations

import time

from reddit_app.core.rate_limit import ClientRateLimiter


def test_requests_within_budget_are_allowed() -> None:
    limiter = ClientRateLimiter(max_requests=10, window_seconds=60.0, max_tracked_clients=100)
    for _ in range(10):
        assert limiter.check("client-a") is None


def test_eleventh_request_is_rejected_with_retry_after() -> None:
    limiter = ClientRateLimiter(max_requests=10, window_seconds=60.0, max_tracked_clients=100)
    for _ in range(10):
        limiter.check("client-a")
    retry_after = limiter.check("client-a")
    assert retry_after is not None
    assert 0 < retry_after <= 60.0


def test_requests_become_available_after_window_expires() -> None:
    limiter = ClientRateLimiter(max_requests=2, window_seconds=0.05, max_tracked_clients=100)
    limiter.check("client-a")
    limiter.check("client-a")
    assert limiter.check("client-a") is not None
    time.sleep(0.08)
    assert limiter.check("client-a") is None


def test_clients_are_isolated_from_each_other() -> None:
    limiter = ClientRateLimiter(max_requests=1, window_seconds=60.0, max_tracked_clients=100)
    assert limiter.check("client-a") is None
    assert limiter.check("client-a") is not None  # a is over budget
    assert limiter.check("client-b") is None  # b is unaffected by a's usage


def test_max_tracked_clients_evicts_idle_entries() -> None:
    limiter = ClientRateLimiter(max_requests=10, window_seconds=0.01, max_tracked_clients=2)
    limiter.check("client-a")
    time.sleep(0.02)  # client-a's window is now fully idle
    limiter.check("client-b")
    limiter.check("client-c")  # pushes tracked count over max, triggers eviction
    assert "client-a" not in limiter._hits
