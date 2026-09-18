"""Public document (PDF/HTML/TXT) mining -- fetches an already-discovered
URL and stores its extracted text as evidence exactly like every other
source (see IMPORTANT IMPLEMENTATION PRINCIPLE: no separate code path). The
pivot engine's existing extraction pass already reads
`Evidence.raw_metadata['snippet']` for candidates -- storing the document's
text there means candidate identifiers found inside a PDF/HTML/TXT document
go through the exact same confidence/dedup/limit gating as anything found
in a search snippet, with zero changes needed to pivot_engine.py.
"""
from datetime import UTC, datetime

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.document_fetch import DocumentUnavailable, fetch_document
from osint_app.document_text import UnsupportedDocumentType, extract_text
from osint_app.enums import ClaimType, EntityType

SNIPPET_CHARS_STORED = 4000  # bounds DB row size; extraction still runs over the fuller MAX_TEXT_CHARS text


class DocumentAdapter(SourceAdapter):
    name = "document_extraction"
    accepts = None  # triggered on discovered URLs, not dispatched by identifier_type -- see registry.py

    async def is_available(self) -> bool:
        return True  # local libraries only (pypdf/bs4), no external service dependency

    async def run(self, url: str) -> list[AdapterResult]:
        try:
            doc = await fetch_document(url)
        except DocumentUnavailable as exc:
            raise SourceUnavailable(str(exc)) from exc

        try:
            text = extract_text(doc.content_type, doc.raw_bytes, doc.url)
        except UnsupportedDocumentType as exc:
            raise SourceUnavailable(str(exc)) from exc

        return [
            AdapterResult(
                source=self.name,
                query=url,
                entity_type=EntityType.DOCUMENT,
                value=doc.url,
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.PUBLIC_ASSOCIATION,
                confidence=1.0,  # we fetched and read it -- not a probabilistic claim
                observed_at=datetime.now(UTC),
                evidence=AdapterEvidence(
                    url=doc.url,
                    title=f"Document ({doc.content_type or 'unknown type'})",
                    metadata={
                        "snippet": text[:SNIPPET_CHARS_STORED],
                        "content_type": doc.content_type,
                        "byte_size": len(doc.raw_bytes),
                        "char_count": len(text),
                    },
                ),
                extraction_method="document_fetch",
            )
        ]
