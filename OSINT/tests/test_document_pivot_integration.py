"""Integration tests proving the full chain works together: a document's
extracted text becomes Evidence via the normal entity_resolution path, and
the pivot engine's existing extraction pass (unmodified) picks up candidate
identifiers embedded in that text -- exactly the same way it already does
for search snippets. No separate candidate-approval code path for documents.
"""
import pytest

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus
from osint_app.adapters.document_adapter import DocumentAdapter
from osint_app.document_fetch import FetchedDocument
from osint_app.entity_resolution import get_or_create_root_entity, ingest_adapter_result
from osint_app.enums import ClaimType, EntityType, IdentifierType
from osint_app.models import Investigation
from osint_app.pivot_engine import evaluate_pivots


def _make_investigation(db_session, identifier_type=IdentifierType.DOMAIN, normalized="example.com") -> Investigation:
    inv = Investigation(input_identifier=normalized, identifier_type=identifier_type, normalized_identifier=normalized)
    db_session.add(inv)
    db_session.flush()
    return inv


@pytest.mark.asyncio
async def test_email_embedded_in_document_text_becomes_pivot_target(db_session, monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "pivot_enabled", True)
    monkeypatch.setattr(settings, "pivot_confidence_threshold", 0.5)
    monkeypatch.setattr(settings, "max_total_pivots_per_investigation", 15)
    monkeypatch.setattr(settings, "max_pivots_per_entity", 5)

    async def fake_fetch(url):
        return FetchedDocument(
            url=url, content_type="application/pdf",
            raw_bytes=b"placeholder -- extract_text is mocked below",
        )

    monkeypatch.setattr("osint_app.adapters.document_adapter.fetch_document", fake_fetch)
    monkeypatch.setattr(
        "osint_app.adapters.document_adapter.extract_text",
        lambda content_type, raw_bytes, url: "For inquiries contact staff@example-corp.com or call +91 98765 43210.",
    )

    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)

    # Real DocumentAdapter run (fetch/extract mocked, everything else real)
    results = await DocumentAdapter().run("https://example.com/staff-directory.pdf")
    assert len(results) == 1

    # Real ingestion -- creates the Document entity + Evidence row exactly
    # like the orchestrator would for any other adapter's results.
    evidence = ingest_adapter_result(db_session, inv, "job-1", results[0], anchor_entity=root)
    db_session.commit()

    assert evidence.entity_type == "Document"
    assert "staff@example-corp.com" in evidence.raw_metadata["snippet"]

    # Real pivot evaluation over that evidence -- proves the document's text
    # is mined through the SAME path as a search snippet, no special-casing.
    targets = evaluate_pivots(db_session, inv, root, [evidence], depth=1, max_depth=2)

    target_types_values = {(t.identifier_type, t.normalized_value) for t in targets}
    assert (IdentifierType.EMAIL, "staff@example-corp.com") in target_types_values
    assert (IdentifierType.PHONE, "+919876543210") in target_types_values


@pytest.mark.asyncio
async def test_low_confidence_document_candidate_is_recorded_but_not_pivoted(db_session, monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "pivot_enabled", True)
    monkeypatch.setattr(settings, "pivot_confidence_threshold", 0.9)  # above email's fixed 0.75

    async def fake_fetch(url):
        return FetchedDocument(url=url, content_type="text/plain", raw_bytes=b"x")

    monkeypatch.setattr("osint_app.adapters.document_adapter.fetch_document", fake_fetch)
    monkeypatch.setattr(
        "osint_app.adapters.document_adapter.extract_text",
        lambda content_type, raw_bytes, url: "contact low-signal@example.com",
    )

    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)

    results = await DocumentAdapter().run("https://example.com/notes.txt")
    evidence = ingest_adapter_result(db_session, inv, "job-1", results[0], anchor_entity=root)
    db_session.commit()

    targets = evaluate_pivots(db_session, inv, root, [evidence], depth=1, max_depth=2)

    from osint_app.models import Pivot

    assert targets == []
    skip = db_session.query(Pivot).filter_by(investigation_id=inv.id, normalized_value="low-signal@example.com").one()
    assert skip.status == "skipped_low_confidence"


def test_document_entity_created_with_technical_metadata_untouched(db_session):
    """Document entities are PUBLIC_ASSOCIATION, not TECHNICAL -- they must
    show up in the entity graph (TECHNICAL claims are merged into
    investigation.technical_metadata and never become entities, which would
    be wrong for a real Document node the report/graph should surface)."""
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)

    result = AdapterResult(
        source="document_extraction",
        query="https://example.com/report.pdf",
        entity_type=EntityType.DOCUMENT,
        value="https://example.com/report.pdf",
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.PUBLIC_ASSOCIATION,
        confidence=1.0,
        evidence=AdapterEvidence(url="https://example.com/report.pdf", title="Document", metadata={"snippet": ""}),
        extraction_method="document_fetch",
    )
    ingest_adapter_result(db_session, inv, "job-1", result, anchor_entity=root)
    db_session.commit()

    from osint_app.models import Entity

    doc_entities = db_session.query(Entity).filter_by(investigation_id=inv.id, entity_type=EntityType.DOCUMENT).all()
    assert len(doc_entities) == 1
    assert doc_entities[0].value == "https://example.com/report.pdf"
    assert inv.technical_metadata == {}  # untouched -- this was not a TECHNICAL claim
