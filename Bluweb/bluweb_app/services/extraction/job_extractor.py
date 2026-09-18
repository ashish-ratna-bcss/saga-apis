"""Job posting extraction (Universal Adaptive Web Intelligence). schema.org
JobPosting is the schema.org-recommended, near-universal markup for job
listings (used by every major ATS/job board) -- structured data only, same
posture as event_extractor.py."""

from __future__ import annotations

from datetime import datetime

from bluweb_app.services.extraction.article_extractor import ArticleDocument
from bluweb_app.services.extraction.structured_data import StructuredData


def extract_job(url: str, html: str, *, structured: StructuredData) -> ArticleDocument | None:
    job = structured.entities.get("JobPosting")
    if not job or not (job.get("title") or job.get("description")):
        return None

    body = job.get("description") or ""
    if len(body) < 10:
        return None

    fields_detected = ["body"]
    if job.get("title"):
        fields_detected.append("headline")
    if job.get("hiring_organization"):
        fields_detected.append("publisher")
    if job.get("location"):
        fields_detected.append("location")
    if job.get("salary"):
        fields_detected.append("salary")

    return ArticleDocument(
        extractor="job",
        headline=job.get("title"),
        body=body,
        author=None,
        publisher=job.get("hiring_organization"),
        published_at=_from_iso(job.get("date_posted")),
        updated_at=_from_iso(job.get("valid_through")),
        section=job.get("location"),
        canonical_url=structured.canonical_url or url,
        language=None,
        tags=[],
        images=[],
        confidence=0.7 if job.get("salary") else 0.55,
        fields_detected=fields_detected,
        raw_metadata={
            "salary": job.get("salary"),
            "salary_currency": job.get("salary_currency"),
            "employment_type": job.get("employment_type"),
            "location": job.get("location"),
            "hiring_organization": job.get("hiring_organization"),
            "valid_through": job.get("valid_through"),
            "status": _infer_status(job.get("valid_through")),
        },
    )


def _infer_status(valid_through: str | None) -> str:
    """No schema.org field says "closed" directly -- `validThrough` in the
    past is the closest generic signal a job board reliably publishes."""
    deadline = _from_iso(valid_through)
    if deadline is None:
        return "open"
    # `validThrough` is often a bare date ("2020-01-01"), which parses
    # naive -- match `now`'s awareness to whatever the deadline turned out
    # to be rather than assuming either.
    now = datetime.now(deadline.tzinfo) if deadline.tzinfo else datetime.now()
    return "closed" if deadline < now else "open"


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
