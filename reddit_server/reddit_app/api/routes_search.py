"""Global search endpoints: public post discovery, subreddit discovery, and the
unified SEARCH abstraction.

Reddit's public search API covers posts and subreddits; it does not offer free-text
search across arbitrary comments (only within one post's own comment tree, via
POST /api/reddit/post/comments). SEARCH_POSTS/SEARCH therefore never claim
comment-wide search -- ``type=comments`` on the unified endpoint returns a structured
UNSUPPORTED_SEARCH error rather than fabricating results (see provider_service.py).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.post import PostOut, PostsPage
from reddit_app.schemas.subreddit import SubredditsPage, SubredditSummaryOut

router = APIRouter(prefix="/api/reddit", tags=["reddit"])

SearchSort = Literal["relevance", "hot", "top", "new", "comments"]
SearchTime = Literal["hour", "day", "week", "month", "year", "all"]
SearchType = Literal["posts", "subreddits"]


@router.get(
    "/search/posts",
    response_model=PostsPage,
    summary="Global public post discovery / keyword+event monitoring",
)
async def search_posts(
    service: ProviderServiceDep,
    q: str | None = Query(default=None, min_length=1, max_length=512),
    keywords: list[str] | None = Query(
        default=None,
        description=(
            "Convenience for 'any of these terms' monitoring, e.g. "
            "?keywords=protest&keywords=strike&keywords=curfew. OR'd together into a "
            "single query when q is not given. Ignored if q is set."
        ),
    ),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    sort: SearchSort = "relevance",
    time: SearchTime = "all",
    subreddit: str | None = Query(
        default=None,
        description=(
            "Restrict the search to one subreddit, or several via Reddit's "
            "'+'-joined combined-subreddit syntax, e.g. 'india+worldnews+politics'."
        ),
    ),
) -> PostsPage:
    items, next_cursor = await service.search_posts(
        q, keywords=keywords, limit=limit, cursor=cursor, sort=sort, time=time, subreddit=subreddit
    )
    return PostsPage(items=[PostOut(**item) for item in items], cursor=next_cursor)


@router.get(
    "/search/subreddits", response_model=SubredditsPage, summary="Discover subreddits by keyword"
)
async def search_subreddits(
    service: ProviderServiceDep,
    q: str = Query(..., min_length=1, max_length=512),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
) -> SubredditsPage:
    items, next_cursor = await service.search_subreddits(q, limit=limit, cursor=cursor)
    return SubredditsPage(items=[SubredditSummaryOut(**item) for item in items], cursor=next_cursor)


@router.get(
    "/search",
    response_model=PostsPage | SubredditsPage,
    summary="Unified search abstraction (type=posts|subreddits)",
    description=(
        "Stable SOC Eye search entry point. type=posts and type=subreddits are "
        "supported today; type=comments returns an UNSUPPORTED_SEARCH error because "
        "Reddit's public API has no free-text search across all comments."
    ),
)
async def unified_search(
    service: ProviderServiceDep,
    q: str | None = Query(default=None, min_length=1, max_length=512),
    keywords: list[str] | None = Query(
        default=None,
        description=(
            "Convenience for 'any of these terms' monitoring; OR'd into q when q is not given."
        ),
    ),
    type: SearchType | Literal["comments"] = "posts",  # noqa: A002 - matches the SOC Eye contract
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = None,
    subreddit: str | None = Query(
        default=None,
        description="One subreddit, or several via '+'-joined combined-subreddit syntax.",
    ),
    time: SearchTime = "all",
) -> PostsPage | SubredditsPage:
    items, next_cursor = await service.unified_search(
        q=q,
        keywords=keywords,
        type_=type,
        limit=limit,
        cursor=cursor,
        subreddit=subreddit,
        time=time,
    )
    if type == "subreddits":
        return SubredditsPage(items=[SubredditSummaryOut(**item) for item in items], cursor=next_cursor)
    return PostsPage(items=[PostOut(**item) for item in items], cursor=next_cursor)
