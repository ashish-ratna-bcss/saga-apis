"""Automatic pivot engine: decides whether evidence just collected for one
entity contains a candidate identifier worth investigating further.

This module never runs an adapter itself -- it only decides *what* to pivot
into (creating the candidate Entity + a DISCOVERED_FROM edge + a Pivot audit
row) and hands the caller (orchestrator.py) a list of approved targets to
actually execute. Every decision is recorded, including skips -- see
PIVOT SAFETY CONTROLS: duplicate/self-reference/low-confidence/limit-reached
pivots are never silently dropped, they're written with a status explaining
why nothing happened.
"""
from collections import Counter
from dataclasses import dataclass

from sqlalchemy.orm import Session

from osint_app import metrics
from osint_app.config import settings
from osint_app.entity_resolution import get_or_create_entity  # reuse the one fingerprinting implementation
from osint_app.enums import IdentifierType, PivotStatus, RelationshipType
from osint_app.extraction import (
    DOMAIN_BASE_CONFIDENCE,
    DOMAIN_CORROBORATED_CONFIDENCE,
    Candidate,
    extract_all,
    extract_domain_from_url,
    is_generic_domain,
)
from osint_app.models import Entity, EntityRelationship, Evidence, Investigation, Pivot
from osint_app.normalization import NormalizationError, normalize_identifier

_PIVOT_ROOT_ENTITY_TYPE = {
    IdentifierType.PHONE: "Phone",
    IdentifierType.EMAIL: "Email",
    IdentifierType.USERNAME: "Username",
    IdentifierType.DOMAIN: "Domain",
    # PERSON_NAME intentionally absent -- not extracted from free text, see extraction.py
}


@dataclass(frozen=True)
class PivotTarget:
    """An approved pivot the orchestrator should actually go execute."""

    pivot_id: str
    parent_entity: Entity
    pivot_entity: Entity
    identifier_type: IdentifierType
    normalized_value: str
    depth: int


def _active_pivot_count(db: Session, investigation_id: str, *, parent_entity_id: str | None = None) -> int:
    q = db.query(Pivot).filter(
        Pivot.investigation_id == investigation_id,
        Pivot.status.in_([PivotStatus.PENDING, PivotStatus.RUNNING, PivotStatus.COMPLETED]),
    )
    if parent_entity_id is not None:
        q = q.filter(Pivot.parent_entity_id == parent_entity_id)
    return q.count()


def _record_skip(
    db: Session,
    investigation: Investigation,
    parent_entity: Entity,
    candidate: Candidate,
    normalized_value: str,
    status: PivotStatus,
    depth: int,
    evidence_id: str | None,
    reason: str,
) -> None:
    db.add(
        Pivot(
            investigation_id=investigation.id,
            parent_entity_id=parent_entity.id,
            pivot_entity_id=None,
            triggering_evidence_id=evidence_id,
            identifier_type=candidate.identifier_type,
            value=candidate.value,
            normalized_value=normalized_value,
            extraction_method=candidate.extraction_method,
            extraction_confidence=candidate.confidence,
            reason=reason,
            depth=depth,
            status=status,
        )
    )


