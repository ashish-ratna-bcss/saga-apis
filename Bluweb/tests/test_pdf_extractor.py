from pathlib import Path

from bluweb_app.services.extraction.pdf_extractor import extract_pdf, is_pdf_response

_PDF_FIXTURE = Path(__file__).parent / "fixtures" / "minimal_report.pdf"


def test_is_pdf_response_by_content_type():
    assert is_pdf_response("application/pdf", "https://example.gov/reports/annual") is True
    assert is_pdf_response("application/pdf; charset=binary", "https://example.gov/x") is True


def test_is_pdf_response_by_url_extension_fallback():
    assert is_pdf_response(None, "https://example.gov/reports/annual.pdf") is True
    assert is_pdf_response("application/octet-stream", "https://example.gov/x.pdf?v=2") is True


def test_is_pdf_response_false_for_html():
    assert is_pdf_response("text/html", "https://example.com/page") is False
    assert is_pdf_response(None, "https://example.com/page") is False


def test_extracts_text_and_metadata_from_real_pdf():
    raw = _PDF_FIXTURE.read_bytes()
    doc = extract_pdf(raw, "https://example.gov/reports/annual.pdf")

    assert doc is not None
    assert doc.extractor == "pdf"
    assert doc.headline == "Test Report"
    assert doc.author == "Jane Doe"
    assert doc.published_at is not None
    assert "Hello PDF World" in doc.body
    assert doc.raw_metadata["page_count"] == 1
    assert doc.raw_metadata["content_type"] == "pdf"


def test_malformed_pdf_returns_none_not_a_crash():
    assert extract_pdf(b"not a pdf at all", "https://example.com/broken.pdf") is None
    assert extract_pdf(b"", "https://example.com/empty.pdf") is None
