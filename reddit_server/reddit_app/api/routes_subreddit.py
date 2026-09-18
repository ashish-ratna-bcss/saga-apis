"""Subreddit endpoints: info, posts (SOC Eye's polling target), and access check."""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.post import PostOut, PostsPage, SubredditPostsRequest
from reddit_app.schemas.subreddit import (
    SubredditAccessRequest,
    SubredditAccessResponse,
    SubredditNameRequest,
    SubredditOut,
)

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


@router.post("/subreddit", response_model=SubredditOut, summary="Resolve subreddit metadata")
async def subreddit_info(payload: SubredditNameRequest, service: ProviderServiceDep) -> SubredditOut:
    return SubredditOut(**await service.subreddit_info(payload.name))


@router.post(
    "/subreddit/posts",
    response_model=PostsPage,
    summary="Posts from a subreddit, or several combined via '+' (SOC Eye polling target)",
)
async def subreddit_posts(payload: SubredditPostsRequest, service: ProviderServiceDep) -> PostsPage:
    items, cursor = await service.subreddit_posts(
        payload.subreddit, sort=payload.sort, limit=payload.limit, cursor=payload.cursor
    )
    return PostsPage(items=[PostOut(**item) for item in items], cursor=cursor)


@router.post(
    "/subreddit/access",
    response_model=SubredditAccessResponse,
    summary="Check whether a subreddit is readable",
)
async def subreddit_access(
    payload: SubredditAccessRequest, service: ProviderServiceDep
) -> SubredditAccessResponse:
    return SubredditAccessResponse(**await service.subreddit_access(payload.subreddit))
