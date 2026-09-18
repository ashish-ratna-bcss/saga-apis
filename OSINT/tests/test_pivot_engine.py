import pytest

from osint_app import pivot_engine
from osint_app.config import settings
from osint_app.entity_resolution import get_or_create_root_entity
from osint_app.enums import EntityType, IdentifierType, PivotStatus, RelationshipType
from osint_app.models import Entity, EntityRelationship, Evidence, Investigation, Pivot


def _make_investigation(db_session, identifier_type=IdentifierType.PHONE, normalized="+919876543210") -> Investigation:
    inv = Investigation(input_identifier=normalized, identifier_type=identifier_type, normalized_identifier=normalized)
    db_session.add(inv)
    db_session.flush()
    return inv


def _make_evidence(db_session, investigation, entity, *, raw_value="", source_url=None, title="", snippet="") -> Evidence:
    ev = Evidence(
        investigation_id=investigation.id,
        entity_id=entity.id,
        source_name="public_web",
        source_type="public_association",
        query=investigation.normalized_identifier,
        raw_value=raw_value,
        normalized_value=raw_value,
        entity_type="URL",
        source_url=source_url,
        confidence=0.4,
        extraction_method="searxng_search",
        raw_metadata={"title": title, "snippet": snippet},
    )
    db_session.add(ev)
    db_session.flush()
    return ev


@pytest.fixture(autouse=True)
def _default_pivot_settings(monkeypatch):
    monkeypatch.setattr(settings, "pivot_enabled", True)
    monkeypatch.setattr(settings, "max_pivot_depth", 2)
    monkeypatch.setattr(settings, "max_total_pivots_per_investigation", 15)
    monkeypatch.setattr(settings, "max_pivots_per_entity", 5)
    monkeypatch.setattr(settings, "pivot_confidence_threshold", 0.5)


def test_email_extracted_from_evidence_text_becomes_approved_target(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="contact rahul@example.com for details")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert len(targets) == 1
    target = targets[0]
    assert target.identifier_type == IdentifierType.EMAIL
    assert target.normalized_value == "rahul@example.com"
    assert target.depth == 1

    pivot_row = db_session.query(Pivot).filter_by(id=target.pivot_id).one()
    assert pivot_row.status == PivotStatus.PENDING
    assert pivot_row.parent_entity_id == root.id
    assert pivot_row.triggering_evidence_id == ev.id

    edge = (
        db_session.query(EntityRelationship)
        .filter_by(source_entity_id=target.pivot_entity.id, target_entity_id=root.id)
        .one()
    )
    assert edge.relationship_type == RelationshipType.DISCOVERED_FROM


def test_phone_extracted_from_evidence_text(db_session):
    inv = _make_investigation(db_session, identifier_type=IdentifierType.EMAIL, normalized="test@example.com")
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="reach the office at +91 98765 43210 anytime")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert len(targets) == 1
    assert targets[0].identifier_type == IdentifierType.PHONE
    assert targets[0].normalized_value == "+919876543210"


def test_username_extracted_from_known_profile_url(db_session):
    inv = _make_investigation(db_session, identifier_type=IdentifierType.EMAIL, normalized="test@example.com")
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, raw_value="https://github.com/torvalds", source_url="https://github.com/torvalds")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert len(targets) == 1
    assert targets[0].identifier_type == IdentifierType.USERNAME
    assert targets[0].normalized_value == "torvalds"


def test_single_mention_domain_is_low_confidence_and_skipped(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, raw_value="https://somecompany.co.in/contact", source_url="https://somecompany.co.in/contact")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert targets == []
    skipped = db_session.query(Pivot).filter_by(investigation_id=inv.id, normalized_value="somecompany.co.in").one()
    assert skipped.status == PivotStatus.SKIPPED_LOW_CONFIDENCE
    assert skipped.pivot_entity_id is None  # never materialized as an Entity


def test_domain_mentioned_twice_is_corroborated_and_approved(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev1 = _make_evidence(db_session, inv, root, raw_value="https://somecompany.co.in/a", source_url="https://somecompany.co.in/a")
    ev2 = _make_evidence(db_session, inv, root, raw_value="https://somecompany.co.in/b", source_url="https://somecompany.co.in/b")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev1, ev2], depth=1, max_depth=settings.max_pivot_depth)

    domain_targets = [t for t in targets if t.identifier_type == IdentifierType.DOMAIN]
    assert len(domain_targets) == 1
    assert domain_targets[0].normalized_value == "somecompany.co.in"


def test_generic_domain_never_pivoted_even_when_repeated(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev1 = _make_evidence(db_session, inv, root, raw_value="https://github.com/a", source_url="https://github.com/a/nonprofile/path")
    ev2 = _make_evidence(db_session, inv, root, raw_value="https://github.com/b", source_url="https://github.com/b/nonprofile/path")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev1, ev2], depth=1, max_depth=settings.max_pivot_depth)

    assert all(t.identifier_type != IdentifierType.DOMAIN for t in targets)


