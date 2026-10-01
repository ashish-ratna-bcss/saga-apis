"""Business logic for the Reddit RSS/event-monitoring transport -- this
service's only Reddit transport (no OAuth, no client id/secret anywhere).

Stateless: every call fetches live from Reddit's RSS host and returns a fresh
result. No polling, no persistence, no cross-request deduplication -- a caller
(e.g. SOC Eye) is expected to call ``monitor``/``event``/``user`` on its own
schedule (see INTEGRATION.md).

``monitor()`` is the single use-case both ``POST /api/reddit/rss/monitor`` and
``GET /api/reddit/rss/search`` call -- there is deliberately no separate code path
per route. ``event()`` calls ``monitor()`` too, then layers a stateless
"activity burst" signal on top.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from reddit_app.core.exceptions import RedditRssError, RedditRssInvalidQueryError
from reddit_app.core.logging import get_logger
from reddit_app.reddit.feed_cache import FeedCache, cache_key
from reddit_app.reddit.rss_client import RedditRssClient, RssAccount
from reddit_app.reddit.rss_parser import parse_feed
from reddit_app.reddit.rss_urls import (
    RSS_LISTING_SORTS,
    RSS_SEARCH_SORTS,
    RSS_TIMES,
    RSS_USER_KINDS,
    RSS_USER_SORTS,
    build_listing_url,
    build_search_url,
    build_user_url,
    normalize_rss_subreddits,
    normalize_rss_username,
)

MAX_KEYWORDS = 25
MAX_KEYWORD_LEN = 100

logger = get_logger(__name__)

#: The pasted "event monitoring" strategy's own subreddit/keyword lists -- used as
#: POST /api/reddit/rss/event's defaults when the caller doesn't override them.
DEFAULT_EVENT_SUBREDDITS: list[str] = [
    "worldnews",
    "politics",
    "India",
    "breakingnews",
    "news",
    "geopolitics",
    "CurrentEvents",
]
DEFAULT_EVENT_KEYWORDS: list[str] = [
    "protest",
    "strike",
    "riot",
    "demonstration",
    "march",
    "sit-in",
    "curfew",
    "shutdown",
    "arrest",
    "detained",
    "water cannon",
    "tear gas",
]


def _clean_terms(terms: list[str] | None, *, label: str) -> list[str]:
    """Shared bounds/normalization for keywords, strong_keywords and exclude --
    same limits for all three, since they're the same kind of caller input."""

    cleaned = [t.strip() for t in (terms or []) if t and t.strip()]
    if len(cleaned) > MAX_KEYWORDS:
        raise RedditRssInvalidQueryError(f"Too many {label} (max {MAX_KEYWORDS})")
    for term in cleaned:
        if len(term) > MAX_KEYWORD_LEN:
            raise RedditRssInvalidQueryError(
                f"{label.capitalize()} entry too long (max {MAX_KEYWORD_LEN} chars)",
                details={label: term},
            )
    return cleaned


def _build_query(query: str | None, cleaned_keywords: list[str]) -> str:
    """``query`` wins when given (full Reddit search-operator control); otherwise
    ``keywords`` are OR'd together -- the pasted strategy's own OR syntax."""

    if query and query.strip():
        return query.strip()
    if not cleaned_keywords:
        raise RedditRssInvalidQueryError("query or keywords is required")
    return " OR ".join(cleaned_keywords)


def _build_haystack(post: dict[str, Any], match_field: str) -> str:
    """``match_field="title"`` matches only the title; ``"full"`` (the
    original, still-default behavior) also checks content/flair."""

    fields = [post.get("title")] if match_field == "title" else [
        post.get("title"),
        post.get("content"),
        post.get("flair"),
    ]
    return re.sub(r"\s+", " ", " ".join(filter(None, fields))).lower()


def _match_terms(haystack: str, terms: list[str]) -> list[str]:
    """Case-insensitive, deduplicated, whitespace-normalized substring
    matching. Deterministic, no fuzzy matching, no LLM."""

    matched: list[str] = []
    seen: set[str] = set()
    for term in terms:
        needle = re.sub(r"\s+", " ", term.strip().lower())
        if needle and needle in haystack and needle not in seen:
            seen.add(needle)
            matched.append(needle)
    return matched


