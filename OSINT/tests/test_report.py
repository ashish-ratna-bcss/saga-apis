from datetime import UTC, datetime

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus
from osint_app.entity_resolution import get_or_create_root_entity, ingest_adapter_result
from osint_app.enums import ClaimType, EntityType, IdentifierType, JobStatus, PivotStatus
from osint_app.models import Investigation, Pivot, SearchJob
from osint_app.report import build_report


def test_report_excludes_root_entity_and_technical_from_discovered(db_session):
    inv = Investigation(
        input_identifier="+919876543210",
        identifier_type=IdentifierType.PHONE,
        normalized_identifier="+919876543210",
    )
    db_session.add(inv)
    db_session.flush()

    technical = AdapterResult(
        source="phonenumbers",
        query="+919876543210",
        entity_type=EntityType.PHONE,
        value="+919876543210",
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.TECHNICAL,
        confidence=1.0,
        observed_at=datetime.now(UTC),
        evidence=AdapterEvidence(url=None, title="t", metadata={"carrier": "Airtel"}),
        extraction_method="phonenumbers_lib",
    )
    discovered = AdapterResult(
        source="public_web",
        query='"+919876543210"',
        entity_type=EntityType.URL,
        value="https://example.com/found",
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.PUBLIC_ASSOCIATION,
        confidence=0.4,
        observed_at=datetime.now(UTC),
        evidence=AdapterEvidence(url="https://example.com/found", title="t", metadata={}),
        extraction_method="searxng_search",
    )
    db_session.add(SearchJob(id="job-1", investigation_id=inv.id, source_name="phonenumbers", status=JobStatus.COMPLETED))
    db_session.add(SearchJob(id="job-2", investigation_id=inv.id, source_name="public_web", status=JobStatus.COMPLETED))
    ingest_adapter_result(db_session, inv, "job-1", technical)
    ingest_adapter_result(db_session, inv, "job-2", discovered)
    db_session.commit()

    report = build_report(db_session, inv)

    assert "Phone" not in report["discovered_entities"]  # root Phone entity is not a "discovery"
    assert report["discovered_entities"]["URL"][0]["value"] == "https://example.com/found"
    assert report["technical_data"]["phonenumbers"]["carrier"] == "Airtel"
    assert report["case"]["normalized_identifier"] == "+919876543210"
    assert len(report["limitations"]) > 0
    assert report["sources"]["queried"] == ["phonenumbers", "public_web"]
    assert report["case"]["mode"] == "standard"


def test_report_summarizes_pivots(db_session):
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123"
    )
    db_session.add(inv)
    db_session.flush()
    root = get_or_create_root_entity(db_session, inv)

    executed = Pivot(
        investigation_id=inv.id, parent_entity_id=root.id, pivot_entity_id=root.id,
        identifier_type=IdentifierType.EMAIL, value="a@b.com", normalized_value="a@b.com",
        extraction_method="regex_email_extraction", extraction_confidence=0.75, reason="found email",
        depth=1, status=PivotStatus.COMPLETED,
    )
    skipped = Pivot(
        investigation_id=inv.id, parent_entity_id=root.id, pivot_entity_id=None,
        identifier_type=IdentifierType.DOMAIN, value="example.com", normalized_value="example.com",
        extraction_method="url_domain_frequency", extraction_confidence=0.35, reason="low confidence",
        depth=1, status=PivotStatus.SKIPPED_LOW_CONFIDENCE,
    )
    db_session.add_all([executed, skipped])
    db_session.commit()

    report = build_report(db_session, inv)

    assert report["pivots"]["counts_by_status"]["completed"] == 1
    assert report["pivots"]["counts_by_status"]["skipped_low_confidence"] == 1
    assert len(report["pivots"]["executed"]) == 1
    assert report["pivots"]["executed"][0]["value"] == "a@b.com"
