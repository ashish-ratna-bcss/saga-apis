"""Stateless single-URL scrape: fetch + classify + extract, return in-process.

No Postgres, no MinIO, no job queue. Callers persist whatever they need.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from bluweb_app.core.config import Settings
from bluweb_app.services.crawling.fetchers import PageFetchResult, browser_fetch_page, http_fetch_page
from bluweb_app.services.extraction.extraction_router import extract_for_page
from bluweb_app.services.preflight.html import analyze_html
from bluweb_app.services.preflight.javascript import assess_javascript_dependency
from bluweb_app.services.security.url_security import URLSecurityError, URLSecurityService


@dataclass
class ScrapePageResult:
    url: str
    final_url: str | None
    http_status: int | None
    content_type: str | None
    fetch_strategy: str
    page_type: str | None
    title: str | None
    author: str | None
    published_at: datetime | None
    language: str | None
    content: str | None
    canonical_url: str | None
    extraction_method: str | None
    extraction_confidence: float | None
    images: list[str]
    tags: list[str]
    error: str | None
    latency_ms: float
    outbound_links: list[str] = field(default_factory=list)


@dataclass
class ScrapeResult:
    url: str
    status: str
    duration_ms: float
    page: ScrapePageResult | None
    error: str | None = None


async def scrape_url(
    url: str,
    settings: Settings,
    security: URLSecurityService,
    *,
    use_browser: bool | None = None,
) -> ScrapeResult:
    """Fetch one URL and return extracted content. Never writes to disk/DB."""
    start = time.monotonic()
    try:
        hostname = security.validate_scheme(url)
        await security.resolve_and_validate(hostname)
    except URLSecurityError as exc:
        return ScrapeResult(
            url=url,
            status="failed",
            duration_ms=(time.monotonic() - start) * 1000,
            page=None,
            error=str(exc),
        )

    http_result = await http_fetch_page(
        url, settings, security, max_bytes=settings.crawl_max_response_bytes
    )
    if not http_result.success:
        return ScrapeResult(
            url=url,
            status="failed",
            duration_ms=(time.monotonic() - start) * 1000,
            page=_failed_page(url, http_result, "http"),
            error=http_result.error,
        )

    strategy = "http"
    fetch = http_result
    html = http_result.html

    want_browser = use_browser
    if want_browser is None and html:
        analysis = analyze_html(html, http_result.final_url or url)
        js = assess_javascript_dependency(html, analysis)
        want_browser = bool(js.likely_requires_browser)

    if want_browser:
        browser_result = await browser_fetch_page(url, settings, security)
        if browser_result.success and browser_result.html:
            fetch = browser_result
            html = browser_result.html
            strategy = "browser"

    if not html:
        # Non-HTML (e.g. PDF bytes) — return raw metadata only; no extract.
        return ScrapeResult(
            url=url,
            status="completed",
            duration_ms=(time.monotonic() - start) * 1000,
            page=ScrapePageResult(
                url=url,
                final_url=fetch.final_url,
                http_status=fetch.status_code,
                content_type=fetch.content_type,
                fetch_strategy=strategy,
                page_type=None,
                title=None,
                author=None,
                published_at=None,
                language=None,
                content=None,
                canonical_url=None,
                extraction_method=None,
                extraction_confidence=None,
                images=[],
                tags=[],
                error="non-html response; content not extracted",
                latency_ms=fetch.latency_ms,
            ),
        )

    final_url = fetch.final_url or url
    analysis = analyze_html(html, final_url)
    extraction = await extract_for_page(
        final_url, html, html_analysis=analysis, is_seed_homepage=True
    )
    doc = extraction.document
    page_type = extraction.classification.page_type.value if extraction.classification else None
    outbound = list(analysis.internal_links or [])
    if doc and isinstance(doc.raw_metadata, dict):
        for item in (doc.raw_metadata.get("listings") or [])[:50]:
            if isinstance(item, dict) and item.get("url"):
                outbound.append(item["url"])

    return ScrapeResult(
        url=url,
        status="completed",
        duration_ms=(time.monotonic() - start) * 1000,
        page=ScrapePageResult(
            url=url,
            final_url=final_url,
            http_status=fetch.status_code,
            content_type=fetch.content_type,
            fetch_strategy=strategy,
            page_type=page_type,
            title=(doc.headline if doc else None),
            author=(doc.author if doc else None),
            published_at=(doc.published_at if doc else None),
            language=(doc.language if doc else None),
            content=(doc.body if doc else None),
            canonical_url=(doc.canonical_url if doc else None),
            extraction_method=(doc.extractor if doc else None),
            extraction_confidence=(doc.confidence if doc else None),
            images=(list(doc.images) if doc else []),
            tags=(list(doc.tags) if doc else []),
            error=None if doc else "extraction produced no body",
            latency_ms=fetch.latency_ms,
            outbound_links=outbound,
        ),
    )


def _failed_page(url: str, fetch: PageFetchResult, strategy: str) -> ScrapePageResult:
    return ScrapePageResult(
        url=url,
        final_url=fetch.final_url,
        http_status=fetch.status_code,
        content_type=fetch.content_type,
        fetch_strategy=strategy,
        page_type=None,
        title=None,
        author=None,
        published_at=None,
        language=None,
        content=None,
        canonical_url=None,
        extraction_method=None,
        extraction_confidence=None,
        images=[],
        tags=[],
        error=fetch.error,
        latency_ms=fetch.latency_ms,
    )
