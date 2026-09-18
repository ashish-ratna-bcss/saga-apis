import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from osint_app.db import Base
from osint_app.enums import ClaimType, EntityType, IdentifierType, JobStatus, PivotStatus, RelationshipType


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        UniqueConstraint("idempotency_scope", "idempotency_key", name="uq_investigation_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    input_identifier: Mapped[str] = mapped_column(Text)
    identifier_type: Mapped[IdentifierType] = mapped_column(String(20))
    normalized_identifier: Mapped[str] = mapped_column(Text, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="standard")  # quick | standard | deep, see INVESTIGATION MODES
    status: Mapped[JobStatus] = mapped_column(String(20), default=JobStatus.QUEUED)
    cancel_requested: Mapped[bool] = mapped_column(default=False)  # cooperative cancellation, see orchestrator.py
    technical_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    analyst_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # --- Postgres-backed queue (see app/job_queue.py, STEP 5/11) -- unused
    # (always NULL) on the SQLite/in-process-queue dev path.
    locked_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Idempotent creation (see STEP 7). This service has no auth/tenancy,
    # so idempotency_scope is always the literal "global" (set in
    # routes/investigations.py) -- kept as a column rather than collapsing
    # the unique constraint to idempotency_key alone, so a future caller
    # concept can reintroduce per-caller scoping without a schema change.
    # NULL (no Idempotency-Key header supplied) is allowed to repeat freely;
    # the unique constraint only bites once both are actually set.
    idempotency_scope: Mapped[str | None] = mapped_column(String(40), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)  # detects key-reuse-with-different-body

    jobs: Mapped[list["SearchJob"]] = relationship(back_populates="investigation", cascade="all, delete-orphan")
    entities: Mapped[list["Entity"]] = relationship(back_populates="investigation", cascade="all, delete-orphan")
    relationships_: Mapped[list["EntityRelationship"]] = relationship(
        back_populates="investigation", cascade="all, delete-orphan"
    )
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="investigation", cascade="all, delete-orphan")
    pivots: Mapped[list["Pivot"]] = relationship(cascade="all, delete-orphan")


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    source_name: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[JobStatus] = mapped_column(String(20), default=JobStatus.QUEUED)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    investigation: Mapped[Investigation] = relationship(back_populates="jobs")


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("investigation_id", "entity_type", "normalized_value", name="uq_entity_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    entity_type: Mapped[EntityType] = mapped_column(String(30))
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text, index=True)
    claim_type: Mapped[ClaimType] = mapped_column(String(20), default=ClaimType.PUBLIC_ASSOCIATION)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_explanation: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    investigation: Mapped[Investigation] = relationship(back_populates="entities")


class EntityRelationship(Base):
    __tablename__ = "entity_relationships"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "source_entity_id", "target_entity_id", "relationship_type",
            name="uq_relationship_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    source_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    target_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    relationship_type: Mapped[RelationshipType] = mapped_column(String(30))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    investigation: Mapped[Investigation] = relationship(back_populates="relationships_")


class Evidence(Base):
    """Evidence-first provenance record. Every entity/relationship must trace
    back to at least one row here -- see EVIDENCE-FIRST DESIGN in the spec."""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True, index=True)
    relationship_id: Mapped[str | None] = mapped_column(ForeignKey("entity_relationships.id"), nullable=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("search_jobs.id"), nullable=True)

    source_name: Mapped[str] = mapped_column(String(50))
    source_type: Mapped[str] = mapped_column(String(30))
    query: Mapped[str] = mapped_column(Text)
    raw_value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(String(30))
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    extraction_method: Mapped[str] = mapped_column(String(50))
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    investigation: Mapped[Investigation] = relationship(back_populates="evidence")


class Pivot(Base):
    """Records every automatic-pivot decision -- including ones that were
    SKIPPED (duplicate, low-confidence, limit-reached) -- so the pivot
    engine's behavior is auditable, not just its successes. See PIVOT SAFETY
    CONTROLS: every pivot records parent_entity, discovered value, source,
    reason, depth, and triggering evidence."""

    __tablename__ = "pivots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"), index=True)
    parent_entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id"), index=True)
    pivot_entity_id: Mapped[str | None] = mapped_column(ForeignKey("entities.id"), nullable=True)
    triggering_evidence_id: Mapped[str | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)

    identifier_type: Mapped[IdentifierType] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text, index=True)
    extraction_method: Mapped[str] = mapped_column(String(50))
    extraction_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer)
    status: Mapped[PivotStatus] = mapped_column(String(30), default=PivotStatus.PENDING)
    job_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    """Operational request log -- who did what, when, how long it took.
    Deliberately separate from investigation Evidence: this is service
    telemetry, not OSINT findings, and carries no raw PII beyond identifiers
    already visible to the requesting client itself (see STEP 16)."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    investigation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(50))
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(255))
    status_code: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column(Float)


class SourceHealth(Base):
    __tablename__ = "source_health"

    source_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    installed: Mapped[bool] = mapped_column(default=False)
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    timeout_count: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    run_count: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def success_rate(self) -> float | None:
        if self.run_count == 0:
            return None
        return round(self.success_count / self.run_count, 4)

    @property
    def average_duration_seconds(self) -> float | None:
        if self.run_count == 0:
            return None
        return round(self.total_duration_seconds / self.run_count, 3)
