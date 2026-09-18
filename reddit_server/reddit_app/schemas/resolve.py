"""POST /api/reddit/resolve request/response shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from reddit_app.schemas.comment import CommentOut
from reddit_app.schemas.post import PostOut
from reddit_app.schemas.subreddit import SubredditOut


class ResolveRequest(BaseModel):
    url: str


class ResolveResponse(BaseModel):
    """One shape covers all three kinds; unused fields are omitted by the route,
    not nulled out, to keep responses minimal (see routes_resolve.py)."""

    kind: Literal["subreddit", "post", "comment"]
    subreddit: SubredditOut | None = None
    post: PostOut | None = None
    comment: CommentOut | None = None