def _consider_candidate(
    db: Session,
    investigation: Investigation,
    parent_entity: Entity,
    candidate: Candidate,
    depth: int,
    evidence_id: str | None,
) -> PivotTarget | None:
    if candidate.identifier_type not in _PIVOT_ROOT_ENTITY_TYPE:
        return None

    try:
        _, normalized_value = normalize_identifier(candidate.value, candidate.identifier_type)
    except NormalizationError:
        return None  # not actually valid data -- nothing to audit, it was never a real candidate

    root_entity_type = _PIVOT_ROOT_ENTITY_TYPE[candidate.identifier_type]

    existing = (
        db.query(Entity)
        .filter_by(investigation_id=investigation.id, entity_type=root_entity_type, normalized_value=normalized_value)
        .one_or_none()
    )
    if existing is not None:
        # Already the investigation's own root, or already pivoted-into by an
        # earlier candidate -- either way, no new work, but a second
        # independent discovery path is still valuable corroboration.
        _add_discovered_from_edge(db, investigation, pivot_entity=existing, parent_entity=parent_entity)
        status = (
            PivotStatus.SKIPPED_SELF_REFERENCE
            if existing.normalized_value == investigation.normalized_identifier.lower()
            else PivotStatus.SKIPPED_DUPLICATE
        )
        _record_skip(
            db, investigation, parent_entity, candidate, normalized_value, status, depth, evidence_id,
            reason=f"{root_entity_type} '{normalized_value}' already known in this investigation",
        )
        return None

    if candidate.confidence < settings.pivot_confidence_threshold:
        _record_skip(
            db, investigation, parent_entity, candidate, normalized_value, PivotStatus.SKIPPED_LOW_CONFIDENCE,
            depth, evidence_id,
            reason=f"extraction confidence {candidate.confidence} below threshold {settings.pivot_confidence_threshold}",
        )
        return None

    if _active_pivot_count(db, investigation.id) >= settings.max_total_pivots_per_investigation:
        _record_skip(
            db, investigation, parent_entity, candidate, normalized_value, PivotStatus.SKIPPED_LIMIT_REACHED,
            depth, evidence_id, reason="max_total_pivots_per_investigation reached",
        )
        return None
    if _active_pivot_count(db, investigation.id, parent_entity_id=parent_entity.id) >= settings.max_pivots_per_entity:
        _record_skip(
            db, investigation, parent_entity, candidate, normalized_value, PivotStatus.SKIPPED_LIMIT_REACHED,
            depth, evidence_id, reason="max_pivots_per_entity reached for this parent",
        )
        return None

    pivot_entity = get_or_create_entity(
        db, investigation.id, root_entity_type, normalized_value, claim_type=parent_entity.claim_type
    )
    _add_discovered_from_edge(db, investigation, pivot_entity=pivot_entity, parent_entity=parent_entity)

    pivot = Pivot(
        investigation_id=investigation.id,
        parent_entity_id=parent_entity.id,
        pivot_entity_id=pivot_entity.id,
        triggering_evidence_id=evidence_id,
        identifier_type=candidate.identifier_type,
        value=candidate.value,
        normalized_value=normalized_value,
        extraction_method=candidate.extraction_method,
        extraction_confidence=candidate.confidence,
        reason=f"{candidate.extraction_method} found {candidate.identifier_type} in evidence from {parent_entity.entity_type}",
        depth=depth,
        status=PivotStatus.PENDING,
    )
    db.add(pivot)
    db.flush()
    metrics.increment("pivots_total")

    return PivotTarget(
        pivot_id=pivot.id,
        parent_entity=parent_entity,
        pivot_entity=pivot_entity,
        identifier_type=candidate.identifier_type,
        normalized_value=normalized_value,
        depth=depth,
    )


def _add_discovered_from_edge(db: Session, investigation: Investigation, *, pivot_entity: Entity, parent_entity: Entity) -> None:
    if pivot_entity.id == parent_entity.id:
        return
    existing = (
        db.query(EntityRelationship)
        .filter_by(
            investigation_id=investigation.id,
            source_entity_id=pivot_entity.id,
            target_entity_id=parent_entity.id,
            relationship_type=RelationshipType.DISCOVERED_FROM,
        )
        .one_or_none()
    )
    if existing is None:
        db.add(
            EntityRelationship(
                investigation_id=investigation.id,
                source_entity_id=pivot_entity.id,
                target_entity_id=parent_entity.id,
                relationship_type=RelationshipType.DISCOVERED_FROM,
                confidence=pivot_entity.confidence,
            )
        )


def evaluate_pivots(
    db: Session,
    investigation: Investigation,
    parent_entity: Entity,
    evidence_rows: list[Evidence],
    depth: int,
    max_depth: int,
) -> list[PivotTarget]:
    """Given the Evidence rows just created for `parent_entity` at this
    depth, extract candidate identifiers and decide which are worth pivoting
    into. Returns approved targets for the orchestrator to execute.

    `max_depth` is passed in (not read from settings directly) because it
    varies per investigation MODE (quick/standard/deep) -- reading a global
    here would let concurrent investigations in different modes race."""
    if not settings.pivot_enabled or depth > max_depth:
        return []

    targets: list[PivotTarget] = []
    domain_occurrences: Counter[str] = Counter()
    domain_first_evidence: dict[str, Evidence] = {}

    for evidence in evidence_rows:
        text = f"{evidence.raw_metadata.get('title', '')} {evidence.raw_metadata.get('snippet', '')} {evidence.raw_value}"
        url = evidence.source_url or (evidence.raw_value if evidence.raw_value.startswith("http") else None)

        for candidate in extract_all(text, url=url):
            target = _consider_candidate(db, investigation, parent_entity, candidate, depth, evidence.id)
            if target is not None:
                targets.append(target)

        if url:
            domain = extract_domain_from_url(url)
            if domain and not is_generic_domain(domain):
                domain_occurrences[domain] += 1
                domain_first_evidence.setdefault(domain, evidence)

    for domain, count in domain_occurrences.items():
        confidence = DOMAIN_CORROBORATED_CONFIDENCE if count >= 2 else DOMAIN_BASE_CONFIDENCE
        candidate = Candidate(IdentifierType.DOMAIN, domain, confidence, "url_domain_frequency")
        target = _consider_candidate(
            db, investigation, parent_entity, candidate, depth, domain_first_evidence[domain].id
        )
        if target is not None:
            targets.append(target)

    db.commit()
    return targets


def mark_pivot_status(db: Session, pivot_id: str, status: PivotStatus, job_ids: list[str] | None = None) -> None:
    pivot = db.get(Pivot, pivot_id)
    if pivot is None:
        return
    pivot.status = status
    if job_ids:
        pivot.job_ids = [*pivot.job_ids, *job_ids]
    db.commit()
