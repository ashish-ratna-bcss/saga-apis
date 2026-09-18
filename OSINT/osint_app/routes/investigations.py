import hashlib
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from osint_app.config import settings
from osint_app.db import get_db
from osint_app.enums import TERMINAL_JOB_STATUSES, JobStatus, PivotStatus
from osint_app.graph import build_graph
from osint_app.models import Investigation, Pivot, SearchJob
from osint_app.normalization import NormalizationError, normalize_identifier
from osint_app.orchestrator import enqueue_investigation
from osint_app.report import build_report
from osint_app.schemas import (
    AnalystNotesUpdate,
    InvestigationCreateRequest,
    InvestigationDetail,
    InvestigationStatusOut,
    InvestigationSummary,
    JobCounts,
    PivotCounts,
)
from osint_app.timeline import build_timeline

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


def _get_or_404(db: Session, investigation_id: str) -> Investigation:
    investigation = db.get(Investigation, investigation_id)
    if investigation is None:
        raise HTTPException(status_code=404, detail="investigation not found")
    return investigation


def _idempotency_lookup(db: Session, scope: str, key: str, fingerprint: str) -> Investigation | None:
    existing = db.query(Investigation).filter_by(idempotency_scope=scope, idempotency_key=key).one_or_none()
    if existing is None:
        return None
    if existing.request_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail="Idempotency-Key already used with a different request body")
    return existing


@router.post("", response_model=InvestigationSummary, status_code=201)
def create_investigation(
    payload: InvestigationCreateRequest,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    scope = "global"  # no auth/tenancy -- see Investigation.idempotency_scope
    fingerprint = hashlib.sha256(json.dumps(payload.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    if idempotency_key:
        existing = _idempotency_lookup(db, scope, idempotency_key, fingerprint)
        if existing is not None:
            return existing

    in_flight = (
        db.query(Investigation).filter(Investigation.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])).count()
    )
    if in_flight >= settings.max_concurrent_investigations:
        raise HTTPException(
            status_code=429,
            detail=f"max_concurrent_investigations ({settings.max_concurrent_investigations}) reached, try again shortly",
        )

    try:
        identifier_type, normalized = normalize_identifier(payload.input_identifier, payload.identifier_type)
    except NormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    investigation = Investigation(
        input_identifier=payload.input_identifier,
        identifier_type=identifier_type,
        normalized_identifier=normalized,
        analyst_notes=payload.analyst_notes,
        mode=payload.mode,
        idempotency_scope=scope if idempotency_key else None,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint if idempotency_key else None,
    )
    db.add(investigation)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent request with the same key (unique
        # constraint uq_investigation_idempotency) -- the winner's row is
        # now visible, so return it instead of erroring.
        db.rollback()
        if not idempotency_key:
            raise
        existing = _idempotency_lookup(db, scope, idempotency_key, fingerprint)
        if existing is None:
            raise
        return existing
    db.refresh(investigation)

    enqueue_investigation(investigation.id)
    return investigation


@router.get("", response_model=list[InvestigationSummary])
def list_investigations(limit: int = 50, db: Session = Depends(get_db)):
    return db.query(Investigation).order_by(Investigation.created_at.desc()).limit(min(limit, 200)).all()


@router.get("/{investigation_id}", response_model=InvestigationDetail)
def get_investigation(investigation_id: str, db: Session = Depends(get_db)):
    investigation = _get_or_404(db, investigation_id)
    return {
        "id": investigation.id,
        "input_identifier": investigation.input_identifier,
        "identifier_type": investigation.identifier_type,
        "normalized_identifier": investigation.normalized_identifier,
        "mode": investigation.mode,
        "status": investigation.status,
        "cancel_requested": investigation.cancel_requested,
        "created_at": investigation.created_at,
        "updated_at": investigation.updated_at,
        "technical_metadata": investigation.technical_metadata,
        "analyst_notes": investigation.analyst_notes,
        "jobs": investigation.jobs,
        "entities": investigation.entities,
        "relationships": investigation.relationships_,
        "evidence": investigation.evidence,
        "pivots": investigation.pivots,
    }


@router.get("/{investigation_id}/status", response_model=InvestigationStatusOut)
def get_investigation_status(investigation_id: str, db: Session = Depends(get_db)):
    """Lightweight progress view for a polling consumer -- see SERVICE
    RELIABILITY. Cheaper than GET /{id} since it never loads entities/
    evidence, just job and pivot counts."""
    investigation = _get_or_404(db, investigation_id)

    jobs = db.query(SearchJob).filter_by(investigation_id=investigation_id).all()
    job_counts = JobCounts(total=len(jobs))
    for job in jobs:
        if hasattr(job_counts, job.status):
            setattr(job_counts, job.status, getattr(job_counts, job.status) + 1)

    pivots = db.query(Pivot).filter_by(investigation_id=investigation_id).all()
    pivot_counts = PivotCounts(total=len(pivots))
    for pivot in pivots:
        if pivot.status == PivotStatus.COMPLETED:
            pivot_counts.completed += 1
        elif pivot.status == PivotStatus.PENDING:
            pivot_counts.pending += 1
        elif pivot.status == PivotStatus.RUNNING:
            pivot_counts.running += 1
        elif pivot.status.startswith("skipped"):
            pivot_counts.skipped += 1

    created_at = investigation.created_at
    if created_at.tzinfo is None:  # SQLite drops tzinfo across a commit+reload, see entity_resolution._aware
        created_at = created_at.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - created_at).total_seconds()

    return InvestigationStatusOut(
        id=investigation.id,
        status=investigation.status,
        mode=investigation.mode,
        cancel_requested=investigation.cancel_requested,
        elapsed_seconds=round(elapsed, 3),
        jobs=job_counts,
        pivots=pivot_counts,
    )


@router.post("/{investigation_id}/cancel", response_model=InvestigationSummary)
def cancel_investigation(investigation_id: str, db: Session = Depends(get_db)):
    """Cooperative cancellation -- see JOB CANCELLATION. Stops new adapter
    calls / pivots from starting; work already in flight completes normally
    rather than being aborted mid-write."""
    investigation = _get_or_404(db, investigation_id)
    if investigation.status in TERMINAL_JOB_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"investigation is already {investigation.status}, nothing to cancel"
        )
    investigation.cancel_requested = True
    db.commit()
    db.refresh(investigation)
    return investigation


@router.get("/{investigation_id}/report")
def get_investigation_report(investigation_id: str, db: Session = Depends(get_db)):
    investigation = _get_or_404(db, investigation_id)
    return build_report(db, investigation)


@router.get("/{investigation_id}/graph")
def get_investigation_graph(investigation_id: str, db: Session = Depends(get_db)):
    investigation = _get_or_404(db, investigation_id)
    return build_graph(db, investigation)


@router.get("/{investigation_id}/timeline")
def get_investigation_timeline(investigation_id: str, db: Session = Depends(get_db)):
    investigation = _get_or_404(db, investigation_id)
    return build_timeline(db, investigation)


@router.put("/{investigation_id}/notes", response_model=InvestigationSummary)
def update_notes(investigation_id: str, payload: AnalystNotesUpdate, db: Session = Depends(get_db)):
    investigation = _get_or_404(db, investigation_id)
    investigation.analyst_notes = payload.analyst_notes
    db.commit()
    db.refresh(investigation)
    return investigation
