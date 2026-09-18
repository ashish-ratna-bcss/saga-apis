import httpx
import pytest

from osint_app.document_fetch import DocumentUnavailable, fetch_document, validate_url

# --- validate_url: real DNS/IP-literal resolution, no mocking -- these are
# the exact real-world SSRF targets this function exists to block.

def test_blocks_loopback_ip_literal():
    with pytest.raises(DocumentUnavailable, match="non-public address"):
        validate_url("http://127.0.0.1/")


def test_blocks_localhost_hostname():
    with pytest.raises(DocumentUnavailable, match="non-public address"):
        validate_url("http://localhost/")


def test_blocks_cloud_metadata_ip():
    with pytest.raises(DocumentUnavailable, match="non-public address"):
        validate_url("http://169.254.169.254/latest/meta-data/")


def test_blocks_ipv6_loopback():
    with pytest.raises(DocumentUnavailable, match="non-public address"):
        validate_url("http://[::1]/")


def test_blocks_private_range_ip_literal():
    with pytest.raises(DocumentUnavailable, match="non-public address"):
        validate_url("http://10.0.0.5/")
    with pytest.raises(DocumentUnavailable, match="non-public address"):
        validate_url("http://192.168.1.1/")


def test_rejects_non_http_scheme():
    with pytest.raises(DocumentUnavailable, match="scheme not allowed"):
        validate_url("ftp://example.com/x")
    with pytest.raises(DocumentUnavailable, match="scheme not allowed"):
        validate_url("file:///etc/passwd")


def test_rejects_url_with_no_hostname():
    with pytest.raises(DocumentUnavailable):
        validate_url("http:///path-only")


def test_allows_real_public_domain():
    validate_url("https://www.python.org/")  # must not raise


# --- fetch_document: mocked transport for deterministic size/redirect tests

_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any monkeypatching -- app.document_fetch shares this module


def _client_factory(handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)
    return factory


@pytest.mark.asyncio
async def test_fetch_returns_body_and_content_type(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello world")

    monkeypatch.setattr("osint_app.document_fetch.validate_url", lambda url: None)
    monkeypatch.setattr("osint_app.document_fetch.httpx.AsyncClient", _client_factory(handler))

    doc = await fetch_document("https://example.com/file.txt")
    assert doc.raw_bytes == b"hello world"
    assert doc.content_type == "text/plain"
    assert doc.url == "https://example.com/file.txt"


@pytest.mark.asyncio
async def test_fetch_rejects_oversized_content_length(monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "max_document_bytes", 10)
    monkeypatch.setattr("osint_app.document_fetch.validate_url", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "1000000", "content-type": "text/plain"}, content=b"x")

    monkeypatch.setattr("osint_app.document_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(DocumentUnavailable, match="too large"):
        await fetch_document("https://example.com/huge.txt")


@pytest.mark.asyncio
async def test_fetch_rejects_stream_exceeding_limit_even_without_content_length(monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "max_document_bytes", 5)
    monkeypatch.setattr("osint_app.document_fetch.validate_url", lambda url: None)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = httpx.Response(200, headers={"content-type": "text/plain"}, content=b"way more than five bytes")
        del resp.headers["content-length"]  # force the code down the no-content-length streaming path
        return resp

    monkeypatch.setattr("osint_app.document_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(DocumentUnavailable, match="byte limit"):
        await fetch_document("https://example.com/stream.txt")


@pytest.mark.asyncio
async def test_fetch_follows_redirect_and_revalidates_target(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://example.com/start":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"final content")

    validated = []

    def fake_validate(url):
        validated.append(url)

    monkeypatch.setattr("osint_app.document_fetch.validate_url", fake_validate)
    monkeypatch.setattr("osint_app.document_fetch.httpx.AsyncClient", _client_factory(handler))

    doc = await fetch_document("https://example.com/start")
    assert doc.raw_bytes == b"final content"
    assert doc.url == "https://example.com/final"
    assert validated == ["https://example.com/start", "https://example.com/final"]


@pytest.mark.asyncio
async def test_fetch_redirect_to_private_ip_is_blocked():
    """The realistic SSRF vector for a service that fetches already-vetted
    public URLs: an initially-public URL redirects to an internal address.
    Each hop is independently validated -- this proves the redirect itself
    can't be used to reach a private target."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "example.com" in str(request.url):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, content=b"should never get here")

    import osint_app.document_fetch as document_fetch_module

    orig_async_client = document_fetch_module.httpx.AsyncClient
    document_fetch_module.httpx.AsyncClient = _client_factory(handler)
    try:
        with pytest.raises(DocumentUnavailable, match="non-public address"):
            await fetch_document("https://example.com/redirector")
    finally:
        document_fetch_module.httpx.AsyncClient = orig_async_client


@pytest.mark.asyncio
async def test_fetch_raises_on_too_many_redirects(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    monkeypatch.setattr("osint_app.document_fetch.validate_url", lambda url: None)
    monkeypatch.setattr("osint_app.document_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(DocumentUnavailable, match="too many redirects"):
        await fetch_document("https://example.com/loop")


@pytest.mark.asyncio
async def test_fetch_raises_on_non_200_status(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    monkeypatch.setattr("osint_app.document_fetch.validate_url", lambda url: None)
    monkeypatch.setattr("osint_app.document_fetch.httpx.AsyncClient", _client_factory(handler))

    with pytest.raises(DocumentUnavailable, match="HTTP 404"):
        await fetch_document("https://example.com/missing")


# --- live network test: a real, small, stable public document

@pytest.mark.asyncio
async def test_live_fetch_real_public_text_file():
    """Live-verified against a real public URL, not mocked -- confirms the
    whole SSRF-validate + stream + decode path works end to end."""
    doc = await fetch_document("https://www.python.org/robots.txt")
    assert doc.content_type in {"text/plain", "text/plain; charset=utf-8"} or "text" in doc.content_type
    assert len(doc.raw_bytes) > 0