def test_self_reference_is_skipped_not_repivoted(db_session):
    inv = _make_investigation(db_session, normalized="+919876543210")
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="call +91 98765 43210 again")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert targets == []
    skip = db_session.query(Pivot).filter_by(investigation_id=inv.id, normalized_value="+919876543210").one()
    assert skip.status == PivotStatus.SKIPPED_SELF_REFERENCE

    entities = db_session.query(Entity).filter_by(investigation_id=inv.id).all()
    assert len(entities) == 1  # only the root -- no duplicate Phone entity


def test_duplicate_candidate_across_two_calls_is_deduped_but_adds_corroboration_edge(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev1 = _make_evidence(db_session, inv, root, snippet="email rahul@example.com")
    targets1 = pivot_engine.evaluate_pivots(db_session, inv, root, [ev1], depth=1, max_depth=settings.max_pivot_depth)
    assert len(targets1) == 1

    # A second, independent parent entity (simulating a different discovered
    # URL) mentions the SAME email -- must not spawn a second pivot job, but
    # should still record the second discovery path.
    other_entity = Entity(
        investigation_id=inv.id, entity_type=EntityType.URL, value="https://x.com", normalized_value="https://x.com",
        claim_type=root.claim_type, confidence=0.4,
    )
    db_session.add(other_entity)
    db_session.flush()
    ev2 = _make_evidence(db_session, inv, other_entity, snippet="also rahul@example.com")
    targets2 = pivot_engine.evaluate_pivots(db_session, inv, other_entity, [ev2], depth=1, max_depth=settings.max_pivot_depth)

    assert targets2 == []
    dup = (
        db_session.query(Pivot)
        .filter_by(investigation_id=inv.id, normalized_value="rahul@example.com", parent_entity_id=other_entity.id)
        .one()
    )
    assert dup.status == PivotStatus.SKIPPED_DUPLICATE

    entities = db_session.query(Entity).filter_by(investigation_id=inv.id, entity_type=EntityType.EMAIL).all()
    assert len(entities) == 1  # still just one Email entity, not two

    edges = db_session.query(EntityRelationship).filter_by(
        source_entity_id=entities[0].id, relationship_type=RelationshipType.DISCOVERED_FROM
    ).all()
    assert {e.target_entity_id for e in edges} == {root.id, other_entity.id}


def test_max_pivots_per_entity_limit_enforced(db_session, monkeypatch):
    monkeypatch.setattr(settings, "max_pivots_per_entity", 1)
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="reach a@example.com or b@example.com")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert len(targets) == 1
    limited = db_session.query(Pivot).filter_by(investigation_id=inv.id, status=PivotStatus.SKIPPED_LIMIT_REACHED).all()
    assert len(limited) == 1


def test_max_total_pivots_per_investigation_enforced(db_session, monkeypatch):
    monkeypatch.setattr(settings, "max_total_pivots_per_investigation", 1)
    monkeypatch.setattr(settings, "max_pivots_per_entity", 10)
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="reach a@example.com or b@example.com")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert len(targets) == 1


def test_pivot_disabled_returns_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "pivot_enabled", False)
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="rahul@example.com")

    assert pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth) == []
    assert db_session.query(Pivot).count() == 0


def test_depth_beyond_max_returns_nothing(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="rahul@example.com")

    assert pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=settings.max_pivot_depth + 1, max_depth=settings.max_pivot_depth) == []


def test_low_confidence_email_below_custom_threshold(db_session, monkeypatch):
    monkeypatch.setattr(settings, "pivot_confidence_threshold", 0.9)  # above email's fixed 0.75
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="rahul@example.com")

    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    assert targets == []
    skip = db_session.query(Pivot).filter_by(investigation_id=inv.id).one()
    assert skip.status == PivotStatus.SKIPPED_LOW_CONFIDENCE


def test_mark_pivot_status_records_job_ids(db_session):
    inv = _make_investigation(db_session)
    root = get_or_create_root_entity(db_session, inv)
    ev = _make_evidence(db_session, inv, root, snippet="rahul@example.com")
    targets = pivot_engine.evaluate_pivots(db_session, inv, root, [ev], depth=1, max_depth=settings.max_pivot_depth)

    pivot_engine.mark_pivot_status(db_session, targets[0].pivot_id, PivotStatus.COMPLETED, job_ids=["job-1", "job-2"])

    updated = db_session.query(Pivot).filter_by(id=targets[0].pivot_id).one()
    assert updated.status == PivotStatus.COMPLETED
    assert updated.job_ids == ["job-1", "job-2"]
