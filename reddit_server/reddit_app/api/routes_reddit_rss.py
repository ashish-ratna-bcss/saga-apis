"""The unauthenticated Reddit RSS transport: keyword/event monitoring without
Reddit OAuth credentials.

Independent of ``routes_search.py``/``routes_subreddit.py`` (the OAuth transport):
these routes never read ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET`` and work
when those are completely unset -- only ``RedditRssService``/``RedditRssClient``
are involved. Reddit authentication is NOT required here; this service's own
``X-API-Key`` application-level protection still applies (see ``app/main.py`` --
this router carries the same ``require_api_key`` dependency as every other
``/api/reddit/*`` router, plus ``rate_limit_rss_client`` -- a per-caller request
budget scoped to this router alone, since it's the one guarding a shared,
process-wide Reddit RSS acquisition path (see ``app/reddit/rate_limiter.py`` and
``app/reddit/feed_cache.py``).

``POST /monitor`` and ``GET /search`` both call ``RedditRssService.monitor`` --
there is deliberately one use-case, not duplicated logic per route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from reddit_app.api.deps import RssServiceDep
from reddit_app.schemas.reddit_rss import (
    RssEventRequest,
    RssEventResponse,
    RssMatchField,
    RssMonitorRequest,
    RssMonitorResponse,
    RssPostOut,
    RssSort,
    RssTimeRange,
)

router = APIRouter(prefix="/api/reddit/rss", tags=["reddit-rss"])


def _to_response(result: dict[str, Any]) -> dict[str, Any]:
    return {**result, "posts": [RssPostOut(**post) for post in result["posts"]]}


@router.post(
    "/monitor",
    response_model=RssMonitorResponse,
    summary="Keyword/event monitoring via Reddit's public RSS (no Reddit credentials)",
)
async def rss_monitor(payload: RssMonitorRequest, service: RssServiceDep) -> RssMonitorResponse:
    result = await service.monitor(
        query=payload.query,
        keywords=payload.keywords,
        strong_keywords=payload.strong_keywords,
        exclude=payload.exclude,
        match_field=payload.match_field,
        min_matches=payload.min_matches,
        subreddits=payload.subreddits,
        sort=payload.sort,
        time_range=payload.time_range,
        from_date=payload.from_date,
        to_date=payload.to_date,
        limit=payload.limit,
    )
    return RssMonitorResponse(**_to_response(result))


@router.get(
    "/search",
    response_model=RssMonitorResponse,
    summary="Convenience GET form of /monitor -- same use-case, no Reddit credentials",
)
async def rss_search(
    service: RssServiceDep,
    q: str | None = Query(default=None, max_length=512),
    keywords: list[str] | None = Query(
        default=None,
        description=(
            "Repeatable, e.g. ?keywords=protest&keywords=strike. "
            "OR'd into a query when q is not given."
        ),
    ),
    strong_keywords: list[str] | None = Query(
        default=None,
        description="Repeatable. Any match sets signal=true regardless of min_matches.",
    ),
    exclude: list[str] | None = Query(
        default=None, description="Repeatable. A post matching any of these is dropped entirely."
    ),
    match_field: RssMatchField = Query(
        default="full",
        description="'title' matches only the post title; 'full' also checks content/flair.",
    ),
    min_matches: int = Query(default=1, ge=1),
    subreddit: str | None = Query(
        default=None,
        description="One subreddit, or several via '+'-joined combined-subreddit syntax.",
    ),
    sort: RssSort = "new",
    time: RssTimeRange = "day",
    from_date: str | None = Query(
        default=None, description="ISO 8601 date/datetime, e.g. 2026-08-01. Applied client-side."
    ),
    to_date: str | None = Query(
        default=None, description="ISO 8601 date/datetime; a bare date means through end of day."
    ),
    limit: int = Query(default=50, ge=1, le=100),
) -> RssMonitorResponse:
    result = await service.monitor(
        query=q,
        keywords=keywords,
        strong_keywords=strong_keywords,
        exclude=exclude,
        match_field=match_field,
        min_matches=min_matches,
        subreddits=subreddit,
        sort=sort,
        time_range=time,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )
    return RssMonitorResponse(**_to_response(result))


@router.post(
    "/event",
    response_model=RssEventResponse,
    summary="Event monitoring: fixed subreddit/keyword strategy + a stateless activity signal",
)
async def rss_event(payload: RssEventRequest, service: RssServiceDep) -> RssEventResponse:
    result = await service.event(
        subreddits=payload.subreddits,
        keywords=payload.keywords,
        strong_keywords=payload.strong_keywords,
        exclude=payload.exclude,
        match_field=payload.match_field,
        min_matches=payload.min_matches,
        time_range=payload.time_range,
        from_date=payload.from_date,
        to_date=payload.to_date,
        limit=payload.limit,
    )
    return RssEventResponse(**_to_response(result))
