"""User investigation endpoints: profile, submitted posts, and comments.

Only what Reddit exposes through the authorized public API is returned -- no private
account data (e-mail, verified status, etc.) is available even in an authenticated
session unless Reddit is asked to identify the token's own owner.
"""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.comment import CommentOut, CommentsPage
from reddit_app.schemas.post import PostOut, PostsPage
from reddit_app.schemas.user import UserListingRequest, UserOut, UsernameRequest

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


@router.post("/user", response_model=UserOut, summary="User profile")
async def user_info(payload: UsernameRequest, service: ProviderServiceDep) -> UserOut:
    return UserOut(**await service.user_info(payload.username))


@router.post("/user/posts", response_model=PostsPage, summary="Posts submitted by a user")
async def user_posts(payload: UserListingRequest, service: ProviderServiceDep) -> PostsPage:
    items, cursor = await service.user_posts(payload.username, limit=payload.limit, cursor=payload.cursor)
    return PostsPage(items=[PostOut(**item) for item in items], cursor=cursor)


@router.post("/user/comments", response_model=CommentsPage, summary="Comments made by a user")
async def user_comments(payload: UserListingRequest, service: ProviderServiceDep) -> CommentsPage:
    items, cursor = await service.user_comments(payload.username, limit=payload.limit, cursor=payload.cursor)
    return CommentsPage(items=[CommentOut(**item) for item in items], cursor=cursor)
