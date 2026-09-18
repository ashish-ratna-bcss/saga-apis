import io

import pytest

from osint_app.document_text import UnsupportedDocumentType, extract_text


def _build_minimal_pdf(text: str) -> bytes:
    """A real, hand-built, spec-valid single-page PDF with `text` in its
    content stream -- not a mock, pypdf parses this exactly like any other
    PDF. Avoids depending on a PDF-generation library Phase 1 doesn't need
    for anything else."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 300] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 18 Tf 10 150 Td ({text}) Tj ET".encode()
    objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode())
        out.write(obj)
        out.write(b"\nendobj\n")
    xref_start = out.tell()
    n = len(objects) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer\n")
    out.write(f"<< /Size {n} /Root 1 0 R >>\n".encode())
    out.write(b"startxref\n")
    out.write(f"{xref_start}\n".encode())
    out.write(b"%%EOF")
    return out.getvalue()


def test_pdf_extraction_by_content_type():
    pdf_bytes = _build_minimal_pdf("Hello PDF rahul@example.com")
    text = extract_text("application/pdf", pdf_bytes, "https://example.com/doc")
    assert "rahul@example.com" in text


def test_pdf_extraction_by_url_extension_when_content_type_generic():
    pdf_bytes = _build_minimal_pdf("Extension fallback works")
    text = extract_text("application/octet-stream", pdf_bytes, "https://example.com/report.PDF")
    assert "Extension fallback works" in text


def test_html_extraction_strips_script_and_style():
    html = b"""
    <html><head><style>body{color:red}</style></head>
    <body><script>alert('x')</script><p>Contact rahul@example.com here</p></body></html>
    """
    text = extract_text("text/html", html, "https://example.com/page.html")
    assert "rahul@example.com" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_txt_extraction_decodes_utf8():
    text = extract_text("text/plain", "plain text café".encode(), "https://example.com/file.txt")
    assert text == "plain text café"


def test_txt_extraction_replaces_invalid_bytes_instead_of_raising():
    text = extract_text("text/plain", b"valid \xff\xfe invalid", "https://example.com/file.txt")
    assert "valid" in text  # must not raise UnicodeDecodeError


def test_unsupported_content_type_raises():
    with pytest.raises(UnsupportedDocumentType):
        extract_text("application/zip", b"PK\x03\x04", "https://example.com/archive.zip")


def test_pdf_extraction_truncates_at_max_chars(monkeypatch):
    import osint_app.document_text as document_text_module

    monkeypatch.setattr(document_text_module, "MAX_TEXT_CHARS", 5)
    text = extract_text("text/plain", b"way more than five characters", "https://example.com/x.txt")
    assert len(text) == 5


def test_malformed_pdf_raises_unsupported_not_a_crash():
    with pytest.raises(UnsupportedDocumentType):
        extract_text("application/pdf", b"not actually a pdf file", "https://example.com/fake.pdf")
