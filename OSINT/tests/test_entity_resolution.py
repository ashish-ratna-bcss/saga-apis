from datetime import UTC, datetime

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus
from osint_app.entity_resolution import ingest_adapter_result
from osint_app.enums import ClaimType, EntityType, IdentifierType, RelationshipType
from osint_app.models import Entity, EntityRelationship, Evidence, Investigation


def _make_investigation(db_session) -> Investigation:
    inv = Investigation(
        input_identifier="rahul_123",
        identifier_type=IdentifierType.USERNAME,
        normalized_identifier="rahul_123",
    )
    db_session.add(inv)
    db_session.flush()
    return inv


def _social_result(source: str, url: str) -> AdapterResult:
    return AdapterResult(
        source=source,
        query="rahul_123",
        entity_type=EntityType.SOCIAL_PROFILE,
        value=url,
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.PUBLIC_ASSOCIATION,
        confidence=0.55,
        observed_at=datetime.now(UTC),
        evidence=AdapterEvidence(url=url, title="profile", metadata={}),
        extraction_method="test",
    )


def test_ingest_creates_root_entity_and_child_with_relationship(db_session):
    inv = _make_investigation(db_session)
    ingest_adapter_result(db_session, inv, "job-1", _social_result("sherlock", "https://github.com/rahul_123"))
    db_session.commit()

    entities = db_session.query(Entity).filter_by(investigation_id=inv.id).all()
    assert len(entities) == 2  # root Username + discovered SocialProfile
    root = next(e for e in entities if e.entity_type == EntityType.USERNAME)
    child = next(e for e in entities if e.entity_type == EntityType.SOCIAL_PROFILE)
    assert root.confidence == 1.0
    assert child.confidence > 0

    rels = db_session.query(EntityRelationship).filter_by(investigation_id=inv.id).all()
    assert len(rels) == 1
    assert rels[0].relationship_type == RelationshipType.HAS_PROFILE
    assert rels[0].source_entity_id == root.id
    assert rels[0].target_entity_id == child.id

    evidences = db_session.query(Evidence).filter_by(investigation_id=inv.id).all()
    assert len(evidences) == 1
    assert evidences[0].source_name == "sherlock"


def test_second_source_for_same_url_boosts_confidence_not_duplicate_entity(db_session):
    inv = _make_investigation(db_session)
    ingest_adapter_result(db_session, inv, "job-1", _social_result("sherlock", "https://github.com/rahul_123"))
    db_session.commit()

    entities_after_first = db_session.query(Entity).filter_by(investigation_id=inv.id).all()
    child = next(e for e in entities_after_first if e.entity_type == EntityType.SOCIAL_PROFILE)
    confidence_after_first = child.confidence

    ingest_adapter_result(db_session, inv, "job-2", _social_result("maigret", "https://github.com/rahul_123"))
    db_session.commit()

    entities_after_second = db_session.query(Entity).filter_by(investigation_id=inv.id).all()
    # still exactly one root + one child -- no duplicate created for the same URL
    assert len(entities_after_second) == 2
    child_after = next(e for e in entities_after_second if e.entity_type == EntityType.SOCIAL_PROFILE)
    assert child_after.id == child.id
    assert child_after.confidence > confidence_after_first

    evidences = db_session.query(Evidence).filter_by(entity_id=child.id).all()
    assert len(evidences) == 2
    assert {e.source_name for e in evidences} == {"sherlock", "maigret"}


def test_technical_claim_merges_into_investigation_metadata_not_graph(db_session):
    inv = Investigation(
        input_identifier="+919876543210",
        identifier_type=IdentifierType.PHONE,
        normalized_identifier="+919876543210",
    )
    db_session.add(inv)
    db_session.flush()

    technical_result = AdapterResult(
        source="phonenumbers",
        query="+919876543210",
        entity_type=EntityType.PHONE,
        value="+919876543210",
        status=AdapterStatus.FOUND,
        claim_type=ClaimType.TECHNICAL,
        confidence=1.0,
        observed_at=datetime.now(UTC),
        evidence=AdapterEvidence(url=None, title="technical", metadata={"carrier": "Airtel", "region_code": "IN"}),
        extraction_method="phonenumbers_lib",
    )
    ingest_adapter_result(db_session, inv, "job-1", technical_result)
    db_session.commit()

    entities = db_session.query(Entity).filter_by(investigation_id=inv.id).all()
    assert len(entities) == 1  # only the root Phone entity, no separate technical entity
    assert inv.technical_metadata["phonenumbers"]["carrier"] == "Airtel"

    rels = db_session.query(EntityRelationship).filter_by(investigation_id=inv.id).all()
    assert len(rels) == 0
