"""FAQ page extraction (Universal Adaptive Web Intelligence). schema.org
FAQPage is the standard markup Google's own rich-results docs recommend --
structured data only, same posture as event_extractor.py."""

from __future__ import annotations

from bluweb_app.services.extraction.article_extractor import ArticleDocument
from bluweb_app.services.extraction.structured_data import StructuredData


def extract_faq(url: str, html: str, *, structured: StructuredData) -> ArticleDocument | None:
    faq = structured.entities.get("FAQPage")
    items = faq.get("items") if faq else None
    if not items:
        return None

    body = "\n\n".join(f"Q: {item['question']}\nA: {item['answer'] or ''}" for item in items)
    if len(body) < 10:
        return None

    return ArticleDocument(
        extractor="faq",
        headline=None,
        body=body,
        author=None,
        publisher=None,
        published_at=None,
        updated_at=None,
        section=None,
        canonical_url=structured.canonical_url or url,
        language=None,
        tags=[],
        images=[],
        confidence=0.7,
        fields_detected=["body", "faq_items"],
        raw_metadata={"faq_items": items, "faq_item_count": len(items)},
    )
