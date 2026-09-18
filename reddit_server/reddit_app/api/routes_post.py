"""Post endpoints: single-post lookup and the post's comment tree."""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.comment import CommentOut, CommentsPage, PostCommentsRequest
from reddit_app.schemas.post import PostIdentifierRequest, PostOut

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


@router.post("/post", response_model=PostOut, summary="Post details by post_id or url")
async def post_details(payload: PostIdentifierRequest, service: ProviderServiceDep) -> PostOut:
    return PostOut(**await service.post_details(post_id=payload.post_id, url=payload.url))


@router.post(
    "/post/comments",
    response_model=CommentsPage,
    summary="Comments/replies belonging to a post",
)
async def post_comments(payload: PostCommentsRequest, service: ProviderServiceDep) -> CommentsPage:
    items, cursor = await service.post_comments(
        post_id=payload.post_id, limit=payload.limit, cursor=payload.cursor
    )
    return CommentsPage(items=[CommentOut(**item) for item in items], cursor=cursor)
