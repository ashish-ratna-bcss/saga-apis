from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status

from bluweb_app.api.deps import get_security_service
from bluweb_app.core.config import Settings, get_settings
from bluweb_app.core.errors import APIError
from bluweb_app.schemas.crawl_api import CrawlJobResponse, CrawlPageItem, CrawlStartRequest
from bluweb_app.services.scrape.crawl import start_crawl_task
from bluweb_app.services.scrape.jobs import CrawlJob, create_job, get_job
from bluweb_app.services.security.url_security import URLSecurityError, URLSecurityService

router = APIRouter(prefix="/crawl", tags=["crawl"])


@router.post("", response_model=CrawlJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_crawl(
    body: CrawlStartRequest,
    settings: Settings = Depends(get_settings),
    security: URLSecurityService = Depends(get_security_service),
) -> CrawlJobResponse:
    """Multi-page self-hosted crawl. Returns extracted pages for the caller to store.

    Uses only open-source fetch/extract (httpx + Playwright). No paid scrape APIs.
    Jobs are in-memory on this instance (TTL ~1h after completion).
    """
    try:
        hostname = security.validate_scheme(body.url)
        await security.resolve_and_validate(hostname)
    except URLSecurityError as exc:
        from bluweb_app.core.url_errors import url_security_to_api_error

        raise url_security_to_api_error(exc) from exc

    job = await create_job(
        seed_url=body.url,
        max_pages=body.max_pages,
        max_depth=body.max_depth,
        same_domain_only=body.same_domain_only,
        use_browser=body.use_browser,
        discover_seeds=body.discover_seeds,
    )
    start_crawl_task(job.crawl_id, settings, security)

    if body.wait_seconds > 0:
        deadline = asyncio.get_event_loop().time() + body.wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            refreshed = await get_job(job.crawl_id)
            if refreshed and refreshed.status in ("completed", "failed", "cancelled"):
                return _to_response(refreshed)
            await asyncio.sleep(0.5)
        refreshed = await get_job(job.crawl_id)
        return _to_response(refreshed or job)

    return _to_response(job)


@router.get("/{crawl_id}", response_model=CrawlJobResponse)
async def get_crawl(crawl_id: str) -> CrawlJobResponse:
    job = await get_job(crawl_id)
    if job is None:
        raise APIError(
            code="CRAWL_NOT_FOUND",
            message=f"No crawl job found for id {crawl_id}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return _to_response(job)


@router.post("/{crawl_id}/cancel", response_model=CrawlJobResponse)
async def cancel_crawl(crawl_id: str) -> CrawlJobResponse:
    job = await get_job(crawl_id)
    if job is None:
        raise APIError(
            code="CRAWL_NOT_FOUND",
            message=f"No crawl job found for id {crawl_id}",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    if job.status in ("queued", "running"):
        job.cancel_requested = True
    return _to_response(job)


def _ts(value: float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _to_response(job: CrawlJob) -> CrawlJobResponse:
    pages = [CrawlPageItem.model_validate(p) for p in job.pages]
    return CrawlJobResponse(
        crawl_id=job.crawl_id,
        seed_url=job.seed_url,
        status=job.status,
        max_pages=job.max_pages,
        max_depth=job.max_depth,
        same_domain_only=job.same_domain_only,
        created_at=_ts(job.created_at),
        started_at=_ts(job.started_at),
        completed_at=_ts(job.completed_at),
        error=job.error,
        statistics=job.statistics,
        pages=pages,
        page_count=len(pages),
    )
