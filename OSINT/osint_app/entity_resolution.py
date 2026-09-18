"""Turns raw AdapterResult objects into Entity/EntityRelationship/Evidence
rows, deduplicating by fingerprint and recomputing confidence from ALL
evidence attached to an entity every time (see IDEMPOTENCY, CONFIDENCE ENGINE
in the spec). Never creates an entity without a provenance (Evidence) row.
"""
from datetime import UTC

from sqlalchemy.orm import Session

from osint_app import metrics
from osint_app.adapters.base import AdapterResult
from osint_app.confidence import compute_confidence
from osint_app.enums import ClaimType, EntityType, IdentifierType, RelationshipType
from osint_app.models import Entity, EntityRelationship, Evidence, Investigation


def _aware(dt):
    """SQLite drops tzinfo on DateTime(timezone=True) columns across a
    commit+reload -- a freshly-created (still tz-aware) row and a
    just-reloaded-from-DB (naive) row can end up in the same list. Comparing
    them raises TypeError, so every timestamp is normalized before use."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


_ROOT_ENTITY_TYPE: dict[IdentifierType, EntityType] = {
    IdentifierType.PHONE: EntityType.PHONE,
    IdentifierType.EMAIL: EntityType.EMAIL,
    IdentifierType.USERNAME: EntityType.USERNAME,
    IdentifierType.PERSON_NAME: EntityType.PERSON,
    IdentifierType.DOMAIN: EntityType.DOMAIN,
}

# (root entity type, discovered entity type) -> relationship type connecting them.
# Not exhaustive of every pairing in the spec's RELATIONSHIP TYPES list -- just
# the ones Phase 1's adapters can actually produce evidence for.
_RELATIONSHIP_RULES: dict[tuple[str, str], RelationshipType] = {
    (EntityType.USERNAME, EntityType.SOCIAL_PROFILE): RelationshipType.HAS_PROFILE,
    (EntityType.EMAIL, EntityType.SOCIAL_PROFILE): RelationshipType.HAS_PROFILE,
    (EntityType.PERSON, EntityType.SOCIAL_PROFILE): RelationshipType.HAS_PROFILE,
    (EntityType.PHONE, EntityType.URL): RelationshipType.MENTIONED_IN,
    (EntityType.EMAIL, EntityType.URL): RelationshipType.MENTIONED_IN,
    (EntityType.USERNAME, EntityType.URL): RelationshipType.MENTIONED_IN,
    (EntityType.PERSON, EntityType.URL): RelationshipType.MENTIONED_IN,
    (EntityType.DOMAIN, EntityType.URL): RelationshipType.MENTIONED_IN,
    (EntityType.PHONE, EntityType.DOCUMENT): RelationshipType.MENTIONED_IN,
    (EntityType.EMAIL, EntityType.DOCUMENT): RelationshipType.MENTIONED_IN,
    (EntityType.USERNAME, EntityType.DOCUMENT): RelationshipType.MENTIONED_IN,
    (EntityType.PERSON, EntityType.DOCUMENT): RelationshipType.MENTIONED_IN,
    (EntityType.DOMAIN, EntityType.DOCUMENT): RelationshipType.MENTIONED_IN,
    (EntityType.PERSON, EntityType.COMPANY): RelationshipType.ASSOCIATED_WITH,
    (EntityType.PERSON, EntityType.PERSON): RelationshipType.ASSOCIATED_WITH,  # same-name Wikidata match, see wikidata_adapter.py
}


def root_entity_type(identifier_type: IdentifierType) -> EntityType:
    return _ROOT_ENTITY_TYPE[identifier_type]


def get_or_create_root_entity(db: Session, investigation: Investigation) -> Entity:
    """The root entity represents the investigator's own input identifier --
    confidence 1.0 because it's given, not a discovered/inferred claim."""
    entity_type = root_entity_type(investigation.identifier_type)
    normalized_value = investigation.normalized_identifier.lower()
    entity = (
        db.query(Entity)
        .filter_by(investigation_id=investigation.id, entity_type=entity_type, normalized_value=normalized_value)
        .one_or_none()
    )
    if entity is None:
        entity = Entity(
            investigation_id=investigation.id,
            entity_type=entity_type,
            value=investigation.normalized_identifier,
            normalized_value=normalized_value,
            claim_type=ClaimType.PUBLIC_ASSOCIATION,
            confidence=1.0,
        )
        db.add(entity)
        db.flush()
        metrics.increment("entities_total")
    return entity


