"""Comment request/response shapes -- the normalized SOC Eye Comment model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from reddit_app.schemas.author import AuthorOut
from reddit_app.schemas.common import CursorPage


class CommentOut(BaseModel):
    id: str
    post_id: str | None = None
    parent_id: str | None = None
    text: str = ""
    author: AuthorOut | None = None
    created_at: str | None = None
    score: int = 0
    url: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "comment123",
                "post_id": "abc123",
                "parent_id": None,
                "text": "Comment text",
                "author": {"id": "t2_userid", "username": "user2"},
                "created_at": "2026-09-08T11:00:00Z",
                "score": 10,
                "url": "https://www.reddit.com/r/india/comments/abc123/example/comment123/",
            }
        }
    )


class PostCommentsRequest(BaseModel):
    post_id: str
    limit: int | None = Field(default=None, ge=1, le=100)
    cursor: str | None = None


class CommentIdentifierRequest(BaseModel):
    comment_id: str


CommentsPage = CursorPage[CommentOut]
