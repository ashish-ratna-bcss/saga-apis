from __future__ import annotations

from fastapi import APIRouter, Depends, status

from bluweb_app.api.deps import get_security_service
from bluweb_app.core.config import Settings, get_settings
from bluweb_app.core.errors import APIError
from bluweb_app.schemas.scrape import ScrapePageResponse, ScrapeRequest, ScrapeResponse
from bluweb_app.services.scrape.stateless import scrape_url
from bluweb_app.services.security.url_security import URLSecurityService

router = APIRouter(prefix="/scrape", tags=["scrape"])


@router.post("", response_model=ScrapeResponse, status_code=status.HTTP_200_OK)
async def scrape(
    body: ScrapeRequest,
    settings: Settings = Depends(get_settings),
    security: URLSecurityService = Depends(get_security_service),
) -> ScrapeResponse:
    """Fetch + extract one URL and return the content in the response.

    Nothing is stored. The caller owns persistence and monitoring.
    """
    result = await scrape_url(
        body.url, settings, security, use_browser=body.use_browser
    )
    if result.status == "failed" and result.page is None:
        raise APIError(
            code="SCRAPE_FAILED",
            message=result.error or "scrape failed",
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    page = None
    if result.page is not None:
        page = ScrapePageResponse(
            url=result.page.url,
            final_url=result.page.final_url,
            http_status=result.page.http_status,
            content_type=result.page.content_type,
            fetch_strategy=result.page.fetch_strategy,
            page_type=result.page.page_type,
            title=result.page.title,
            author=result.page.author,
            published_at=result.page.published_at,
            language=result.page.language,
            content=result.page.content,
            canonical_url=result.page.canonical_url,
            extraction_method=result.page.extraction_method,
            extraction_confidence=result.page.extraction_confidence,
            images=result.page.images,
            tags=result.page.tags,
            error=result.page.error,
            latency_ms=result.page.latency_ms,
        )
    return ScrapeResponse(
        url=result.url,
        status=result.status,
        duration_ms=result.duration_ms,
        page=page,
        error=result.error,
    )
