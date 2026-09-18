import pytest

from osint_app.adapters.base import AdapterStatus, SourceUnavailable
from osint_app.adapters.document_adapter import DocumentAdapter
from osint_app.document_fetch import DocumentUnavailable, FetchedDocument
from osint_app.document_text import UnsupportedDocumentType
from osint_app.enums import ClaimType, EntityType


@pytest.mark.asyncio
async def test_is_available_always_true():
    assert await DocumentAdapter().is_available() is True


@pytest.mark.asyncio
async def test_run_returns_document_result_with_snippet(monkeypatch):
    async def fake_fetch(url):
        return FetchedDocument(url=url, content_type="text/html", raw_bytes=b"<p>hi rahul@example.com</p>")

    monkeypatch.setattr("osint_app.adapters.document_adapter.fetch_document", fake_fetch)
    monkeypatch.setattr(
        "osint_app.adapters.document_adapter.extract_text",
        lambda content_type, raw_bytes, url: "hi rahul@example.com",
    )

    results = await DocumentAdapter().run("https://example.com/page.html")

    assert len(results) == 1
    result = results[0]
    assert result.entity_type == EntityType.DOCUMENT
    assert result.claim_type == ClaimType.PUBLIC_ASSOCIATION
    assert result.confidence == 1.0
    assert result.status == AdapterStatus.FOUND
    assert result.evidence.metadata["snippet"] == "hi rahul@example.com"
    assert result.evidence.metadata["content_type"] == "text/html"


@pytest.mark.asyncio
async def test_fetch_failure_raises_source_unavailable(monkeypatch):
    async def fake_fetch(url):
        raise DocumentUnavailable("blocked: non-public address")

    monkeypatch.setattr("osint_app.adapters.document_adapter.fetch_document", fake_fetch)

    with pytest.raises(SourceUnavailable, match="non-public address"):
        await DocumentAdapter().run("http://127.0.0.1/secret")


@pytest.mark.asyncio
async def test_unsupported_type_raises_source_unavailable(monkeypatch):
    async def fake_fetch(url):
        return FetchedDocument(url=url, content_type="application/zip", raw_bytes=b"PK")

    def fake_extract(content_type, raw_bytes, url):
        raise UnsupportedDocumentType(f"unsupported {content_type}")

    monkeypatch.setattr("osint_app.adapters.document_adapter.fetch_document", fake_fetch)
    monkeypatch.setattr("osint_app.adapters.document_adapter.extract_text", fake_extract)

    with pytest.raises(SourceUnavailable, match="unsupported"):
        await DocumentAdapter().run("https://example.com/archive.zip")


@pytest.mark.asyncio
async def test_snippet_truncated_to_stored_limit(monkeypatch):
    long_text = "x" * 10_000

    async def fake_fetch(url):
        return FetchedDocument(url=url, content_type="text/plain", raw_bytes=long_text.encode())

    monkeypatch.setattr("osint_app.adapters.document_adapter.fetch_document", fake_fetch)
    monkeypatch.setattr("osint_app.adapters.document_adapter.extract_text", lambda *a: long_text)

    from osint_app.adapters.document_adapter import SNIPPET_CHARS_STORED

    results = await DocumentAdapter().run("https://example.com/big.txt")
    assert len(results[0].evidence.metadata["snippet"]) == SNIPPET_CHARS_STORED
    assert results[0].evidence.metadata["char_count"] == 10_000
