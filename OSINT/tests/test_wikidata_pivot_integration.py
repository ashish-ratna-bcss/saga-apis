"""Proves a Wikidata-discovered social profile URL flows through the
existing, unmodified pivot engine -- no special-casing for wikidata results,
exactly like document-mining's snippet text (see
test_document_pivot_integration.py for that half of the same principle)."""
import pytest

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus
from osint_app.entity_resolution import get_or_create_root_entity, ingest_adapter_result
from osint_app.enums import ClaimType, EntityType, IdentifierType, RelationshipType
from osint_app.models import EntityRelationship, Investigation
from osint_app.pivot_engine import evaluate_pivots


def _make_investigation(db_session) -> Investigation:
    inv = Investigation(input_identifier="Test Person", identifier_type=IdentifierType.PERSON_NAME, normalized_identifier="Test Person")
    db_session.add(inv)
    db_session.flush()
    return inv


@pytest.mark.asyncio
async def test_wikidata_social_profile_url_becomes_username_pivot_target(db_session, monkeypatch):
    from osint_app.config import settings

    monkeypatch.setattr(settings, "pivot_enabled", True)
    monkeypatch.setattr(settings, "pivot_confidence_threshold", 0.5)

    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)

    social_result = AdapterResult(
        source="wikidata",
        query="Test Person",
        entity_type=EntityType.SOCIAL_PROFILE,
        value="https://twitter.com/testhandle",
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.PUBLIC_ASSOCIATION,
        confidence=0.55,
        evidence=AdapterEvidence(
            url="https://twitter.com/testhandle", title="twitter.com profile", metadata={"snippet": "Twitter handle from Wikidata claim"}
        ),
        extraction_method="wikidata_claim_P2002",
    )
    evidence = ingest_adapter_result(db_session, inv, "job-1", social_result, anchor_entity=root)
    db_session.commit()

    # relationship for the SocialProfile itself: (PERSON, SOCIAL_PROFILE) -> HAS_PROFILE
    rel = db_session.query(EntityRelationship).filter_by(investigation_id=inv.id).one()
    assert rel.relationship_type == RelationshipType.HAS_PROFILE

    targets = evaluate_pivots(db_session, inv, root, [evidence], depth=1, max_depth=2)

    username_targets = [t for t in targets if t.identifier_type == IdentifierType.USERNAME]
    assert len(username_targets) == 1
    assert username_targets[0].normalized_value == "testhandle"


def test_person_company_relationship_rule_exists(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)

    company_result = AdapterResult(
        source="wikidata",
        query="Test Person",
        entity_type=EntityType.COMPANY,
        value="Employer Co",
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.PUBLIC_ASSOCIATION,
        confidence=0.55,
        evidence=AdapterEvidence(url="https://www.wikidata.org/wiki/Q999", title="Employer", metadata={}),
        extraction_method="wikidata_claim_P108",
    )
    ingest_adapter_result(db_session, inv, "job-1", company_result, anchor_entity=root)
    db_session.commit()

    rel = db_session.query(EntityRelationship).filter_by(investigation_id=inv.id).one()
    assert rel.relationship_type == RelationshipType.ASSOCIATED_WITH
