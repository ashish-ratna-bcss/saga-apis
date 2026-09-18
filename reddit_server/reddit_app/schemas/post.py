"""Post request/response shapes -- the normalized SOC Eye Post model."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from reddit_app.schemas.author import AuthorOut
from reddit_app.schemas.common import CursorPage

SubredditSort = Literal["new", "hot", "top", "rising", "controversial"]


class PostOut(BaseModel):
    id: str
    subreddit: str | None = None
    title: str
    text: str = ""
    author: AuthorOut | None = None
    created_at: str | None = None
    url: str | None = None
    score: int = 0
    upvote_ratio: float | None = None
    num_comments: int = 0
    media: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "abc123",
                "subreddit": "india",
                "title": "Example title",
                "text": "Post body",
                "author": {"id": "t2_userid", "username": "user1"},
                "created_at": "2026-09-08T10:00:00Z",
                "url": "https://www.reddit.com/r/india/comments/abc123/example/",
                "score": 123,
                "upvote_ratio": 0.95,
                "num_comments": 25,
                "media": [],
            }
        }
    )


class PostIdentifierRequest(BaseModel):
    """POST /api/reddit/post -- exactly one of post_id or url."""

    post_id: str | None = None
    url: str | None = None


class SubredditPostsRequest(BaseModel):
    subreddit: str = Field(
        ...,
        description=(
            "One subreddit, or several via Reddit's '+'-joined combined-subreddit "
            "syntax (e.g. 'worldnews+politics+India') to poll a fixed set of "
            "'event subreddits' as a single listing/cursor."
        ),
    )
    sort: SubredditSort = "new"
    limit: int | None = Field(default=None, ge=1, le=100)
    cursor: str | None = None


PostsPage = CursorPage[PostOut]
