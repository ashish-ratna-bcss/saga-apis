import httpx
import pytest

from osint_app.config import settings
from osint_app.search_client import SearxngUnavailable, search


@pytest.mark.asyncio
async def test_unavailable_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", None)
    with pytest.raises(SearxngUnavailable):
        await search("query")


@pytest.mark.asyncio
async def test_normalizes_results_and_reports_engine_status(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://fake-searxng.local")
    monkeypatch.setattr(settings, "searxng_engines", "bing,duckduckgo")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "engines=bing%2Cduckduckgo" in str(request.url) or "engines=bing,duckduckgo" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Result one",
                        "url": "https://example.com/1",
                        "content": "snippet one",
                        "engine": "bing",
                        "category": "general",
                        "publishedDate": None,
                    }
                ],
                "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
            },
        )

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("osint_app.search_client.httpx.AsyncClient", client_factory)

    response = await search("test query")

    assert response.query == "test query"
    assert response.total == 1
    assert response.results[0].title == "Result one"
    assert response.results[0].url == "https://example.com/1"
    assert response.successful_engines == ["bing"]
    assert response.failed_engines == ["duckduckgo"]


@pytest.mark.asyncio
async def test_http_error_raises_searxng_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "http://fake-searxng.local")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("osint_app.search_client.httpx.AsyncClient", client_factory)

    with pytest.raises(SearxngUnavailable):
        await search("test query")
