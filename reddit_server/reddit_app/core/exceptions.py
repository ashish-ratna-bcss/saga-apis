"""Exception hierarchy for the Reddit provider service.

Every exception carries a stable ``code`` plus an HTTP status so the API layer can
translate it into a sanitized JSON error without leaking internals or secrets. The
wire format is the SOC Eye contract::

    {"error": {"code": "SUBREDDIT_NOT_FOUND", "message": "..."}}
"""

from __future__ import annotations

from typing import Any


class RedditServiceError(Exception):
    """Base class for every error raised by this service."""

    code: str = "REDDIT_SERVICE_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str = "Reddit provider service error",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        """Sanitized ``{"error": {...}}`` representation returned to API clients."""

        error: dict[str, Any] = {"code": self.code, "message": self.message}
        error.update(self.details)
        return {"error": error}


class ValidationError(RedditServiceError):
    """A caller supplied a value that request-body validation could not reject."""

    code = "VALIDATION_ERROR"
    http_status = 422


class InvalidRedditUrlError(RedditServiceError):
    """A supplied URL is not a recognizable Reddit subreddit/post/comment link."""

    code = "INVALID_REDDIT_URL"
    http_status = 400


# --------------------------------------------------------------------------------------
# Reddit RSS errors -- this service's only Reddit transport (app/reddit/rss_client.py
# and friends). Public, unauthenticated .rss/.atom endpoints; no OAuth involved.
# --------------------------------------------------------------------------------------
class RedditRssError(RedditServiceError):
    """Base class for failures returned by (or while talking to) Reddit's public RSS."""

    code = "REDDIT_RSS_UNAVAILABLE"
    http_status = 502


class RedditRssTimeoutError(RedditRssError):
    code = "REDDIT_RSS_TIMEOUT"
    http_status = 504


class RedditRssRateLimitedError(RedditRssError):
    code = "REDDIT_RSS_RATE_LIMITED"
    http_status = 429

    def __init__(
        self, message: str = "Reddit RSS rate limit exceeded", *, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.retry_after is not None:
            payload["error"]["retry_after"] = self.retry_after
        return payload


class RedditRssForbiddenError(RedditRssError):
    """Reddit blocked/refused the unauthenticated request (e.g. anti-scraping)."""

    code = "REDDIT_RSS_FORBIDDEN"
    http_status = 403


class RedditRssUnavailableError(RedditRssError):
    """5xx, a connection failure, an oversized response, or any other transport
    failure that isn't more specifically classified above."""

    code = "REDDIT_RSS_UNAVAILABLE"
    http_status = 502


class RedditRssParseError(RedditRssError):
    """Reddit returned a 200 whose body is not well-formed XML."""

    code = "REDDIT_RSS_PARSE_ERROR"
    http_status = 502


class RedditRssInvalidQueryError(RedditRssError):
    """The caller's query/keywords/sort/time_range/limit failed validation."""

    code = "REDDIT_RSS_INVALID_QUERY"
    http_status = 422


class RedditRssInvalidSubredditError(RedditRssError):
    """The caller's subreddit name(s) failed validation."""

    code = "REDDIT_RSS_INVALID_SUBREDDIT"
    http_status = 422


class RedditRssInvalidUsernameError(RedditRssError):
    """The caller's username failed validation."""

    code = "REDDIT_RSS_INVALID_USERNAME"
    http_status = 422


class RedditRssQueueTimeoutError(RedditRssError):
    """Waiting for the shared process-wide Reddit RSS budget (see
    app/reddit/rate_limiter.py) would have taken longer than
    reddit_rss_max_queue_wait_seconds. Distinct from RedditRssTimeoutError,
    which means Reddit itself didn't respond in time -- this means the request
    never even got as far as Reddit."""

    code = "REDDIT_RSS_QUEUE_TIMEOUT"
    http_status = 504


class RedditRssClientRateLimitedError(RedditRssError):
    """A single caller exceeded its own request budget against this service's
    RSS endpoints (see app/core/rate_limit.py) -- distinct from
    RedditRssRateLimitedError, which means *Reddit* rate limited this service,
    not this service rate limiting the caller."""

    code = "REDDIT_RSS_CLIENT_RATE_LIMITED"
    http_status = 429

    def __init__(
        self,
        message: str = "Too many requests to the Reddit RSS endpoints",
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.retry_after is not None:
            payload["error"]["retry_after"] = self.retry_after
        return payload
