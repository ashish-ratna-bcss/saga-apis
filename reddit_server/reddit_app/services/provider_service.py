"""Reddit provider service - the storage-decoupled contract SOC Eye integrates
against.

Deliberately independent of any database: every method here reads live from Reddit
and returns plain normalized dicts - nothing is persisted, and there is no
``/monitoring/start``/``/stop`` lifecycle. SOC Eye owns scheduling, deduplication,
storage, detection and alerts; this service only owns "fetch Reddit data, return
stable JSON" (see the root of this task: monitoring lifecycle stays entirely on the
SOC Eye side).
"""

from __future__ import annotations

from collections import deque
from typing import Any

from reddit_app.core.exceptions import (
    CommentNotFoundError,
    PostNotFoundError,
    RedditAPIError,
    RedditForbiddenError,
    RedditNotConfiguredError,
    RedditNotFoundError,
    SubredditNotFoundError,
    SubredditPrivateError,
    UnsupportedSearchError,
    UserNotFoundError,
    ValidationError,
)
from reddit_app.reddit.client import RedditClientManager
from reddit_app.reddit.normalize import (
    normalize_comment,
    normalize_post,
    normalize_subreddit,
    normalize_user,
    parse_listing,
)
from reddit_app.reddit.pagination import clamp_limit, decode_cursor, encode_cursor
from reddit_app.reddit.rest_client import mask_username
from reddit_app.reddit.urls import (
    classify_reddit_url,
    normalize_subreddit_name,
    normalize_subreddit_names,
    normalize_username,
)

SUBREDDIT_SORTS = frozenset({"new", "hot", "top", "rising", "controversial"})
SEARCH_SORTS = frozenset({"relevance", "hot", "top", "new", "comments"})
SEARCH_TIMES = frozenset({"hour", "day", "week", "month", "year", "all"})
ACCESS_STATES = frozenset({"public", "private", "restricted", "quarantined", "not_found", "unknown"})

#: Bounded "load more comments" expansion per POST_COMMENTS call - protects Reddit's
#: rate limit and this service's response time on very large threads. See the
#: known-limitations note in comment_details/post_comments below.
MAX_MORE_EXPANSIONS = 3
MORE_CHILDREN_BATCH = 100