def _parse_date_bound(raw: str | None, *, end_of_day: bool) -> datetime | None:
    """``from_date``/``to_date`` -> a UTC ``datetime``, or ``None``. Accepts a bare
    date ("2026-08-01") or a full ISO 8601 timestamp. Reddit's RSS/search API has
    no server-side absolute date-range param (only the relative ``time_range``
    buckets already supported) -- this bound is applied client-side against each
    post's own ``published_at`` after fetching."""

    if not raw or not raw.strip():
        return None
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RedditRssInvalidQueryError(
            f"Invalid date '{raw}'; expected ISO 8601, e.g. '2026-08-01' or '2026-08-01T00:00:00Z'",
            details={"date": raw},
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if end_of_day and len(text) <= 10:
        # a bare "to_date" means through the end of that day, not its first instant
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt.astimezone(UTC)


def _within_date_range(post: dict[str, Any], start: datetime | None, end: datetime | None) -> bool:
    published_raw = post.get("published_at")
    if not published_raw:
        return False  # can't verify this post's date -- exclude rather than assume
    try:
        published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if start and published < start:
        return False
    return not (end and published > end)


def _annotate_and_filter(
    posts: list[dict[str, Any]],
    *,
    cleaned_keywords: list[str],
    cleaned_strong: list[str],
    cleaned_exclude: list[str],
    match_field: str,
    min_matches: int,
    start: datetime | None,
    end: datetime | None,
) -> list[dict[str, Any]]:
    """Shared post-fetch pipeline for every RSS feed shape (search results,
    profile activity, ...): annotate ``matched_keywords``/``signal``, drop
    ``exclude`` matches, dedupe, then apply the client-side date bound. Kept as
    one function so ``monitor()`` and ``user()`` can never drift apart on what
    "signal"/"exclude"/"dedupe" mean."""

    for post in posts:
        haystack = _build_haystack(post, match_field)
        matched = _match_terms(haystack, cleaned_keywords)
        strong_matched = _match_terms(haystack, cleaned_strong)
        post["matched_keywords"] = matched + [k for k in strong_matched if k not in matched]
        post["signal"] = bool(strong_matched) or len(matched) >= min_matches
    if cleaned_exclude:
        posts = [
            post
            for post in posts
            if not _match_terms(_build_haystack(post, match_field), cleaned_exclude)
        ]
    posts = _dedupe(posts)
    if start or end:
        posts = [post for post in posts if _within_date_range(post, start, end)]
    return posts


def _newest_first(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Latest published item first. Missing timestamps sort last."""

    return sorted(posts, key=lambda post: post.get("published_at") or post.get("updated_at") or "", reverse=True)


def _dedupe(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Within-response only, by id/guid/url -- see INTEGRATION.md for why this
    service deliberately does not deduplicate across requests."""

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for post in posts:
        key = post.get("id") or post.get("guid") or post.get("url")
        if key:
            if key in seen:
                continue
            seen.add(key)
        out.append(post)
    return out


class RedditRssService:
    """No caller-visible persistence -- but *feed acquisition* is shared: raw
    feeds are cached/coalesced across every caller through ``feed_cache``
    (a process-wide instance, see ``app/reddit/client.py``), while filtering
    (keywords/exclude/match_field/min_matches/dates) is computed fresh per
    call against whatever feed comes back. See ``INTEGRATION.md`` section 10
    for why this is safe: the cache never sees or stores caller-specific
    options, only the shared, already-built (path, params) Reddit request."""

    def __init__(
        self, client: RedditRssClient, feed_cache: FeedCache, *, event_threshold: int
    ) -> None:
        self._client = client
        self._feed_cache = feed_cache
        self._event_threshold = event_threshold

    async def monitor(
        self,
        *,
        query: str | None = None,
        keywords: list[str] | None = None,
        strong_keywords: list[str] | None = None,
        exclude: list[str] | None = None,
        match_field: str = "full",
        min_matches: int = 1,
        subreddits: str | list[str] | None = None,
        sort: str = "new",
        time_range: str = "all",
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        cleaned_keywords = _clean_terms(keywords, label="keywords")
        cleaned_strong = _clean_terms(strong_keywords, label="strong_keywords")
        cleaned_exclude = _clean_terms(exclude, label="exclude")
        sub_path, sub_list = normalize_rss_subreddits(subreddits)
        explicit_query = bool(query and query.strip())
        built_query = ""
        if explicit_query or cleaned_keywords:
            built_query = _build_query(query, cleaned_keywords)
        start = _parse_date_bound(from_date, end_of_day=False)
        end = _parse_date_bound(to_date, end_of_day=True)
        if start and end and start > end:
            raise RedditRssInvalidQueryError(
                "from_date must be on or before to_date",
                details={"from_date": from_date, "to_date": to_date},
            )
        if match_field not in ("title", "full"):
            raise RedditRssInvalidQueryError(
                f"Unsupported match_field '{match_field}'", details={"allowed": ["title", "full"]}
            )
        if min_matches < 1:
            raise RedditRssInvalidQueryError("min_matches must be at least 1")

        sort_key = (sort or "new").lower()
        if sort_key not in RSS_SEARCH_SORTS:
            raise RedditRssInvalidQueryError(
                f"Unsupported sort '{sort}'",
                details={"sort": sort, "allowed": sorted(RSS_SEARCH_SORTS)},
            )
        time_key = (time_range or "all").lower()
        if time_key not in RSS_TIMES:
            raise RedditRssInvalidQueryError(
                f"Unsupported time_range '{time_range}'",
                details={"time_range": time_range, "allowed": sorted(RSS_TIMES)},
            )

        if not explicit_query and not cleaned_keywords:
            if not sub_path:
                raise RedditRssInvalidQueryError("query or keywords is required")
            listing_sort = sort_key if sort_key in RSS_LISTING_SORTS else "new"
            posts = await self._fetch_listing(sub_path, sort=listing_sort, limit=limit)
        elif (
            not explicit_query
            and len(cleaned_keywords) > 1
            and len(self._client.accounts) > 1
        ):
            posts = await self._fetch_split_keywords(
                cleaned_keywords,
                subreddits=sub_path,
                sort=sort_key,
                time_range=time_key,
                limit=limit,
            )
        else:
            posts = await self._fetch_search(
                built_query,
                subreddits=sub_path,
                sort=sort_key,
                time_range=time_key,
                limit=limit,
            )
        posts = _annotate_and_filter(
            posts,
            cleaned_keywords=cleaned_keywords,
            cleaned_strong=cleaned_strong,
            cleaned_exclude=cleaned_exclude,
            match_field=match_field,
            min_matches=min_matches,
            start=start,
            end=end,
        )

        return {
            "source": "reddit",
            "transport": "rss",
            "authenticated": False,
            "query": built_query,
            "subreddits": sub_list,
            "sort": sort_key,
            "time_range": time_key,
            "from_date": from_date,
            "to_date": to_date,
            "count": len(posts),
            "posts": posts,
        }

    async def _fetch_search(
        self,
        query: str,
        *,
        subreddits: str | None,
        sort: str,
        time_range: str,
        limit: int,
        account: RssAccount | None = None,
    ) -> list[dict[str, Any]]:
        path, params = build_search_url(
            query=query, subreddits=subreddits, sort=sort, time_range=time_range, limit=limit
        )
        raw_xml = await self._feed_cache.get_or_fetch(
            cache_key(path, params),
            lambda: self._client.fetch(path, params, account=account),
        )
        return parse_feed(raw_xml)

    async def _fetch_listing(
        self,
        subreddits: str,
        *,
        sort: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        path, params = build_listing_url(subreddits=subreddits, sort=sort, limit=limit)
        raw_xml = await self._feed_cache.get_or_fetch(
            cache_key(path, params),
            lambda: self._client.fetch(path, params),
        )
        return parse_feed(raw_xml)

    async def _fetch_split_keywords(
        self,
        keywords: list[str],
        *,
        subreddits: str | None,
        sort: str,
        time_range: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Give each RSS account its share of the keywords, wait for every search, merge."""

        accounts = self._client.accounts
        groups: list[list[str]] = [[] for _ in accounts]
        for index, keyword in enumerate(keywords):
            groups[index % len(accounts)].append(keyword)

        async def run(account: RssAccount, group: list[str]) -> list[dict[str, Any]]:
            return await self._fetch_search(
                " OR ".join(group),
                subreddits=subreddits,
                sort=sort,
                time_range=time_range,
                limit=limit,
                account=account,
            )

        results = await asyncio.gather(
            *[run(account, group) for account, group in zip(accounts, groups, strict=True) if group],
            return_exceptions=True,
        )
        posts: list[dict[str, Any]] = []
        errors: list[BaseException] = []
        for result in results:
            if isinstance(result, BaseException):
                errors.append(result)
                logger.warning(
                    "RSS keyword group failed",
                    extra={"error_type": type(result).__name__},
                )
            else:
                posts.extend(result)
        if errors and not posts:
            first = errors[0]
            if isinstance(first, RedditRssError):
                raise first
            raise RedditRssError("Reddit RSS keyword searches failed")
        return posts

    async def user(
        self,
        *,
        username: str,
        kind: str = "overview",
        keywords: list[str] | None = None,
        strong_keywords: list[str] | None = None,
        exclude: list[str] | None = None,
        match_field: str = "full",
        min_matches: int = 1,
        sort: str = "new",
        time_range: str = "all",
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Monitor one redditor's public activity (overview/submitted/comments)
        via Reddit's unauthenticated ``/user/{username}/...rss`` feeds --
        the profile equivalent of ``monitor()``'s subreddit/keyword search.
        ``keywords``/``strong_keywords``/``exclude`` are optional here (unlike
        ``monitor()``, where a search term is mandatory): the username alone is
        already a complete feed to watch, and keywords just narrow it further."""

        cleaned_keywords = _clean_terms(keywords, label="keywords")
        cleaned_strong = _clean_terms(strong_keywords, label="strong_keywords")
        cleaned_exclude = _clean_terms(exclude, label="exclude")
        name = normalize_rss_username(username)
        start = _parse_date_bound(from_date, end_of_day=False)
        end = _parse_date_bound(to_date, end_of_day=True)
        if start and end and start > end:
            raise RedditRssInvalidQueryError(
                "from_date must be on or before to_date",
                details={"from_date": from_date, "to_date": to_date},
            )
        if match_field not in ("title", "full"):
            raise RedditRssInvalidQueryError(
                f"Unsupported match_field '{match_field}'", details={"allowed": ["title", "full"]}
            )
        if min_matches < 1:
            raise RedditRssInvalidQueryError("min_matches must be at least 1")

        kind_key = (kind or "overview").lower()
        if kind_key not in RSS_USER_KINDS:
            raise RedditRssInvalidQueryError(
                f"Unsupported kind '{kind}'",
                details={"kind": kind, "allowed": sorted(RSS_USER_KINDS)},
            )
        sort_key = (sort or "new").lower()
        if sort_key not in RSS_USER_SORTS:
            raise RedditRssInvalidQueryError(
                f"Unsupported sort '{sort}'",
                details={"sort": sort, "allowed": sorted(RSS_USER_SORTS)},
            )
        time_key = (time_range or "all").lower()
        if time_key not in RSS_TIMES:
            raise RedditRssInvalidQueryError(
                f"Unsupported time_range '{time_range}'",
                details={"time_range": time_range, "allowed": sorted(RSS_TIMES)},
            )

        path, params = build_user_url(
            username=name, kind=kind_key, sort=sort_key, time_range=time_key, limit=limit
        )
        # Profile feeds are fetched live on every hit. The shared keyword cache would
        # replay the same posts for up to a minute, and each hit checks out the next
        # RSS account so concurrent BluGate profile calls do not share one quota.
        # The tenant queue waits until Reddit replies; it does not expire the request.
        raw_xml = await self._client.fetch(path, params)
        posts = _newest_first(parse_feed(raw_xml))
        posts = _annotate_and_filter(
            posts,
            cleaned_keywords=cleaned_keywords,
            cleaned_strong=cleaned_strong,
            cleaned_exclude=cleaned_exclude,
            match_field=match_field,
            min_matches=min_matches,
            start=start,
            end=end,
        )

        return {
            "source": "reddit",
            "transport": "rss",
            "authenticated": False,
            "username": name,
            "kind": kind_key,
            "sort": sort_key,
            "time_range": time_key,
            "from_date": from_date,
            "to_date": to_date,
            "count": len(posts),
            "posts": posts,
        }

    async def event(
        self,
        *,
        subreddits: list[str] | None = None,
        keywords: list[str] | None = None,
        strong_keywords: list[str] | None = None,
        exclude: list[str] | None = None,
        match_field: str = "full",
        min_matches: int = 1,
        time_range: str = "day",
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        subs = subreddits if subreddits else DEFAULT_EVENT_SUBREDDITS
        kws = keywords if keywords else DEFAULT_EVENT_KEYWORDS
        result = await self.monitor(
            keywords=kws,
            strong_keywords=strong_keywords,
            exclude=exclude,
            match_field=match_field,
            min_matches=min_matches,
            subreddits=subs,
            sort="new",
            time_range=time_range,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
        posts = result["posts"]
        unique_subreddits = {p["subreddit"] for p in posts if p.get("subreddit")}
        matched_union = sorted({kw for p in posts for kw in p.get("matched_keywords", [])})
        result["event_signal"] = {
            # A stateless activity-burst candidate within this one response --
            # never a confirmed real-world event, and never a trend (no history
            # is kept). See INTEGRATION.md for the terminology rationale.
            "detected": len(posts) >= self._event_threshold,
            "post_count": len(posts),
            "unique_subreddits": len(unique_subreddits),
            "matched_keywords": matched_union,
            "threshold": self._event_threshold,
        }
        return result
