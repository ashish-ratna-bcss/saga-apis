"""The Reddit RSS/Atom transport -- no OAuth, no token, no client id/secret of
any kind. This is the only class in this service allowed to make HTTP requests
to Reddit's public RSS host (``www.reddit.com``'s ``.rss`` endpoints).

Reddit cut unauthenticated RSS's rate limit hard in June 2026 (~100/10min down to
~1/min per feed), confirmed via ``X-Ratelimit-*`` response headers, so this client
proactively throttles from them (see ``_Bucket`` below) rather than a blind
hardcoded sleep. Reddit's own documented workaround is appending ``user=``/
``feed=`` (from an account's RSS preferences) to every request, which restores
the old ceiling; ``reddit_rss_user``/``reddit_rss_feed`` are optional and this
client works without them, just harder-throttled.

Only 429/5xx/timeouts/connection errors are retried, bounded, with backoff and
jitter.

Every attempt (including retries) first passes through ``self._global_bucket``
(see ``rate_limiter.py``) -- a proactive, process-wide pacing gate that exists
because this service is a shared gateway: many callers can hit this process at
once, and only Reddit's response headers driving ``self._bucket`` above would
otherwise do nothing to stop the first concurrent burst.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import Final

import httpx

from reddit_app.core.config import Settings
from reddit_app.core.exceptions import (
    RedditRssError,
    RedditRssForbiddenError,
    RedditRssQueueTimeoutError,
    RedditRssRateLimitedError,
    RedditRssTimeoutError,
    RedditRssUnavailableError,
)
from reddit_app.core.logging import get_logger, register_secret
from reddit_app.reddit.rate_limiter import TokenBucket

logger = get_logger(__name__)

RSS_HOST: Final[str] = "www.reddit.com"
RSS_BASE_URL: Final[str] = f"https://{RSS_HOST}"


class _Bucket:
    """Tracks when the next request may be sent from Reddit's
    ``X-Ratelimit-*`` response headers on RSS responses."""

    __slots__ = ("lock", "reset_at")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.reset_at: float = 0.0


class RedditRssClient:
    """Fetches raw feed bytes from Reddit's public RSS host. Parsing lives in
    ``rss_parser.py``; this class only owns the HTTP concern."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._bucket = _Bucket()
        self._global_bucket = TokenBucket(
            capacity=settings.reddit_rss_global_burst, refill_rate=settings.reddit_rss_global_rate
        )
        register_secret(settings.reddit_rss_feed_value)

    async def start(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=RSS_BASE_URL,
            headers={
                "User-Agent": self._settings.reddit_rss_user_agent,
                "Accept": "application/atom+xml, application/rss+xml, text/xml;q=0.9, */*;q=0.1",
            },
            timeout=httpx.Timeout(self._settings.reddit_rss_timeout_seconds),
            transport=self._transport,
            follow_redirects=False,
        )
        logger.info("Reddit RSS client initialised", extra={"base_url": RSS_BASE_URL})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        logger.info("Reddit RSS client closed")

    async def __aenter__(self) -> RedditRssClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def fetch(self, path: str, params: dict[str, str]) -> bytes:
        """GET ``path`` (already-validated, already-built by ``rss_urls.py``) and
        return the raw response body, bounded and retried per the module docstring."""

        if self._client is None:
            await self.start()
        assert self._client is not None

        request_params = dict(params)
        if self._settings.reddit_rss_user and self._settings.reddit_rss_feed_value:
            request_params["user"] = self._settings.reddit_rss_user
            request_params["feed"] = self._settings.reddit_rss_feed_value

        attempts = self._settings.reddit_rss_max_retries + 1
        last_error: RedditRssError | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self._global_bucket.acquire(
                    max_wait_seconds=self._settings.reddit_rss_max_queue_wait_seconds
                )
            except TimeoutError as exc:
                logger.warning(
                    "Gave up waiting for the shared Reddit RSS budget",
                    extra={"path": path, "attempt": attempt},
                )
                raise RedditRssQueueTimeoutError(str(exc)) from exc
            async with self._bucket.lock:
                await self._wait_for_slot()
            try:
                async with self._client.stream("GET", path, params=request_params) as response:
                    body = await self._read_body(
                        response, path=path, attempt=attempt, attempts=attempts
                    )
            except httpx.TimeoutException:
                last_error = RedditRssTimeoutError("Timed out talking to Reddit RSS")
                logger.warning(
                    "Reddit RSS request timed out", extra={"path": path, "attempt": attempt}
                )
            except httpx.HTTPError as exc:
                last_error = RedditRssUnavailableError("Network error talking to Reddit RSS")
                logger.warning(
                    "Reddit RSS request failed at transport level",
                    extra={"path": path, "attempt": attempt, "error": str(exc)},
                )
            else:
                if body is not None:
                    return body
                # A retryable 429/5xx already slept inside _read_body; loop again
                # immediately, with no additional backoff sleep.
                continue

            if attempt < attempts:
                await asyncio.sleep(self._backoff_delay(attempt))
            else:
                raise last_error

        assert last_error is not None
        raise last_error

    async def _wait_for_slot(self) -> None:
        now = time.monotonic()
        if self._bucket.reset_at > now:
            delay = self._bucket.reset_at - now
            logger.debug("Waiting for RSS rate-limit slot", extra={"delay_seconds": delay})
            await asyncio.sleep(delay)

    def _update_bucket(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-Ratelimit-Remaining")
        reset = response.headers.get("X-Ratelimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            if float(remaining) < 1:
                self._bucket.reset_at = time.monotonic() + float(reset)
        except ValueError:  # pragma: no cover - malformed header
            return

    async def _read_body(
        self, response: httpx.Response, *, path: str, attempt: int, attempts: int
    ) -> bytes | None:
        """Read one response. Returns the body on success, ``None`` if this attempt
        was a retryable 429/5xx (already slept for it -- ``fetch`` should just loop
        again with no additional backoff), or raises for a non-retryable failure."""

        self._update_bucket(response)

        if response.status_code in (301, 302, 303, 307, 308):
            raise RedditRssUnavailableError(
                "Reddit RSS returned an unexpected redirect",
                details={"path": path, "status": response.status_code},
            )
        if response.status_code == 403:
            raise RedditRssForbiddenError(
                "Reddit RSS refused this request (blocked, or rate-limited by IP reputation)"
            )
        if response.status_code == 429:
            retry_after = self._parse_retry_after(response)
            logger.warning(
                "Reddit RSS rate limited this request",
                extra={"path": path, "retry_after": retry_after, "attempt": attempt},
            )
            if attempt >= attempts:
                raise RedditRssRateLimitedError(
                    "Reddit RSS rate limit exceeded", retry_after=retry_after
                )
            await asyncio.sleep(retry_after)
            return None
        if response.status_code >= 500:
            logger.warning(
                "Reddit RSS server error",
                extra={"path": path, "status": response.status_code, "attempt": attempt},
            )
            if attempt >= attempts:
                raise RedditRssUnavailableError(
                    "Reddit RSS returned a server error", details={"status": response.status_code}
                )
            await asyncio.sleep(self._backoff_delay(attempt))
            return None
        if response.status_code >= 400:
            raise RedditRssUnavailableError(
                "Reddit RSS returned an unexpected error",
                details={"path": path, "status": response.status_code},
            )

        cap = self._settings.reddit_rss_max_response_bytes
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > cap:
                raise RedditRssUnavailableError(
                    "Reddit RSS response exceeded the size limit", details={"limit_bytes": cap}
                )
        return bytes(body)

    def _backoff_delay(self, attempt: int) -> float:
        base = self._settings.reddit_rss_retry_base_delay_seconds
        delay = min(base * (2 ** (attempt - 1)), 10.0)
        return delay * (0.5 + random.random() / 2)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After")
        if header:
            with contextlib.suppress(ValueError):
                return max(float(header), 0.0)
        reset = response.headers.get("X-Ratelimit-Reset")
        if reset:
            with contextlib.suppress(ValueError):
                return max(float(reset), 0.0)
        return 1.0
