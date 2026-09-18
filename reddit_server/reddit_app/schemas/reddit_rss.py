"""Request/response shapes for the unauthenticated Reddit RSS transport.

Independent of ``app/schemas/post.py`` (the OAuth transport's ``PostOut``): RSS
exposes fewer and differently-shaped fields than Reddit's authenticated API (no
score, no comment count, no upvote ratio -- Atom feeds don't carry them), so this
is its own normalized shape rather than a reuse of ``PostOut``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from reddit_app.services.reddit_rss_service import DEFAULT_EVENT_KEYWORDS, DEFAULT_EVENT_SUBREDDITS

RssSort = Literal["relevance", "hot", "top", "new", "comments"]
RssTimeRange = Literal["hour", "day", "week", "month", "year", "all"]
RssMatchField = Literal["title", "full"]


class RssPostOut(BaseModel):
    id: str | None = None
    guid: str | None = None
    title: str | None = None
    author: str | None = None
    url: str | None = None
    subreddit: str | None = None
    flair: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    content: str | None = None
    source: Literal["reddit"] = "reddit"
    source_type: Literal["rss"] = "rss"
    matched_keywords: list[str] = Field(default_factory=list)
    signal: bool = Field(
        default=False,
        description=(
            "True if any strong_keywords matched, or matched_keywords reached "
            "min_matches. A caller-computed hint, not a hard filter -- every "
            "post that passed 'exclude' is still returned regardless of signal."
        ),
    )


class RssMonitorRequest(BaseModel):
    """``POST /api/reddit/rss/monitor``. Exactly one of ``query``/``keywords``
    must resolve to a non-empty search term."""

    query: str | None = Field(default=None, max_length=512)
    keywords: list[str] = Field(
        default_factory=list,
        max_length=25,
        description="OR'd into a query when 'query' is not given, e.g. ['protest', 'strike'].",
    )
    strong_keywords: list[str] = Field(
        default_factory=list,
        max_length=25,
        description="Any match here sets signal=true regardless of min_matches.",
    )
    exclude: list[str] = Field(
        default_factory=list,
        max_length=25,
        description="A post matching any of these is dropped from the results entirely.",
    )
    match_field: RssMatchField = Field(
        default="full",
        description="'title' matches only the post title; 'full' also checks content/flair.",
    )
    min_matches: int = Field(
        default=1,
        ge=1,
        description="Minimum matched_keywords count (excluding strong_keywords) for signal=true.",
    )
    subreddits: list[str] = Field(
        default_factory=list,
        max_length=25,
        description="Empty means global search across all of Reddit.",
    )
    sort: RssSort = "new"
    time_range: RssTimeRange = "day"
    from_date: str | None = Field(
        default=None,
        description=(
            "ISO 8601 date/datetime (e.g. '2026-08-01'). Applied client-side against "
            "each post's published_at -- Reddit's RSS API has no server-side absolute "
            "date-range param, only the relative time_range buckets above."
        ),
    )
    to_date: str | None = Field(
        default=None,
        description="ISO 8601 date/datetime; a bare date means through the end of that day.",
    )
    limit: int = Field(default=50, ge=1, le=100)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "keywords": ["protest", "strike", "curfew"],
                "subreddits": ["India", "worldnews", "politics"],
                "sort": "new",
                "time_range": "day",
                "limit": 100,
            }
        }
    )


class RssEventRequest(BaseModel):
    """``POST /api/reddit/rss/event``. Defaults to the pasted "event
    monitoring" strategy's own subreddit/keyword lists; override either to
    monitor a different set."""

    subreddits: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EVENT_SUBREDDITS), max_length=25
    )
    keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_EVENT_KEYWORDS), max_length=25)
    strong_keywords: list[str] = Field(default_factory=list, max_length=25)
    exclude: list[str] = Field(default_factory=list, max_length=25)
    match_field: RssMatchField = "full"
    min_matches: int = Field(default=1, ge=1)
    time_range: RssTimeRange = "day"
    from_date: str | None = Field(
        default=None, description="ISO 8601 date/datetime, applied client-side."
    )
    to_date: str | None = Field(
        default=None, description="ISO 8601 date/datetime, applied client-side."
    )
    limit: int = Field(default=100, ge=1, le=100)


class EventSignalOut(BaseModel):
    """A stateless, single-response activity signal -- never a claim of a
    confirmed real-world event, and never a trend (no history is kept)."""

    detected: bool
    post_count: int
    unique_subreddits: int
    matched_keywords: list[str]
    threshold: int


class RssMonitorResponse(BaseModel):
    source: Literal["reddit"] = "reddit"
    transport: Literal["rss"] = "rss"
    authenticated: Literal[False] = False
    query: str
    subreddits: list[str]
    sort: str
    time_range: str
    from_date: str | None = None
    to_date: str | None = None
    count: int
    posts: list[RssPostOut]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "source": "reddit",
                "transport": "rss",
                "authenticated": False,
                "query": "protest OR strike OR curfew",
                "subreddits": ["India", "worldnews"],
                "sort": "new",
                "time_range": "day",
                "count": 1,
                "posts": [
                    {
                        "id": "abc123",
                        "guid": "t3_abc123",
                        "title": "Example title",
                        "author": "someuser",
                        "url": "https://www.reddit.com/r/India/comments/abc123/example/",
                        "subreddit": "India",
                        "flair": None,
                        "published_at": "2026-09-10T08:20:00Z",
                        "updated_at": "2026-09-10T08:20:00Z",
                        "content": "post description",
                        "source": "reddit",
                        "source_type": "rss",
                        "matched_keywords": ["protest"],
                    }
                ],
            }
        }
    )


class RssEventResponse(RssMonitorResponse):
    event_signal: EventSignalOut
