"""PDF/document intelligence (Universal Adaptive Web Intelligence section
6). Deliberately NOT part of extraction_router.py's HTML dispatch -- a PDF
never has `fetch_result.html`, so crawl_engine.py routes to this module on
`content_type`/URL-extension detection BEFORE the HTML page-classification
pipeline ever runs (see `is_pdf_response` and its call site in
crawl_engine.py). Same `ArticleDocument`-shaped return as every HTML
extractor, so downstream storage/versioning/change-detection code doesn't
need a second document shape.

pypdf (BSD-3-Clause, pure Python, actively maintained by the py-pdf org --
see pyproject.toml) is used for both text and metadata; no OCR, no image
extraction from PDF pages -- text-layer extraction only, which covers the
overwhelming majority of real-world PDFs (reports, filings, notices).
A scanned/image-only PDF with no text layer yields an empty body and
`extract_pdf` returns None, same "no usable content" contract as every
other extractor.
"""

from __future__ import annotations

import logging
from datetime import datetime

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from bluweb_app.services.extraction.article_extractor import ArticleDocument

logger = logging.getLogger("webintel.pdf_extractor")

MIN_PDF_BODY_CHARS = 30  # below this, treat as "no text layer" (likely scanned/image-only)


def is_pdf_response(content_type: str | None, url: str) -> bool:
    """Content-type is authoritative; URL extension is a fallback for
    servers that mislabel (or omit) the header -- the same
    "don't trust the label alone" posture soft_block_detector.py takes
    with HTTP status."""
    if content_type and "application/pdf" in content_type.lower():
        return True
    return url.split("?")[0].lower().endswith(".pdf")


def extract_pdf(raw_bytes: bytes, url: str) -> ArticleDocument | None:
    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(raw_bytes))
    except (PdfReadError, ValueError, OSError):
        logger.warning("PDF parse failed for %s", url, exc_info=True)
        return None

    try:
        page_count = len(reader.pages)
        body_parts = []
        for page in reader.pages:
            try:
                body_parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page must not lose the whole document
                continue
        body = "\n\n".join(p.strip() for p in body_parts if p.strip())
    except Exception:  # noqa: BLE001 - a malformed PDF must never crash the crawl
        logger.warning("PDF text extraction failed for %s", url, exc_info=True)
        return None

    if len(body) < MIN_PDF_BODY_CHARS:
        return None

    meta = reader.metadata or {}
    title = _clean(meta.get("/Title")) if meta else None
    author = _clean(meta.get("/Author")) if meta else None
    created_at = _pdf_date(meta.get("/CreationDate")) if meta else None
    modified_at = _pdf_date(meta.get("/ModDate")) if meta else None

    fields_detected = ["body"]
    if title:
        fields_detected.append("headline")
    if author:
        fields_detected.append("author")
    if created_at:
        fields_detected.append("published_at")

    return ArticleDocument(
        extractor="pdf",
        headline=title,
        body=body,
        author=author,
        publisher=None,
        published_at=created_at,
        updated_at=modified_at,
        section=None,
        canonical_url=url,
        language=None,
        tags=[],
        images=[],
        confidence=0.7 if title else 0.5,
        fields_detected=fields_detected,
        raw_metadata={"page_count": page_count, "content_type": "pdf"},
    )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _pdf_date(value: str | None) -> datetime | None:
    """PDF date strings look like D:20260115120000+00'00' -- pypdf's own
    metadata accessors return the raw string, not a parsed datetime."""
    if not value:
        return None
    raw = str(value)
    if raw.startswith("D:"):
        raw = raw[2:]
    raw = raw[:14]  # YYYYMMDDHHmmSS, drop timezone suffix -- naive is fine for a "roughly when" signal
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S")
    except ValueError:
        try:
            return datetime.strptime(raw[:8], "%Y%m%d")
        except ValueError:
            return None
