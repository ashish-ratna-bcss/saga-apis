"""Byte content -> plain text for PDF/HTML/TXT documents. Pure function, no
network -- see document_fetch.py for the SSRF-safe fetch layer, and
app.extraction for text -> candidate-identifier mining (this module only
gets bytes into text; extraction.py stays the single place that turns text
into candidates, reused by both search snippets and document text).
"""
import io
import logging

from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

# pypdf logs a warning per font it can't fully parse without the optional
# fontTools dependency (cosmetic -- text extraction still works); at real
# document-mining volume this floods logs with noise unrelated to any actual
# error, so only pypdf's own ERROR-level (real failure) output passes through.
logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_TEXT_CHARS = 200_000  # bounds extraction.py's regex/matcher cost on huge documents

PDF_CONTENT_TYPES = {"application/pdf"}
HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_CONTENT_TYPES = {"text/plain"}


class UnsupportedDocumentType(Exception):
    pass


def extract_text(content_type: str, raw_bytes: bytes, url: str) -> str:
    content_type = (content_type or "").split(";")[0].strip().lower()
    lowered_url = url.lower()

    if content_type in PDF_CONTENT_TYPES or lowered_url.endswith(".pdf"):
        return _extract_pdf_text(raw_bytes)
    if content_type in HTML_CONTENT_TYPES or lowered_url.endswith((".html", ".htm")):
        return _extract_html_text(raw_bytes)
    if content_type in TEXT_CONTENT_TYPES or lowered_url.endswith(".txt"):
        return _decode_text(raw_bytes)

    raise UnsupportedDocumentType(f"unsupported content-type {content_type!r} for {url}")


def _extract_pdf_text(raw_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        parts = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise UnsupportedDocumentType(f"could not parse PDF: {exc}") from exc
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def _extract_html_text(raw_bytes: bytes) -> str:
    soup = BeautifulSoup(raw_bytes, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)[:MAX_TEXT_CHARS]


def _decode_text(raw_bytes: bytes) -> str:
    return raw_bytes.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
