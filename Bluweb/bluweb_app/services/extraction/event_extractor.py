"""Event extraction (Universal Adaptive Web Intelligence). schema.org Event
is the near-universal way sites mark up events (conferences, meetings,
concerts, government notices with a date) -- structured data is
authoritative here; no DOM-heuristic fallback exists yet (add one if a real
site turns up that needs it, same posture as listing_extractor.py)."""

from __future__ import annotations

from datetime import datetime

from bluweb_app.services.extraction.article_extractor import ArticleDocument
from bluweb_app.services.extraction.structured_data import StructuredData


def extract_event(url: str, html: str, *, structured: StructuredData) -> ArticleDocument | None:
    event = structured.entities.get("Event")
    if not event or not (event.get("name") or event.get("description")):
        return None

    body = event.get("description") or event.get("name") or ""
    if len(body) < 10:
        return None

    fields_detected = ["body"]
    if event.get("name"):
        fields_detected.append("headline")
    if event.get("start_date"):
        fields_detected.append("published_at")
    if event.get("location"):
        fields_detected.append("location")
    if event.get("organizer"):
        fields_detected.append("organizer")

    return ArticleDocument(
        extractor="event",
        headline=event.get("name"),
        body=body,
        author=None,
        publisher=event.get("organizer"),
        published_at=_from_iso(event.get("start_date")),
        updated_at=_from_iso(event.get("end_date")),
        section=event.get("location"),
        canonical_url=structured.canonical_url or url,
        language=None,
        tags=[],
        images=event.get("image") or [],
        confidence=0.7 if event.get("start_date") else 0.5,
        fields_detected=fields_detected,
        raw_metadata={
            "start_date": event.get("start_date"),
            "end_date": event.get("end_date"),
            "status": event.get("status"),
            "location": event.get("location"),
            "organizer": event.get("organizer"),
            "price": event.get("price"),
        },
    )


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