def get_or_create_entity(
    db: Session, investigation_id: str, entity_type: EntityType, value: str, claim_type: ClaimType
) -> Entity:
    normalized_value = value.strip().lower()
    entity = (
        db.query(Entity)
        .filter_by(investigation_id=investigation_id, entity_type=entity_type, normalized_value=normalized_value)
        .one_or_none()
    )
    if entity is None:
        entity = Entity(
            investigation_id=investigation_id,
            entity_type=entity_type,
            value=value,
            normalized_value=normalized_value,
            claim_type=claim_type,
        )
        db.add(entity)
        db.flush()
        metrics.increment("entities_total")
    return entity


def _recompute_confidence(db: Session, entity: Entity) -> None:
    evidences = db.query(Evidence).filter_by(entity_id=entity.id).all()
    if not evidences:
        return
    result = compute_confidence(
        source_names=[e.source_name for e in evidences],
        most_recent_observation=max(_aware(e.observed_at) for e in evidences),
    )
    entity.confidence = result.score
    entity.confidence_explanation = [
        {"label": f.label, "weight": f.weight, "detail": f.detail} for f in result.factors
    ]


def _merge_technical_metadata(investigation: Investigation, source: str, metadata: dict) -> None:
    merged = dict(investigation.technical_metadata or {})
    merged[source] = metadata
    investigation.technical_metadata = merged  # reassign: JSON column change-tracking needs a new object


def ingest_adapter_result(
    db: Session, investigation: Investigation, job_id: str, result: AdapterResult, anchor_entity: Entity | None = None
) -> Evidence:
    """`anchor_entity` is what new relationships connect FROM -- defaults to
    the investigation's own root entity, but the pivot engine passes a
    pivoted entity here so e.g. an email discovered mid-investigation gets
    its own HAS_PROFILE edges, not ones misattributed to the original root."""
    anchor = anchor_entity or get_or_create_root_entity(db, investigation)

    if result.claim_type == ClaimType.TECHNICAL:
        # Deterministic technical data (e.g. phonenumbers carrier/region) merges
        # into the investigation record, not the association graph.
        evidence = Evidence(
            investigation_id=investigation.id,
            entity_id=anchor.id,
            job_id=job_id,
            source_name=result.source,
            source_type="technical",
            query=result.query,
            raw_value=result.value,
            normalized_value=result.value,
            entity_type=result.entity_type.value,
            source_url=result.evidence.url if result.evidence else None,
            observed_at=result.observed_at,
            confidence=result.confidence,
            extraction_method=result.extraction_method,
            raw_metadata=result.evidence.metadata if result.evidence else {},
        )
        db.add(evidence)
        if result.evidence:
            _merge_technical_metadata(investigation, result.source, result.evidence.metadata)
        db.flush()
        return evidence

    child = get_or_create_entity(db, investigation.id, result.entity_type, result.value, result.claim_type)

    evidence = Evidence(
        investigation_id=investigation.id,
        entity_id=child.id,
        job_id=job_id,
        source_name=result.source,
        source_type="public_association",
        query=result.query,
        raw_value=result.value,
        normalized_value=child.normalized_value,
        entity_type=result.entity_type.value,
        source_url=result.evidence.url if result.evidence else None,
        observed_at=result.observed_at,
        confidence=result.confidence,
        extraction_method=result.extraction_method,
        raw_metadata=result.evidence.metadata if result.evidence else {},
    )
    db.add(evidence)
    db.flush()
    _recompute_confidence(db, child)

    rel_type = _RELATIONSHIP_RULES.get((anchor.entity_type, child.entity_type))
    if rel_type is not None and anchor.id != child.id:
        relationship = (
            db.query(EntityRelationship)
            .filter_by(
                investigation_id=investigation.id,
                source_entity_id=anchor.id,
                target_entity_id=child.id,
                relationship_type=rel_type,
            )
            .one_or_none()
        )
        if relationship is None:
            db.add(
                EntityRelationship(
                    investigation_id=investigation.id,
                    source_entity_id=anchor.id,
                    target_entity_id=child.id,
                    relationship_type=rel_type,
                    confidence=child.confidence,
                )
            )
        else:
            relationship.confidence = child.confidence

    return evidence
