"""Public-web discovery via a self-hosted SearxNG instance
(https://docs.searxng.org). SearxNG is a free, open-source meta-search
engine: running your own instance means every query goes out through
SearxNG's own per-engine rate limiting and robots.txt handling rather than
this app scraping a third party's search results page directly.

Configure OSINT_SEARXNG_URL to point at your instance and enable the JSON
output format (search.formats: [html, json] in searxng's settings.yml -- it
is off by default). Unset or unreachable => this adapter reports
`unavailable`, never a faked result (see FAILURE LOOP).
"""
import re
from datetime import UTC, datetime

import httpx

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.config import settings
from osint_app.enums import ClaimType, EntityType
from osint_app.search_client import SearxngUnavailable
from osint_app.search_client import search as searxng_search

FOUND_CONFIDENCE = 0.40  # a search hit is weaker corroboration than a direct account-existence check
MAX_RESULTS_PER_QUERY = 10

_QUOTED_TERM_RE = re.compile(r'"([^"]+)"')


def _relevance_term(query: str) -> str:
    """The literal substring a result must contain to count as a hit.

    Our queries are generated as `"<value>"` or `"<value>" suffix` (see
    phone_adapter.generate_search_queries) -- the required term is what's
    inside the quotes, not the whole query string (a suffix like "whatsapp"
    must not itself be required to appear)."""
    match = _QUOTED_TERM_RE.search(query)
    return (match.group(1) if match else query).lower()


def _is_relevant(term: str, item: dict) -> bool:
    """SearxNG's underlying engines often fall back to loosely-related
    results when an exact phrase has no hits (common for numeric queries --
    a phone number search can come back with unrelated product/tracking
    IDs). Require the literal term to appear somewhere in the result before
    treating it as evidence -- anything else is not a real correlation."""
    haystack = f"{item.get('title') or ''} {item.get('content') or ''} {item.get('url') or ''}".lower()
    return term in haystack


class PublicWebAdapter(SourceAdapter):
    name = "public_web"
    accepts = None  # universal: applicable to every identifier type, see registry.UNIVERSAL_ADAPTERS

    async def is_available(self) -> bool:
        if not settings.searxng_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.searxng_url.rstrip('/')}/healthz")
                if resp.status_code == 404:
                    # older SearxNG builds have no /healthz; a reachable /search is good enough
                    resp = await client.get(f"{settings.searxng_url.rstrip('/')}/search", params={"q": "ping", "format": "json"})
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        return await self.run_queries([f'"{normalized_identifier}"'])

    async def run_queries(self, queries: list[str]) -> list[AdapterResult]:
        """Multi-query entry point -- the orchestrator passes richer query
        sets here (e.g. phone_adapter.generate_search_queries output)."""
        observed_at = datetime.now(UTC)
        results: list[AdapterResult] = []
        seen_urls: set[str] = set()

        try:
            for query in queries:
                term = _relevance_term(query)
                response = await searxng_search(query)
                for item in response.results[:MAX_RESULTS_PER_QUERY]:
                    url = item.url
                    if not url or url in seen_urls:
                        continue
                    if not _is_relevant(term, {"title": item.title, "content": item.snippet, "url": url}):
                        continue
                    seen_urls.add(url)
                    results.append(
                        AdapterResult(
                            source=self.name,
                            query=query,
                            entity_type=EntityType.URL,
                            value=url,
                            status=AdapterStatus.FOUND,
                            claim_type=ClaimType.PUBLIC_ASSOCIATION,
                            confidence=FOUND_CONFIDENCE,
                            observed_at=observed_at,
                            evidence=AdapterEvidence(
                                url=url,
                                title=item.title,
                                # "title" duplicated into metadata: Evidence.raw_metadata is the only
                                # copy that survives storage (AdapterEvidence.title itself isn't
                                # persisted separately), and the pivot engine's extraction reads
                                # title+snippet from raw_metadata.
                                metadata={
                                    "title": item.title,
                                    "snippet": item.snippet,
                                    "engine": item.engine,
                                    "query": query,
                                },
                            ),
                            extraction_method="searxng_search",
                        )
                    )
        except SearxngUnavailable as exc:
            raise SourceUnavailable(str(exc)) from exc

        return results
