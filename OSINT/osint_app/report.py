"""Investigation report generation -- see REPORTING / UI REQUIREMENT in the
spec. Groups discovered entities by type, separates TECHNICAL data from
PUBLIC_ASSOCIATION claims, and always states what Phase 1 cannot establish.
"""
from collections import Counter

from sqlalchemy.orm import Session

from osint_app.entity_resolution import root_entity_type
from osint_app.enums import ClaimType, JobStatus
from osint_app.models import Entity, EntityRelationship, Evidence, Investigation, Pivot, SearchJob

LIMITATIONS = [
    "This report contains public-source associations, not verified identity claims.",
    "A phone/email/username appearing alongside a name or company on the public web is "
    "correlation, not proof of ownership -- see each entity's claim_type and confidence.",
    "Technical phone data (carrier/region) describes the number range, not who currently holds the number.",
    "Sources that returned zero results were queried but found nothing public -- absence of "
    "evidence is not evidence of absence.",
    "Sources marked unavailable were not queried at all (dependency missing, not configured, or "
    "unreachable) -- their absence does not lower other sources' confidence.",
    "Phase 1 uses only free/public sources; no authoritative subscriber, KYC, or breach-email "
    "database was consulted.",
]


def build_report(db: Session, investigation: Investigation) -> dict:
    jobs = db.query(SearchJob).filter_by(investigation_id=investigation.id).all()
    entities = db.query(Entity).filter_by(investigation_id=investigation.id).all()
    relationships = db.query(EntityRelationship).filter_by(investigation_id=investigation.id).all()
    evidence = db.query(Evidence).filter_by(investigation_id=investigation.id).all()
    pivots = db.query(Pivot).filter_by(investigation_id=investigation.id).all()

    root_type = root_entity_type(investigation.identifier_type)
    root_normalized_value = investigation.normalized_identifier.lower()

    entities_by_type: dict[str, list[dict]] = {}
    for entity in entities:
        if entity.claim_type == ClaimType.TECHNICAL:
            continue  # technical data is reported separately, not as a discovered entity
        if entity.entity_type == root_type and entity.normalized_value == root_normalized_value:
            continue  # the investigator's own input, already shown in `case` -- not a "discovery"
        entities_by_type.setdefault(entity.entity_type, []).append(
            {
                "id": entity.id,
                "value": entity.value,
                "confidence": entity.confidence,
                "confidence_explanation": entity.confidence_explanation,
                "claim_type": entity.claim_type,
            }
        )

    return {
        "investigation_id": investigation.id,
        "case": {
            "input_identifier": investigation.input_identifier,
            "identifier_type": investigation.identifier_type,
            "normalized_identifier": investigation.normalized_identifier,
            "mode": investigation.mode,
            "search_timestamp": investigation.created_at.isoformat(),
            "status": investigation.status,
        },
        "sources": {
            "queried": sorted({j.source_name for j in jobs}),
            "completed": sorted({j.source_name for j in jobs if j.status == JobStatus.COMPLETED}),
            "unavailable": sorted({j.source_name for j in jobs if j.status == JobStatus.UNAVAILABLE}),
            "failed": sorted({j.source_name for j in jobs if j.status == JobStatus.FAILED}),
        },
        "technical_data": investigation.technical_metadata,
        "discovered_entities": entities_by_type,
        "relationships": [
            {
                "source_entity_id": r.source_entity_id,
                "target_entity_id": r.target_entity_id,
                "relationship_type": r.relationship_type,
                "confidence": r.confidence,
            }
            for r in relationships
        ],
        "evidence": [
            {
                "source_name": e.source_name,
                "source_type": e.source_type,
                "query": e.query,
                "raw_value": e.raw_value,
                "entity_type": e.entity_type,
                "source_url": e.source_url,
                "observed_at": e.observed_at.isoformat(),
                "confidence": e.confidence,
                "extraction_method": e.extraction_method,
            }
            for e in evidence
        ],
        "pivots": {
            "counts_by_status": dict(Counter(p.status for p in pivots)),
            "executed": [
                {
                    "parent_entity_id": p.parent_entity_id,
                    "pivot_entity_id": p.pivot_entity_id,
                    "identifier_type": p.identifier_type,
                    "value": p.value,
                    "depth": p.depth,
                    "reason": p.reason,
                    "extraction_confidence": p.extraction_confidence,
                }
                for p in pivots
                if p.pivot_entity_id is not None
            ],
        },
        "limitations": LIMITATIONS,
        "analyst_notes": investigation.analyst_notes,
    }
