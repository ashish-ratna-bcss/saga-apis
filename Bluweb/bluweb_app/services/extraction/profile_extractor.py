"""Person/profile page extraction (Universal Adaptive Web Intelligence).
schema.org Person -- a staff bio, faculty page, or "meet the founder" page.
Falls back to the page's own Organization schema (a company "about us" page
that names itself but no individual) when no Person block is present,
rather than returning nothing."""

from __future__ import annotations

from bluweb_app.services.extraction.article_extractor import ArticleDocument
from bluweb_app.services.extraction.structured_data import StructuredData


def extract_profile(url: str, html: str, *, structured: StructuredData) -> ArticleDocument | None:
    person = structured.entities.get("Person")
    org = structured.entities.get("Organization")

    if person and (person.get("name") or person.get("description")):
        body = person.get("description") or person.get("name") or ""
        if len(body) < 5:
            return None
        fields_detected = ["body"]
        if person.get("name"):
            fields_detected.append("headline")
        if person.get("job_title"):
            fields_detected.append("job_title")
        return ArticleDocument(
            extractor="profile",
            headline=person.get("name"),
            body=body,
            author=None,
            publisher=person.get("works_for"),
            published_at=None,
            updated_at=None,
            section=person.get("job_title"),
            canonical_url=structured.canonical_url or url,
            language=None,
            tags=[],
            images=person.get("image") or [],
            confidence=0.65 if person.get("job_title") else 0.5,
            fields_detected=fields_detected,
            raw_metadata={
                "job_title": person.get("job_title"),
                "works_for": person.get("works_for"),
                "profile_url": person.get("url"),
                "subject_type": "Person",
            },
        )

    if org and (org.get("name") or org.get("description")):
        body = org.get("description") or org.get("name") or ""
        if len(body) < 5:
            return None
        return ArticleDocument(
            extractor="profile",
            headline=org.get("name"),
            body=body,
            author=None,
            publisher=org.get("name"),
            published_at=None,
            updated_at=None,
            section=org.get("address"),
            canonical_url=structured.canonical_url or url,
            language=None,
            tags=[],
            images=[],
            confidence=0.5,
            fields_detected=["body", "headline"],
            raw_metadata={
                "address": org.get("address"),
                "telephone": org.get("telephone"),
                "subject_type": "Organization",
            },
        )

    return None
