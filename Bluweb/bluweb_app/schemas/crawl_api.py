from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CrawlStartRequest(BaseModel):
    url: str = Field(..., examples=["https://example.org"])
    max_pages: int = Field(default=25, ge=1, le=500)
    max_depth: int = Field(default=2, ge=0, le=5)
    same_domain_only: bool = True
    use_browser: bool | None = None
    discover_seeds: bool = Field(
        default=True,
        description="Also seed from sitemap/RSS when available (self-hosted, no paid APIs).",
    )
    wait_seconds: float = Field(
        default=0,
        ge=0,
        le=300,
        description="If >0, hold the HTTP request until done or timeout, then return full result.",
    )


class CrawlPageItem(BaseModel):
    url: str
    depth: int = 0
    status: str | None = None
    error: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    fetch_strategy: str | None = None
    page_type: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    language: str | None = None
    content: str | None = None
    canonical_url: str | None = None
    extraction_method: str | None = None
    extraction_confidence: float | None = None
    images: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    latency_ms: float | None = None
    outbound_links: list[str] = Field(default_factory=list)


class CrawlJobResponse(BaseModel):
    crawl_id: str
    seed_url: str
    status: str
    max_pages: int
    max_depth: int
    same_domain_only: bool
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    statistics: dict[str, Any] = Field(default_factory=dict)
    pages: list[CrawlPageItem] = Field(default_factory=list)
    page_count: int = 0
