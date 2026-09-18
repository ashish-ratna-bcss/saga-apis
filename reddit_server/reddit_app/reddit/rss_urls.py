"""URL construction for Reddit's public, unauthenticated RSS/Atom endpoints.

Independent of ``app/reddit/rest_client.py``'s OAuth URL construction -- different
host, no token, no per-token rate-limit bucket, no coupling to OAuth's request
path. Subreddit-*name* validation is the one thing reused from
``app.reddit.urls``: it is pure string validation with zero HTTP/OAuth
involvement, so a subreddit name is a subreddit name regardless of which
transport eventually reads it.

SSRF note: every function here builds a path/query from validated, bounded
inputs (subreddit names, an enum sort, an enum time range, a clamped limit).
Callers never supply a URL, host, scheme or path -- see ``rss_client.py``,
which is hardcoded to Reddit's own RSS host.
"""

from __future__ import annotations

from urllib.parse import quote

from reddit_app.core.exceptions import RedditRssInvalidQueryError, RedditRssInvalidSubredditError
from reddit_app.core.exceptions import ValidationError as _ValidationError
from reddit_app.reddit.urls import normalize_subreddit_names

#: Valid ``sort`` values for Reddit's search.rss (mirrors the OAuth search API).
RSS_SEARCH_SORTS = frozenset({"relevance", "hot", "top", "new", "comments"})
#: Valid ``sort`` path segments for a bare subreddit-listing .rss feed.
RSS_LISTING_SORTS = frozenset({"new", "hot", "top", "rising", "controversial"})
#: Valid ``t`` (time filter) values, search.rss only -- listings don't take one.
RSS_TIMES = frozenset({"hour", "day", "week", "month", "year", "all"})

MAX_SUBREDDITS = 25
MAX_QUERY_LEN = 512
MAX_RSS_LIMIT = 100


def normalize_rss_subreddits(subreddits: str | list[str] | None) -> tuple[str | None, list[str]]:
    """Accept a single "+"-joined string or a list of subreddit names.

    Returns ``(combined, expanded)``: ``combined`` is the "+"-joined string to embed
    literally in the URL path (Reddit's multireddit separator), ``expanded`` is the
    per-name list for echoing back in a response body. ``None``/empty means "no
    subreddit scope" (global search) -> ``(None, [])``.
    """

    if subreddits is None:
        return None, []
    if isinstance(subreddits, str):
        raw = subreddits
    else:
        if len(subreddits) > MAX_SUBREDDITS:
            raise RedditRssInvalidSubredditError(
                f"Too many subreddits (max {MAX_SUBREDDITS})", details={"count": len(subreddits)}
            )
        raw = "+".join(subreddits)
    if not raw.strip():
        return None, []
    try:
        combined = normalize_subreddit_names(raw)
    except _ValidationError as exc:
        raise RedditRssInvalidSubredditError(exc.message, details=exc.details) from exc
    names = combined.split("+")
    if len(names) > MAX_SUBREDDITS:
        raise RedditRssInvalidSubredditError(
            f"Too many subreddits (max {MAX_SUBREDDITS})", details={"name": raw}
        )
    return combined, names


def build_search_url(
    *, query: str, subreddits: str | None, sort: str, time_range: str, limit: int
) -> tuple[str, dict[str, str]]:
    """``GET /search.rss`` or ``GET /r/{subs}/search.rss`` -- returns
    ``(path, params)`` for the RSS client to request; never a full URL string, so
    there is no manual concatenation to get wrong."""

    if not query or not query.strip():
        raise RedditRssInvalidQueryError("query is required")
    if len(query) > MAX_QUERY_LEN:
        raise RedditRssInvalidQueryError(f"query exceeds {MAX_QUERY_LEN} characters")
    if sort not in RSS_SEARCH_SORTS:
        raise RedditRssInvalidQueryError(
            f"Unsupported sort '{sort}'",
            details={"sort": sort, "allowed": sorted(RSS_SEARCH_SORTS)},
        )
    if time_range not in RSS_TIMES:
        raise RedditRssInvalidQueryError(
            f"Unsupported time_range '{time_range}'",
            details={"time_range": time_range, "allowed": sorted(RSS_TIMES)},
        )
    params = {
        "q": query,
        "sort": sort,
        "t": time_range,
        "limit": str(max(1, min(int(limit), MAX_RSS_LIMIT))),
    }
    if subreddits:
        params["restrict_sr"] = "1"
        path = f"/r/{quote(subreddits, safe='+')}/search.rss"
    else:
        path = "/search.rss"
    return path, params


def build_listing_url(*, subreddits: str, sort: str, limit: int) -> tuple[str, dict[str, str]]:
    """``GET /r/{subs}/{sort}.rss`` -- a plain (non-search) subreddit listing feed,
    e.g. for polling a fixed set of "event subreddits" with no keyword filter."""

    if not subreddits or not subreddits.strip():
        raise RedditRssInvalidSubredditError("subreddits is required")
    if sort not in RSS_LISTING_SORTS:
        raise RedditRssInvalidQueryError(
            f"Unsupported sort '{sort}'",
            details={"sort": sort, "allowed": sorted(RSS_LISTING_SORTS)},
        )
    path = f"/r/{quote(subreddits, safe='+')}/{sort}.rss"
    params = {"limit": str(max(1, min(int(limit), MAX_RSS_LIMIT)))}
    return path, params
