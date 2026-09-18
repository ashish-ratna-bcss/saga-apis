"""Subreddit request/response shapes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reddit_app.schemas.common import CursorPage

AccessState = Literal["public", "private", "restricted", "quarantined", "not_found", "unknown"]


class SubredditOut(BaseModel):
    id: str | None = None
    name: str
    display_name: str
    title: str = ""
    description: str = ""
    subscribers: int = 0
    url: str | None = None
    public: bool = False
    over18: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "t5_xxxxx",
                "name": "india",
                "display_name": "r/india",
                "title": "India",
                "description": "...",
                "subscribers": 1000000,
                "url": "https://www.reddit.com/r/india/",
                "public": True,
                "over18": False,
            }
        }
    )


class SubredditSummaryOut(BaseModel):
    id: str | None = None
    name: str
    display_name: str
    title: str = ""
    subscribers: int = 0
    url: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "t5_xxx",
                "name": "cybersecurity",
                "display_name": "r/cybersecurity",
                "title": "Cybersecurity",
                "subscribers": 500000,
                "url": "https://www.reddit.com/r/cybersecurity/",
            }
        }
    )


class SubredditNameRequest(BaseModel):
    name: str = Field(..., description="e.g. 'india' or 'r/india'.")


class SubredditAccessRequest(BaseModel):
    subreddit: str = Field(..., description="e.g. 'india' or 'r/india'.")


class SubredditAccessResponse(BaseModel):
    accessible: bool
    state: AccessState
    detail: str


SubredditsPage = CursorPage[SubredditSummaryOut]
