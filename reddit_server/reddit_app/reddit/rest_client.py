"""The single abstraction over the official Reddit REST API.

Nothing else in this service is allowed to construct an HTTP request to Reddit. That
keeps OAuth2 authentication, the pinned API host, rate-limit handling, retry policy and
error translation in exactly one place.

Authentication
---------------
Reddit's officially supported OAuth2 flows (https://github.com/reddit-archive/reddit/wiki/OAuth2):
``client_credentials`` for application-only, read-only access (no specific Reddit
account -- used when only ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET`` are set), or
``password`` for a script app authenticating as one Reddit account (used when
``REDDIT_USERNAME``/``REDDIT_PASSWORD`` are also set, which raises the rate limit and
gives ``/api/reddit/status`` a real account to report). Tokens are held only in memory,
re-requested on expiry (neither grant type returns a refresh token), and never
returned by any endpoint of this service.

Rate limits
-----------
Reddit returns ``X-Ratelimit-Remaining``/``X-Ratelimit-Used``/``X-Ratelimit-Reset``
headers on every OAuth API response, scoped to the whole token (not per-route, unlike
Discord). This client tracks the moment it is safe to send the next request from those
headers and waits proactively rather than provoking a 429. If a 429 does happen, it
honours ``Retry-After`` for a bounded number of attempts.

Retries
-------
Only transient failures are retried: 429, 5xx, timeouts and connection errors. A single
401 triggers exactly one re-authentication + retry (the token may have simply expired
early); permission problems (403) and missing resources (404) raise immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import Any, Final, Literal
from urllib.parse import quote

import httpx

from reddit_app.core.config import Settings
from reddit_app.core.exceptions import (
    RedditAPIError,
    RedditAuthError,
    RedditForbiddenError,
    RedditNotConfiguredError,
    RedditNotFoundError,
    RedditRateLimitError,
    RedditServerError,
    RedditTransportError,
)
from reddit_app.core.logging import get_logger, register_secret

logger = get_logger(__name__)

#: Reddit caps every listing endpoint at 100 items per call.
MAX_LISTING_PAGE_SIZE: Final[int] = 100

HttpMethod = Literal["GET", "POST"]


def mask_username(username: str | None) -> str | None:
    """``"john_doe"`` -> ``"j******e"``. Never returns the raw value for anything
    longer than 2 characters; short names are fully masked."""

    if not username:
        return None
    if len(username) <= 2:
        return "*" * len(username)
    return username[0] + "*" * (len(username) - 2) + username[-1]


class _Bucket:
    """Tracks when the next request may be sent. Reddit's rate limit is scoped to the
    whole OAuth token, not per-route, so one bucket for the whole client suffices."""

    __slots__ = ("lock", "reset_at")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.reset_at: float = 0.0


class RedditRestClient:
    """Async client for the official Reddit OAuth2 REST API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._token_transport = token_transport if token_transport is not None else transport
        self._client: httpx.AsyncClient | None = None
        self._token_client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._bucket = _Bucket()
        self._authenticated_username: str | None = None
        register_secret(settings.client_secret)
        register_secret(settings.password)

    # ------------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        """Create the underlying HTTP clients (idempotent)."""

        if self._client is not None:
            return
        if not self._settings.reddit_configured:
            logger.error(
                "Reddit credentials are not configured; Reddit API calls will be rejected"
            )
        user_agent = self._settings.reddit_user_agent or "reddit-service (unconfigured)"
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._client = httpx.AsyncClient(
            base_url=self._settings.reddit_api_base_url,
            headers=headers,
            timeout=httpx.Timeout(self._settings.reddit_request_timeout_seconds),
            transport=self._transport,
            follow_redirects=False,
        )
        self._token_client = httpx.AsyncClient(
            base_url=self._settings.reddit_oauth_base_url,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(self._settings.reddit_request_timeout_seconds),
            transport=self._token_transport,
            follow_redirects=False,
        )
        logger.info(
            "Reddit REST client initialised",
            extra={"base_url": self._settings.reddit_api_base_url},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._token_client is not None:
            await self._token_client.aclose()
            self._token_client = None
        logger.info("Reddit REST client closed")

    async def __aenter__(self) -> RedditRestClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ---------------------------------------------------------------- identity/auth --
    @property
    def is_authenticated(self) -> bool:
        return self._access_token is not None and time.monotonic() < self._token_expires_at

    @property
    def authenticated_username(self) -> str | None:
        """The Reddit account this client is authenticated as, or ``None`` for
        application-only (``client_credentials``) auth, which has no specific account."""

        return self._authenticated_username

    @property
    def grant_type(self) -> str:
        return self._settings.grant_type

    async def _authenticate(self) -> None:
        if self._token_client is None:
            await self.start()
        assert self._token_client is not None
        if not self._settings.reddit_configured:
            raise RedditNotConfiguredError(
                "Reddit is not configured. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET "
                "and REDDIT_USER_AGENT."
            )

        grant_type = self._settings.grant_type
        data: dict[str, str] = {"grant_type": grant_type}
        if grant_type == "password":
            data["username"] = self._settings.reddit_username
            data["password"] = self._settings.password

        try:
            response = await self._token_client.post(
                "/api/v1/access_token",
                data=data,
                auth=(self._settings.reddit_client_id, self._settings.client_secret),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.TimeoutException as exc:
            raise RedditTransportError("Timed out authenticating with Reddit") from exc
        except httpx.HTTPError as exc:
            raise RedditTransportError("Network error authenticating with Reddit") from exc

        if response.status_code >= 400:
            logger.error(
                "Reddit rejected the OAuth2 token request",
                extra={"status": response.status_code, "grant_type": grant_type},
            )
            raise RedditAuthError(
                "Reddit rejected the configured credentials", status_code=response.status_code
            )

        body = self._decode(response)
        if not isinstance(body, dict) or not body.get("access_token"):
            raise RedditAuthError("Reddit's token response did not include an access_token")

        self._access_token = body["access_token"]
        register_secret(self._access_token)
        expires_in = float(body.get("expires_in", 3600))
        margin = self._settings.reddit_token_refresh_margin_seconds
        self._token_expires_at = time.monotonic() + max(expires_in - margin, 1.0)
        self._authenticated_username = self._settings.reddit_username or None
        logger.info(
            "Authenticated with Reddit", extra={"grant_type": grant_type, "expires_in": expires_in}
        )

    async def ensure_token(self, *, force: bool = False) -> None:
        """Authenticate if there is no valid token yet (or ``force`` invalidates one)."""

        if force:
            self._access_token = None
            self._token_expires_at = 0.0
        if self.is_authenticated:
            return
        async with self._token_lock:
            if self.is_authenticated:
                return
            await self._authenticate()

    # -------------------------------------------------------------- rate limiting --
    async def _wait_for_slot(self) -> None:
        now = time.monotonic()
        if self._bucket.reset_at > now:
            delay = self._bucket.reset_at - now
            logger.debug("Waiting for rate-limit slot", extra={"delay_seconds": delay})
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

    # ------------------------------------------------------------------- requests --
    async def request(
        self,
        method: HttpMethod,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        route_key: str | None = None,
    ) -> Any:
        """Perform an authenticated Reddit API call with retries and rate limiting."""

        if self._client is None:
            await self.start()
        if not self._settings.reddit_configured:
            raise RedditNotConfiguredError(
                "Reddit is not configured. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET "
                "and REDDIT_USER_AGENT."
            )
        assert self._client is not None

        await self.ensure_token()

        key = route_key or f"{method}:{path}"
        attempts = self._settings.reddit_max_retries + 1
        last_error: RedditAPIError | None = None
        reauthenticated = False

        for attempt in range(1, attempts + 1):
            async with self._bucket.lock:
                await self._wait_for_slot()
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=params,
                    data=data,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
            except httpx.TimeoutException as exc:
                last_error = RedditTransportError(
                    "Timed out talking to Reddit", details={"path": path}
                )
                logger.warning(
                    "Reddit request timed out",
                    extra={"path": path, "attempt": attempt, "error": str(exc)},
                )
            except httpx.HTTPError as exc:
                last_error = RedditTransportError(
                    "Network error talking to Reddit", details={"path": path}
                )
                logger.warning(
                    "Reddit request failed at transport level",
                    extra={"path": path, "attempt": attempt, "error": str(exc)},
                )
            else:
                self._update_bucket(response)

                if response.status_code == 401 and not reauthenticated:
                    logger.info("Reddit token rejected mid-flight; re-authenticating once")
                    await self.ensure_token(force=True)
                    reauthenticated = True
                    continue

                if response.status_code == 429:
                    retry_after = self._parse_retry_after(response)
                    logger.warning(
                        "Reddit rate limited this request",
                        extra={"path": path, "retry_after": retry_after, "attempt": attempt},
                    )
                    if retry_after > self._settings.reddit_max_rate_limit_wait_seconds:
                        raise RedditRateLimitError(
                            "Reddit rate limit retry window is longer than allowed",
                            retry_after=retry_after,
                        )
                    self._bucket.reset_at = time.monotonic() + retry_after
                    last_error = RedditRateLimitError(
                        "Reddit rate limit exceeded", retry_after=retry_after
                    )
                    if attempt < attempts:
                        await asyncio.sleep(retry_after)
                        continue
                    raise last_error

                if response.status_code >= 500:
                    last_error = RedditServerError(
                        "Reddit returned a server error", status_code=response.status_code
                    )
                    logger.warning(
                        "Reddit server error",
                        extra={"path": path, "status": response.status_code, "attempt": attempt},
                    )
                elif response.status_code >= 400:
                    raise self._translate_client_error(response, path, key=key)
                else:
                    return self._decode(response)

            if attempt < attempts:
                await asyncio.sleep(self._backoff_delay(attempt))

        assert last_error is not None
        raise last_error

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, clamped to the configured maximum."""

        base = self._settings.reddit_retry_base_delay_seconds
        delay = min(base * (2 ** (attempt - 1)), self._settings.reddit_max_retry_delay_seconds)
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

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise RedditAPIError(
                "Reddit returned a non-JSON response", status_code=response.status_code
            ) from exc

    @staticmethod
    def _translate_client_error(
        response: httpx.Response, path: str, *, key: str
    ) -> RedditAPIError:
        """Map a 4xx into a typed, non-retryable exception."""

        status = response.status_code
        details = {"path": path}
        message = f"Reddit rejected the request ({status})"
        if status == 401:
            logger.error("Reddit rejected the access token (401)", extra={"path": path})
            return RedditAuthError(
                "Reddit rejected the access token", status_code=status, details=details
            )
        if status == 403:
            return RedditForbiddenError(
                "Reddit refused access to this resource", status_code=status, details=details
            )
        if status == 404:
            return RedditNotFoundError(
                "Reddit reports this resource does not exist", status_code=status, details=details
            )
        return RedditAPIError(message, status_code=status, details=details)

    # ------------------------------------------------------------------ endpoints --
    async def verify_identity(self) -> dict[str, Any] | None:
        """``GET /api/v1/me`` -- only meaningful for a ``password``-grant (account)
        token; Reddit has no "identity" for an application-only token, so this is
        skipped in that mode (the token exchange itself is the connectivity proof)."""

        if self._settings.grant_type != "password":
            return None
        return await self.request("GET", "/api/v1/me", route_key="GET:/api/v1/me")

    async def get_subreddit_about(self, name: str) -> dict[str, Any]:
        """``GET /r/{name}/about``."""

        return await self.request(
            "GET", f"/r/{quote(name)}/about", route_key="GET:/r/{name}/about"
        )

    async def get_subreddit_listing(
        self,
        name: str,
        sort: str,
        *,
        limit: int,
        after: str | None = None,
        t: str | None = None,
    ) -> dict[str, Any]:
        """``GET /r/{name}/{sort}`` -- one page of a subreddit listing.

        ``name`` may be Reddit's "+"-joined combined-subreddit syntax (e.g.
        "india+worldnews"); ``+`` is passed through unescaped since Reddit's
        router treats it as the multireddit separator, not a literal character.
        """

        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_LISTING_PAGE_SIZE))}
        if after:
            params["after"] = after
        if t:
            params["t"] = t
        return await self.request(
            "GET",
            f"/r/{quote(name, safe='+')}/{quote(sort)}",
            params=params,
            route_key="GET:/r/{name}/{sort}",
        )

    async def get_comments_tree(
        self, post_id: str, *, subreddit: str | None = None, limit: int | None = None
    ) -> list[Any]:
        """``GET /comments/{post_id}`` (or the ``/r/{sub}/...`` form) -- returns a raw
        2-element array: ``[post_listing, comments_listing]``."""

        path = (
            f"/r/{quote(subreddit)}/comments/{quote(post_id)}"
            if subreddit
            else f"/comments/{quote(post_id)}"
        )
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = max(1, min(int(limit), MAX_LISTING_PAGE_SIZE))
        result = await self.request("GET", path, params=params, route_key="GET:/comments/{id}")
        if not isinstance(result, list):
            raise RedditNotFoundError(f"Post {post_id} could not be found", status_code=404)
        return result

    async def get_more_children(self, *, link_id: str, children: list[str]) -> dict[str, Any]:
        """``POST /api/morechildren`` -- expand a "more comments" stub."""

        data = {
            "api_type": "json",
            "link_id": link_id,
            "children": ",".join(children[:MAX_LISTING_PAGE_SIZE]),
        }
        return await self.request(
            "POST", "/api/morechildren", data=data, route_key="POST:/api/morechildren"
        )

    async def get_info(self, *, fullnames: list[str]) -> dict[str, Any]:
        """``GET /api/info`` -- fetch one or more Things (post/comment/subreddit) by
        their fullname in a single call."""

        params = {"id": ",".join(fullnames)}
        return await self.request("GET", "/api/info", params=params, route_key="GET:/api/info")

    async def search_posts(
        self,
        q: str,
        *,
        subreddit: str | None = None,
        sort: str | None = None,
        t: str | None = None,
        limit: int,
        after: str | None = None,
    ) -> dict[str, Any]:
        """``GET /search`` or ``GET /r/{sub}/search`` -- public post discovery.

        ``subreddit`` may be a "+"-joined combined-subreddit (e.g. "india+worldnews")
        to scope the search to several subreddits at once.
        """

        params: dict[str, Any] = {
            "q": q,
            "limit": max(1, min(int(limit), MAX_LISTING_PAGE_SIZE)),
            "type": "link",
        }
        if sort:
            params["sort"] = sort
        if t:
            params["t"] = t
        if after:
            params["after"] = after
        if subreddit:
            params["restrict_sr"] = "true"
            path = f"/r/{quote(subreddit, safe='+')}/search"
            route_key = "GET:/r/{name}/search"
        else:
            path = "/search"
            route_key = "GET:/search"
        return await self.request("GET", path, params=params, route_key=route_key)

    async def search_subreddits(
        self, q: str, *, limit: int, after: str | None = None
    ) -> dict[str, Any]:
        """``GET /subreddits/search``."""

        params: dict[str, Any] = {"q": q, "limit": max(1, min(int(limit), MAX_LISTING_PAGE_SIZE))}
        if after:
            params["after"] = after
        return await self.request(
            "GET", "/subreddits/search", params=params, route_key="GET:/subreddits/search"
        )

    async def get_user_about(self, username: str) -> dict[str, Any]:
        """``GET /user/{username}/about``."""

        return await self.request(
            "GET", f"/user/{quote(username)}/about", route_key="GET:/user/{username}/about"
        )

    async def get_user_listing(
        self, username: str, kind: str, *, limit: int, after: str | None = None
    ) -> dict[str, Any]:
        """``GET /user/{username}/submitted`` or ``.../comments``."""

        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_LISTING_PAGE_SIZE))}
        if after:
            params["after"] = after
        return await self.request(
            "GET",
            f"/user/{quote(username)}/{quote(kind)}",
            params=params,
            route_key="GET:/user/{username}/" + kind,
        )
