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


class ConfigurationError(RedditServiceError):
    """The service is missing or has invalid configuration."""

    code = "CONFIGURATION_ERROR"
    http_status = 500


class RedditNotConfiguredError(ConfigurationError):
    """No usable Reddit OAuth2 credentials are configured."""

    code = "REDDIT_NOT_CONFIGURED"
    http_status = 503


class ValidationError(RedditServiceError):
    """A caller supplied a value that request-body validation could not reject."""

    code = "VALIDATION_ERROR"
    http_status = 422


class InvalidRedditUrlError(RedditServiceError):
    """A supplied URL is not a recognizable Reddit subreddit/post/comment link."""

    code = "INVALID_REDDIT_URL"
    http_status = 400


class SubredditNotFoundError(RedditServiceError):
    code = "SUBREDDIT_NOT_FOUND"
    http_status = 404

    def __init__(self, name: str) -> None:
        super().__init__(
            f"The subreddit r/{name} could not be found.", details={"subreddit": name}
        )


class SubredditPrivateError(RedditServiceError):
    """The subreddit exists but is private -- not readable without membership."""

    code = "SUBREDDIT_PRIVATE"
    http_status = 403

    def __init__(self, name: str) -> None:
        super().__init__(
            f"The subreddit r/{name} is private and cannot be read.",
            details={"subreddit": name},
        )


class SubredditQuarantinedError(RedditServiceError):
    """The subreddit is quarantined; Reddit refuses listings without an opted-in,
    logged-in user explicitly confirming the quarantine interstitial -- something an
    application-only/service-account integration cannot do non-interactively."""

    code = "SUBREDDIT_QUARANTINED"
    http_status = 403

    def __init__(self, name: str) -> None:
        super().__init__(
            f"The subreddit r/{name} is quarantined and requires interactive "
            "opt-in that this service account has not completed.",
            details={"subreddit": name},
        )


class PostNotFoundError(RedditServiceError):
    code = "POST_NOT_FOUND"
    http_status = 404

    def __init__(self, post_id: str) -> None:
        super().__init__(
            f"The post {post_id} could not be found.", details={"post_id": post_id}
        )


class CommentNotFoundError(RedditServiceError):
    code = "COMMENT_NOT_FOUND"
    http_status = 404

    def __init__(self, comment_id: str) -> None:
        super().__init__(
            f"The comment {comment_id} could not be found.",
            details={"comment_id": comment_id},
        )


class UserNotFoundError(RedditServiceError):
    code = "USER_NOT_FOUND"
    http_status = 404

    def __init__(self, username: str) -> None:
        super().__init__(
            f"The user u/{username} could not be found.", details={"username": username}
        )


class UnsupportedSearchError(RedditServiceError):
    """A search capability was requested that Reddit's public API does not support
    (e.g. free-text search across all comments)."""

    code = "UNSUPPORTED_SEARCH"
    http_status = 400


# --------------------------------------------------------------------------------------
# Reddit API errors
# --------------------------------------------------------------------------------------
class RedditAPIError(RedditServiceError):
    """Base class for failures returned by (or while talking to) the Reddit API."""

    code = "REDDIT_API_ERROR"
    http_status = 502

    #: Whether retrying the same call could plausibly succeed.
    retryable: bool = False

    def __init__(
        self,
        message: str = "Reddit API error",
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.status_code = status_code

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.status_code is not None:
            payload["error"]["reddit_status"] = self.status_code
        return payload


class RedditAuthError(RedditAPIError):
    """401/invalid_grant -- the configured Reddit credentials are missing or wrong."""

    code = "REDDIT_AUTH_FAILED"
    http_status = 502
    retryable = False


class RedditForbiddenError(RedditAPIError):
    """403 -- Reddit rejected the request for a reason other than auth (e.g. a
    private/quarantined subreddit, or a suspended/shadow-restricted account)."""

    code = "REDDIT_FORBIDDEN"
    http_status = 403
    retryable = False


class RedditNotFoundError(RedditAPIError):
    """404 -- the raw Reddit resource does not exist. Route handlers translate this
    into a more specific *NotFoundError once the resource kind is known."""

    code = "REDDIT_NOT_FOUND"
    http_status = 404
    retryable = False


class RedditRateLimitError(RedditAPIError):
    """429 -- the rate limit was hit and retries were exhausted."""

    code = "REDDIT_RATE_LIMITED"
    http_status = 429
    retryable = True

    def __init__(
        self, message: str = "Reddit rate limit exceeded", *, retry_after: float | None = None, **kwargs: Any
    ) -> None:
        super().__init__(message, status_code=429, **kwargs)
        self.retry_after = retry_after

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        if self.retry_after is not None:
            payload["error"]["retry_after"] = self.retry_after
        return payload


class RedditServerError(RedditAPIError):
    """5xx returned by Reddit."""

    code = "REDDIT_API_ERROR"
    http_status = 502
    retryable = True


class RedditTransportError(RedditAPIError):
    """Network/timeout failure while talking to Reddit."""

    code = "REDDIT_API_ERROR"
    http_status = 504
    retryable = True


# --------------------------------------------------------------------------------------
# Reddit RSS errors -- the unauthenticated transport (app/reddit/rss_client.py and
# friends). Deliberately a separate hierarchy from RedditAPIError above: distinct error
# codes so a caller can tell which transport failed, per the RSS integration contract.
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
