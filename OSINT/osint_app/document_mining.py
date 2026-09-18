"""Decides which discovered URLs are worth fetching+mining this pass, and
enforces the per-investigation document cap -- same spirit as PIVOT SAFETY
CONTROLS, applied to document fetches instead of identifier pivots (a
document fetch is a real network round-trip against a third-party site,
same cost concern as a pivot).
"""
from sqlalchemy.orm import Session

from osint_app.config import settings
from osint_app.models import Evidence, Investigation, SearchJob


def select_document_urls(
    db: Session, investigation: Investigation, evidence_rows: list[Evidence], adapter_name: str
) -> list[str]:
    if not settings.document_mining_enabled:
        return []

    already_mined = db.query(SearchJob).filter_by(investigation_id=investigation.id, source_name=adapter_name).count()
    remaining = settings.max_documents_per_investigation - already_mined
    if remaining <= 0:
        return []

    seen: set[str] = set()
    urls: list[str] = []
    for evidence in evidence_rows:
        if evidence.entity_type != "URL" or not evidence.raw_value:
            continue
        url = evidence.raw_value
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= remaining:
            break
    return urls