class ProviderService:
    """One live Reddit call per method; nothing is cached or stored."""

    def __init__(self, client_manager: RedditClientManager) -> None:
        self.client_manager = client_manager

    @property
    def _client(self):
        return self.client_manager.rest

    def _require_configured(self) -> None:
        if not self.client_manager.is_configured:
            raise RedditNotConfiguredError(
                "Reddit is not configured. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET "
                "and REDDIT_USER_AGENT."
            )

    # -- STATUS ------------------------------------------------------------- #

    async def status(self) -> dict[str, Any]:
        if not self.client_manager.is_configured:
            return {"connected": False, "authorized": False, "account": None}
        try:
            await self._client.ensure_token()
            if self._client.grant_type == "password":
                await self._client.verify_identity()
        except RedditAPIError:
            return {"connected": False, "authorized": False, "account": None}

        username = self._client.authenticated_username
        account = {"username": mask_username(username)} if username else None
        return {"connected": True, "authorized": True, "account": account}

    # -- SUBREDDIT_INFO ------------------------------------------------------- #

    async def subreddit_info(self, raw_name: str) -> dict[str, Any]:
        self._require_configured()
        name = normalize_subreddit_name(raw_name)
        data = await self._fetch_subreddit_about(name)
        return normalize_subreddit(data)

    async def _fetch_subreddit_about(self, name: str) -> dict[str, Any]:
        try:
            thing = await self._client.get_subreddit_about(name)
        except RedditNotFoundError as exc:
            raise SubredditNotFoundError(name) from exc
        except RedditForbiddenError as exc:
            raise SubredditPrivateError(name) from exc
        data = thing.get("data") if isinstance(thing, dict) else None
        if not data:
            raise SubredditNotFoundError(name)
        return data

    # -- SUBREDDIT_POSTS ------------------------------------------------------- #

    async def subreddit_posts(
        self, raw_name: str, *, sort: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """``raw_name`` may be a "+"-joined combined subreddit (e.g.
        "worldnews+politics+India") -- Reddit's own multireddit syntax -- to poll
        several "event subreddits" as one listing/cursor instead of one call each."""

        self._require_configured()
        name = normalize_subreddit_names(raw_name)
        sort_key = (sort or "new").lower()
        if sort_key not in SUBREDDIT_SORTS:
            raise ValidationError(
                f"Unsupported sort '{sort}'", details={"sort": sort, "allowed": sorted(SUBREDDIT_SORTS)}
            )
        page_size = clamp_limit(limit)
        after = decode_cursor(cursor).get("after")

        try:
            listing = await self._client.get_subreddit_listing(name, sort_key, limit=page_size, after=after)
        except RedditNotFoundError as exc:
            raise SubredditNotFoundError(name) from exc
        except RedditForbiddenError as exc:
            raise SubredditPrivateError(name) from exc

        children, next_after = parse_listing(listing)
        items = [normalize_post(child["data"]) for child in children if child.get("kind") == "t3"]
        next_cursor = encode_cursor({"after": next_after}) if next_after else None
        return items, next_cursor

    # -- SUBREDDIT_ACCESS ------------------------------------------------------- #

    async def subreddit_access(self, raw_name: str) -> dict[str, Any]:
        self._require_configured()
        name = normalize_subreddit_name(raw_name)
        try:
            thing = await self._client.get_subreddit_about(name)
        except RedditNotFoundError:
            return {"accessible": False, "state": "not_found", "detail": "subreddit does not exist"}
        except RedditForbiddenError:
            return {
                "accessible": False,
                "state": "private",
                "detail": "Reddit returned 403 for this subreddit; likely private or quarantined",
            }
        except RedditAPIError as exc:
            return {"accessible": False, "state": "unknown", "detail": exc.message}

        data = thing.get("data") if isinstance(thing, dict) else None
        if not data:
            return {"accessible": False, "state": "not_found", "detail": "subreddit does not exist"}

        if data.get("quarantine"):
            return {
                "accessible": False,
                "state": "quarantined",
                "detail": "subreddit is quarantined; requires interactive opt-in this service account cannot complete",
            }

        subreddit_type = data.get("subreddit_type")
        if subreddit_type == "public":
            return {"accessible": True, "state": "public", "detail": "readable"}
        if subreddit_type == "restricted":
            return {
                "accessible": True,
                "state": "restricted",
                "detail": "readable; posting is restricted to approved users",
            }
        if subreddit_type == "private":
            return {
                "accessible": False,
                "state": "private",
                "detail": "private subreddit; not readable without membership",
            }
        return {
            "accessible": False,
            "state": "unknown",
            "detail": f"unrecognized subreddit_type={subreddit_type!r}",
        }

    # -- POST_DETAILS ------------------------------------------------------- #

    async def post_details(self, *, post_id: str | None = None, url: str | None = None) -> dict[str, Any]:
        self._require_configured()
        subreddit_hint = None
        if url:
            ref = classify_reddit_url(url)
            if ref.kind not in ("post", "comment") or not ref.post_id:
                raise ValidationError("url must point to a Reddit post", details={"url": url})
            post_id = ref.post_id
            subreddit_hint = ref.subreddit
        if not post_id:
            raise ValidationError("either post_id or url is required")

        tree = await self._fetch_comments_tree(post_id, subreddit=subreddit_hint)
        post_children, _ = parse_listing(tree[0])
        if not post_children:
            raise PostNotFoundError(post_id)
        return normalize_post(post_children[0]["data"])

    async def _fetch_comments_tree(
        self, post_id: str, *, subreddit: str | None = None, limit: int | None = None
    ) -> list[Any]:
        try:
            return await self._client.get_comments_tree(post_id, subreddit=subreddit, limit=limit)
        except RedditNotFoundError as exc:
            raise PostNotFoundError(post_id) from exc
        except RedditForbiddenError as exc:
            raise PostNotFoundError(post_id) from exc

    # -- POST_COMMENTS ------------------------------------------------------- #

    async def post_comments(
        self, *, post_id: str, limit: int | None = None, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Flattens Reddit's nested comment tree into a flat, cursor-paginated list.

        Known limitation: Reddit's single ``/comments/{id}`` call already truncates
        very large threads behind "more comments" stubs. This method follows up to
        MAX_MORE_EXPANSIONS of those automatically (bounded to protect the rate limit
        and response latency); a handful of the very deepest/largest stubs in an
        exceptionally large thread may still not be expanded. ``parent_id`` is always
        preserved for whatever is returned, so SOC Eye can reconstruct the tree from
        however many comments come back.
        """

        self._require_configured()
        page_size = clamp_limit(limit, default=100, maximum=100)
        offset = int(decode_cursor(cursor).get("offset", 0))

        tree = await self._fetch_comments_tree(post_id)
        post_children, _ = parse_listing(tree[0])
        if not post_children:
            raise PostNotFoundError(post_id)
        post_fullname = f"t3_{post_id}"

        flat, more_ids = self._flatten_comments(tree[1] if len(tree) > 1 else {})

        expansions = 0
        while more_ids and expansions < MAX_MORE_EXPANSIONS and len(flat) < offset + page_size:
            batch, more_ids = more_ids[:MORE_CHILDREN_BATCH], more_ids[MORE_CHILDREN_BATCH:]
            try:
                expanded = await self._client.get_more_children(link_id=post_fullname, children=batch)
            except RedditAPIError:
                break
            expansions += 1
            things = ((expanded.get("json") or {}).get("data") or {}).get("things") or []
            for thing in things:
                if thing.get("kind") == "t1":
                    flat.append(thing.get("data") or {})
                elif thing.get("kind") == "more":
                    more_ids.extend((thing.get("data") or {}).get("children") or [])

        page = flat[offset : offset + page_size]
        items = [normalize_comment(c, post_id=post_id) for c in page]
        next_offset = offset + len(page)
        has_more = next_offset < len(flat) or bool(more_ids)
        next_cursor = encode_cursor({"offset": next_offset}) if has_more else None
        return items, next_cursor

    @staticmethod
    def _flatten_comments(listing: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        flat: list[dict[str, Any]] = []
        more_ids: list[str] = []
        children, _ = parse_listing(listing)
        queue: deque[dict[str, Any]] = deque(children)
        while queue:
            thing = queue.popleft()
            kind = thing.get("kind")
            data = thing.get("data") or {}
            if kind == "t1":
                flat.append(data)
                replies = data.get("replies")
                if isinstance(replies, dict):
                    reply_children, _ = parse_listing(replies)
                    queue.extend(reply_children)
            elif kind == "more":
                more_ids.extend(data.get("children") or [])
        return flat, more_ids

    # -- COMMENT_DETAILS ------------------------------------------------------- #

    async def comment_details(self, comment_id: str) -> dict[str, Any]:
        self._require_configured()
        fullname = f"t1_{comment_id}"
        result = await self._client.get_info(fullnames=[fullname])
        children, _ = parse_listing(result)
        if not children:
            raise CommentNotFoundError(comment_id)
        data = children[0].get("data") or {}
        return normalize_comment(data)

    # -- SEARCH_POSTS ------------------------------------------------------- #

    async def search_posts(
        self,
        q: str | None = None,
        *,
        keywords: list[str] | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        sort: str | None = None,
        time: str | None = None,
        subreddit: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Keyword/event monitoring entry point. ``subreddit`` may be a "+"-joined
        combined subreddit to scope the search to several subreddits at once.
        ``keywords`` is a convenience for the common "any of these terms" case
        (e.g. ["protest", "strike", "curfew"]) -- OR'd together into ``q`` when
        ``q`` itself is not given; pass ``q`` directly for full Reddit search-operator
        control (exact phrases, AND, flair, etc.)."""

        self._require_configured()
        query = q if q and q.strip() else " OR ".join(k.strip() for k in keywords or [] if k.strip())
        if not query.strip():
            raise ValidationError("q or keywords is required")
        sort_key = (sort or "relevance").lower()
        if sort_key not in SEARCH_SORTS:
            raise ValidationError(
                f"Unsupported sort '{sort}'", details={"sort": sort, "allowed": sorted(SEARCH_SORTS)}
            )
        time_key = (time or "all").lower()
        if time_key not in SEARCH_TIMES:
            raise ValidationError(
                f"Unsupported time '{time}'", details={"time": time, "allowed": sorted(SEARCH_TIMES)}
            )
        sub_name = normalize_subreddit_names(subreddit) if subreddit else None
        page_size = clamp_limit(limit)
        after = decode_cursor(cursor).get("after")

        try:
            listing = await self._client.search_posts(
                query, subreddit=sub_name, sort=sort_key, t=time_key, limit=page_size, after=after
            )
        except RedditNotFoundError as exc:
            if sub_name:
                raise SubredditNotFoundError(sub_name) from exc
            raise
        except RedditForbiddenError as exc:
            if sub_name:
                raise SubredditPrivateError(sub_name) from exc
            raise

        children, next_after = parse_listing(listing)
        items = [normalize_post(child["data"]) for child in children if child.get("kind") == "t3"]
        next_cursor = encode_cursor({"after": next_after}) if next_after else None
        return items, next_cursor

    # -- SEARCH_SUBREDDITS ------------------------------------------------------- #

    async def search_subreddits(
        self, q: str | None, *, limit: int | None = None, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._require_configured()
        if not q or not q.strip():
            raise ValidationError("q is required")
        page_size = clamp_limit(limit)
        after = decode_cursor(cursor).get("after")

        listing = await self._client.search_subreddits(q, limit=page_size, after=after)
        children, next_after = parse_listing(listing)
        items = [normalize_subreddit(child["data"]) for child in children if child.get("kind") == "t5"]
        next_cursor = encode_cursor({"after": next_after}) if next_after else None
        return items, next_cursor

    # -- UNIFIED SEARCH ------------------------------------------------------- #

    async def unified_search(
        self,
        *,
        q: str | None = None,
        keywords: list[str] | None = None,
        type_: str | None = "posts",
        limit: int | None = None,
        cursor: str | None = None,
        subreddit: str | None = None,
        time: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        kind = (type_ or "posts").lower()
        if kind == "posts":
            return await self.search_posts(
                q, keywords=keywords, limit=limit, cursor=cursor, time=time, subreddit=subreddit
            )
        if kind == "subreddits":
            return await self.search_subreddits(q, limit=limit, cursor=cursor)
        if kind == "comments":
            raise UnsupportedSearchError(
                "Reddit's public search API does not support free-text search across "
                "all comments (only within one post's own comment tree). Use "
                "POST /api/reddit/post/comments for a specific post instead.",
                details={"type": "comments"},
            )
        raise ValidationError(
            f"Unsupported search type '{type_}'",
            details={"type": type_, "allowed": ["posts", "subreddits"]},
        )

    # -- USER_INFO ------------------------------------------------------- #

    async def user_info(self, raw_username: str) -> dict[str, Any]:
        self._require_configured()
        username = normalize_username(raw_username)
        try:
            thing = await self._client.get_user_about(username)
        except RedditNotFoundError as exc:
            raise UserNotFoundError(username) from exc
        data = thing.get("data") if isinstance(thing, dict) else None
        if not data:
            raise UserNotFoundError(username)
        return normalize_user(data)

    # -- USER_POSTS / USER_COMMENTS ------------------------------------------------------- #

    async def user_posts(
        self, raw_username: str, *, limit: int | None = None, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        return await self._user_listing(
            raw_username, "submitted", limit=limit, cursor=cursor, normalizer=normalize_post, expected_kind="t3"
        )

    async def user_comments(
        self, raw_username: str, *, limit: int | None = None, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        return await self._user_listing(
            raw_username, "comments", limit=limit, cursor=cursor, normalizer=normalize_comment, expected_kind="t1"
        )

    async def _user_listing(
        self,
        raw_username: str,
        kind: str,
        *,
        limit: int | None,
        cursor: str | None,
        normalizer,
        expected_kind: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._require_configured()
        username = normalize_username(raw_username)
        page_size = clamp_limit(limit)
        after = decode_cursor(cursor).get("after")

        try:
            listing = await self._client.get_user_listing(username, kind, limit=page_size, after=after)
        except RedditNotFoundError as exc:
            raise UserNotFoundError(username) from exc

        children, next_after = parse_listing(listing)
        items = [normalizer(child["data"]) for child in children if child.get("kind") == expected_kind]
        next_cursor = encode_cursor({"after": next_after}) if next_after else None
        return items, next_cursor

    # -- RESOLVE ------------------------------------------------------- #

    async def resolve(self, url: str) -> dict[str, Any]:
        self._require_configured()
        ref = classify_reddit_url(url)

        if ref.kind == "subreddit":
            assert ref.subreddit is not None
            subreddit = await self.subreddit_info(ref.subreddit)
            return {"kind": "subreddit", "subreddit": subreddit}

        if ref.kind == "post":
            assert ref.post_id is not None
            post = await self.post_details(post_id=ref.post_id)
            subreddit = await self._safe_subreddit(post.get("subreddit"))
            return {"kind": "post", "subreddit": subreddit, "post": post}

        assert ref.kind == "comment" and ref.comment_id is not None
        comment = await self.comment_details(ref.comment_id)
        post = await self.post_details(post_id=comment.get("post_id") or ref.post_id)
        subreddit = await self._safe_subreddit(post.get("subreddit"))
        return {"kind": "comment", "subreddit": subreddit, "post": post, "comment": comment}

    async def _safe_subreddit(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        try:
            return await self.subreddit_info(name)
        except (SubredditNotFoundError, SubredditPrivateError):
            return None
