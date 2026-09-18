import httpx
import pytest

from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.adapters.public_web_adapter import PublicWebAdapter, _is_relevant, _relevance_term
from osint_app.enums import ClaimType


@pytest.mark.asyncio
async def test_unavailable_when_not_configured(monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "searxng_url", None)
    adapter = PublicWebAdapter()

    assert await adapter.is_available() is False
    with pytest.raises(SourceUnavailable):
        await adapter.run_queries(["anything"])


@pytest.mark.asyncio
async def test_run_queries_maps_results_and_dedupes(monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "searxng_url", "http://fake-searxng.local")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.com/q1", "title": "q1 profile", "content": "snippet a", "engine": "duckduckgo"},
                    {"url": "https://example.com/q1", "title": "q1 dup", "content": "dup", "engine": "bing"},
                ]
            },
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("osint_app.search_client.httpx.AsyncClient", client_factory)

    adapter = PublicWebAdapter()
    results = await adapter.run_queries(['"q1"', '"q2"'])

    assert len(results) == 1  # deduped across both queries
    result = results[0]
    assert result.value == "https://example.com/q1"
    assert result.status == AdapterStatus.FOUND
    assert result.claim_type == ClaimType.PUBLIC_ASSOCIATION


@pytest.mark.asyncio
async def test_run_queries_filters_out_irrelevant_results(monkeypatch):
    """Regression test for a real bug: SearxNG's underlying engines fall back
    to loosely-related results (e.g. unrelated product/tracking-number pages)
    when an exact numeric phrase has no hits. A result that doesn't actually
    contain the searched term must never be reported as evidence."""
    from osint_app.config import settings

    monkeypatch.setattr(settings, "searxng_url", "http://fake-searxng.local")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/9876543210",
                        "title": "Contact 9876543210",
                        "content": "call now",
                        "engine": "duckduckgo",
                    },
                    {
                        "url": "https://www.wal-mart.com/ip/1281693719",
                        "title": "Some unrelated product",
                        "content": "totally unrelated content",
                        "engine": "bing",
                    },
                ]
            },
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("osint_app.search_client.httpx.AsyncClient", client_factory)

    adapter = PublicWebAdapter()
    results = await adapter.run_queries(['"9876543210"'])

    assert len(results) == 1
    assert results[0].value == "https://example.com/9876543210"


def test_relevance_term_extracts_quoted_value_not_suffix():
    assert _relevance_term('"+919876543210" whatsapp') == "+919876543210"
    assert _relevance_term('"9876543210"') == "9876543210"
    assert _relevance_term("no quotes here") == "no quotes here"


def test_is_relevant_checks_title_content_and_url():
    assert _is_relevant("9876543210", {"title": "Contact 9876543210", "content": "", "url": ""}) is True
    assert _is_relevant("9876543210", {"title": "", "content": "", "url": "https://x.com/9876543210"}) is True
    assert _is_relevant("9876543210", {"title": "unrelated", "content": "unrelated", "url": "https://x.com/other"}) is False
