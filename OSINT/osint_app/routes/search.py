"""Public search endpoint -- a thin, normalized wrapper around the shared
SearxNG client (app/search_client.py). Deliberately just "the app's search
endpoint", not a distinct "global search" subsystem: the investigation
pipeline's public_web adapter calls the exact same client (see
app/adapters/public_web_adapter.py) -- there is one SearxNG integration,
reachable two ways.
"""
from fastapi import APIRouter, HTTPException, Query

from osint_app.schemas import SearchMetaOut, SearchRequest, SearchResponseOut, SearchResultOut
from osint_app.search_client import SearxngUnavailable
from osint_app.search_client import search as searxng_search

router = APIRouter(prefix="/api/v1/search", tags=["search"])


async def _run_search(query: str, page: int, language: str, safesearch: int) -> SearchResponseOut:
    try:
        response = await searxng_search(query, page=page, language=language, safesearch=safesearch)
    except SearxngUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return SearchResponseOut(
        query=response.query,
        results=[
            SearchResultOut(
                title=r.title, url=r.url, snippet=r.snippet, engine=r.engine,
                category=r.category, published_at=r.published_at,
            )
            for r in response.results
        ],
        meta=SearchMetaOut(
            total=response.total, successful_engines=response.successful_engines, failed_engines=response.failed_engines
        ),
    )


@router.post("", response_model=SearchResponseOut)
async def search_post(payload: SearchRequest):
    return await _run_search(payload.query, payload.page, payload.language, payload.safesearch)


@router.get("", response_model=SearchResponseOut)
async def search_get(
    q: str = Query(min_length=1),
    page: int = Query(default=1, ge=1),
    language: str = "en",
    safesearch: int = Query(default=0, ge=0, le=2),
):
    return await _run_search(q, page, language, safesearch)
