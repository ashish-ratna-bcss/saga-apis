"""Self-hosted multi-page crawl: HTTP + optional Playwright, BFS frontier.

No paid proxies/CAPTCHA APIs. No long-term storage — pages accumulate on the
in-memory job and the caller copies them out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from bluweb_app.core.config import Settings
from bluweb_app.services.discovery.seed_discovery import discover_extra_seeds
from bluweb_app.services.normalization.url_normalizer import (
    canonicalize_for_frontier,
    registrable_domain,
)
from bluweb_app.services.scrape.jobs import CrawlJob, get_job
from bluweb_app.services.scrape.stateless import scrape_url
from bluweb_app.services.security.url_security import URLSecurityError, URLSecurityService

logger = logging.getLogger("webintel.stateless_crawl")

_HARD_MAX_PAGES = 500
_HARD_MAX_DEPTH = 5
_HARD_MAX_DISCOVERED = 5000


async def run_crawl_job(
    crawl_id: str,
    settings: Settings,
    security: URLSecurityService,
) -> None:
    job = await get_job(crawl_id)
    if job is None:
        return

    job.status = "running"
    job.started_at = time.time()
    start = time.time()
    stats = {
        "pages_discovered": 0,
        "pages_fetched": 0,
        "pages_failed": 0,
        "pages_extracted": 0,
        "http_pages": 0,
        "browser_pages": 0,
        "seeds_from_sitemap_feed": 0,
    }

    try:
        max_pages = max(1, min(job.max_pages, _HARD_MAX_PAGES))
        max_depth = max(0, min(job.max_depth, _HARD_MAX_DEPTH))
        seed_domain = registrable_domain(job.seed_url)

        hostname = security.validate_scheme(job.seed_url)
        await security.resolve_and_validate(hostname)

        frontier: deque[tuple[str, int]] = deque()
        seen: set[str] = set()

        def enqueue(url: str, depth: int) -> None:
            if len(seen) >= _HARD_MAX_DISCOVERED:
                return
            if depth > max_depth:
                return
            try:
                security.validate_scheme(url)
            except URLSecurityError:
                return
            if job.same_domain_only and registrable_domain(url) != seed_domain:
                return
            key = canonicalize_for_frontier(url)
            if key in seen:
                return
            seen.add(key)
            frontier.append((url, depth))
            stats["pages_discovered"] = len(seen)

        enqueue(job.seed_url, 0)

        if job.discover_seeds:
            try:
                discovery = await discover_extra_seeds(job.seed_url, settings, security)
                for u in discovery.urls:
                    enqueue(u, 0)
                stats["seeds_from_sitemap_feed"] = (
                    discovery.sitemap_url_count + discovery.feed_url_count
                )
            except Exception:  # noqa: BLE001
                logger.warning("seed discovery failed for %s", job.seed_url, exc_info=True)

        while frontier and len(job.pages) < max_pages:
            if job.cancel_requested:
                job.status = "cancelled"
                break

            url, depth = frontier.popleft()
            result = await scrape_url(
                url, settings, security, use_browser=job.use_browser
            )
            page = result.page
            outbound = list(page.outbound_links) if page else []
            page_dict = {
                "url": url,
                "depth": depth,
                "status": result.status,
                "error": result.error or (page.error if page else None),
                "final_url": page.final_url if page else None,
                "http_status": page.http_status if page else None,
                "content_type": page.content_type if page else None,
                "fetch_strategy": page.fetch_strategy if page else None,
                "page_type": page.page_type if page else None,
                "title": page.title if page else None,
                "author": page.author if page else None,
                "published_at": page.published_at.isoformat() if page and page.published_at else None,
                "language": page.language if page else None,
                "content": page.content if page else None,
                "canonical_url": page.canonical_url if page else None,
                "extraction_method": page.extraction_method if page else None,
                "extraction_confidence": page.extraction_confidence if page else None,
                "images": page.images if page else [],
                "tags": page.tags if page else [],
                "latency_ms": page.latency_ms if page else result.duration_ms,
                "outbound_links": outbound[:50],
            }

            if result.status == "completed" and page and page.content:
                stats["pages_extracted"] += 1
            if result.status == "failed" or (page and page.error and not page.content):
                stats["pages_failed"] += 1
            else:
                stats["pages_fetched"] += 1
            if page and page.fetch_strategy == "browser":
                stats["browser_pages"] += 1
            elif page:
                stats["http_pages"] += 1

            if depth < max_depth:
                for link in outbound:
                    if len(seen) >= _HARD_MAX_DISCOVERED:
                        break
                    enqueue(link, depth + 1)

            job.pages.append(page_dict)
            job.statistics = dict(stats)

        if job.status == "running":
            job.status = "completed"
    except Exception as exc:  # noqa: BLE001
        logger.exception("crawl %s failed", crawl_id)
        job.status = "failed"
        job.error = str(exc)
    finally:
        job.completed_at = time.time()
        job.statistics = {
            **stats,
            "duration_ms": (time.time() - start) * 1000,
            "pages_returned": len(job.pages),
        }


def start_crawl_task(
    crawl_id: str, settings: Settings, security: URLSecurityService
) -> asyncio.Task:
    return asyncio.create_task(run_crawl_job(crawl_id, settings, security))
