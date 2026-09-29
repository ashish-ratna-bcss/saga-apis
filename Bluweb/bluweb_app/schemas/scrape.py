from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ScrapeRequest(BaseModel):
    url: str = Field(..., examples=["https://example.org"])
    # None = auto (browser if JS heuristic says so); True/False force.
    use_browser: bool | None = None


class ScrapePageResponse(BaseModel):
    url: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    fetch_strategy: str
    page_type: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    content: str | None = None
    canonical_url: str | None = None
    extraction_method: str | None = None
    extraction_confidence: float | None = None
    images: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0


class ScrapeResponse(BaseModel):
    url: str
    status: str
    duration_ms: float
    page: ScrapePageResponse | None = None
    error: str | None = None
