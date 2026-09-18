"""Shared SearxNG HTTP client -- the one place that knows how to call the
local SearxNG instance's JSON API (`search: formats: [html, json]` must be
enabled in its settings.yml, see scripts/setup_searxng.sh). Both
`POST/GET /api/v1/search` (routes/search.py) and
`app.adapters.public_web_adapter` (the investigation pipeline's public_web
source) call this -- neither re-implements the request/response handling, so
there is exactly one SearxNG integration to keep working.

OSINT_SEARXNG_ENGINES restricts every query to a curated engine subset
(default: google,bing,brave,wikipedia) -- verified by
scripts/verify_searxng_engines.py to actually return results when
self-hosted; duckduckgo/startpage/mojeek are configured in settings.yml but
CAPTCHA-blocked/parsing-broken/dead respectively as of this writing, so
querying them by default would just waste a request every time.
"""
from dataclasses import dataclass, field

import httpx

from osint_app.config import settings


class SearxngUnavailable(Exception):
    """Raised when SearxNG is unconfigured, unreachable, or returns an
    error -- callers translate this to their own "unavailable" semantics
    (SourceUnavailable for adapters, 503 for the /search route) rather than
    ever fabricating a result."""


@dataclass(frozen=True)
class SearchResultItem:
    title: str
    url: str
    snippet: str
    engine: str
    category: str
    published_at: str | None = None


@dataclass(frozen=True)
class SearchResponse:
    query: str
    results: list[SearchResultItem] = field(default_factory=list)
    total: int = 0
    successful_engines: list[str] = field(default_factory=list)
    failed_engines: list[str] = field(default_factory=list)


async def search(
    query: str,
    *,
    page: int = 1,
    language: str = "en",
    safesearch: int = 0,
    engines: list[str] | None = None,
) -> SearchResponse:
    """Queries the configured SearxNG instance and returns a normalized
    result set. Raises SearxngUnavailable if SEARXNG_URL is unset or the
    request fails -- never returns a faked/empty-but-successful response for
    an actual failure."""
    if not settings.searxng_url:
        raise SearxngUnavailable("OSINT_SEARXNG_URL is not configured (self-host SearxNG, see README)")

    engine_list = settings.searxng_engines_list if engines is None else engines
    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "pageno": page,
        "language": language,
        "safesearch": safesearch,
    }
    if engine_list:
        params["engines"] = ",".join(engine_list)

    base_url = f"{settings.searxng_url.rstrip('/')}/search"
    try:
        async with httpx.AsyncClient(timeout=settings.adapter_timeout_seconds) as client:
            resp = await client.get(base_url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise SearxngUnavailable(f"SearxNG request failed: {exc}") from exc

    results = [
        SearchResultItem(
            title=item.get("title") or "",
            url=item.get("url") or "",
            snippet=item.get("content") or "",
            engine=item.get("engine") or "",
            category=item.get("category") or "general",
            published_at=item.get("publishedDate"),
        )
        for item in payload.get("results", [])
    ]

    # unresponsive_engines entries are [name, error_type] or [name, error_type, suspended_bool]
    failed = sorted({entry[0] for entry in payload.get("unresponsive_engines", []) if entry})
    queried = set(engine_list) if engine_list else {r.engine for r in results} | set(failed)
    successful = sorted(queried - set(failed))

    return SearchResponse(
        query=query, results=results, total=len(results), successful_engines=successful, failed_engines=failed
    )
