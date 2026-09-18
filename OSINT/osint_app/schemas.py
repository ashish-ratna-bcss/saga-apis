from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from osint_app.enums import ClaimType, EntityType, IdentifierType, JobStatus, PivotStatus, RelationshipType

InvestigationMode = Literal["quick", "standard", "deep"]


class InvestigationCreateRequest(BaseModel):
    input_identifier: str
    identifier_type: IdentifierType | None = None
    analyst_notes: str | None = None
    mode: InvestigationMode = "standard"


class InvestigationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    input_identifier: str
    identifier_type: IdentifierType
    normalized_identifier: str
    mode: str
    status: JobStatus
    cancel_requested: bool
    created_at: datetime
    updated_at: datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_name: str
    status: JobStatus
    error: str | None
    result_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entity_type: EntityType
    value: str
    normalized_value: str
    claim_type: ClaimType
    confidence: float
    confidence_explanation: list
    created_at: datetime


class RelationshipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: RelationshipType
    confidence: float


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entity_id: str | None
    job_id: str | None
    source_name: str
    source_type: str
    query: str
    raw_value: str
    normalized_value: str
    entity_type: str
    source_url: str | None
    observed_at: datetime
    confidence: float
    extraction_method: str
    raw_metadata: dict


class PivotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_entity_id: str
    pivot_entity_id: str | None
    identifier_type: IdentifierType
    value: str
    normalized_value: str
    extraction_method: str
    extraction_confidence: float
    reason: str
    depth: int
    status: PivotStatus
    job_ids: list
    created_at: datetime


class InvestigationDetail(InvestigationSummary):
    technical_metadata: dict
    analyst_notes: str | None
    jobs: list[JobOut]
    entities: list[EntityOut]
    relationships: list[RelationshipOut]
    evidence: list[EvidenceOut]
    pivots: list[PivotOut]


class AnalystNotesUpdate(BaseModel):
    analyst_notes: str


class SourceHealthOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_name: str
    enabled: bool
    installed: bool
    version: str | None
    last_success: datetime | None
    last_failure: datetime | None
    last_error: str | None
    success_count: int
    failure_count: int
    timeout_count: int
    success_rate: float | None
    average_duration_seconds: float | None
    run_count: int
    # working | unavailable | disabled | degraded -- see routes/sources.py's
    # _compute_status for exactly how each is derived.
    # Defaulted (not present on the SourceHealth ORM model) so
    # model_validate(health) succeeds; routes/sources.py always overrides it.
    status: str = "working"


class JobCounts(BaseModel):
    total: int = 0
    queued: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    unavailable: int = 0
    cancelled: int = 0


class PivotCounts(BaseModel):
    total: int = 0
    pending: int = 0
    running: int = 0
    completed: int = 0
    skipped: int = 0


class InvestigationStatusOut(BaseModel):
    """Lightweight progress view -- cheap to poll frequently, unlike the
    full detail endpoint (which loads every entity/evidence/relationship
    row). See SERVICE RELIABILITY / progress reporting."""

    id: str
    status: JobStatus
    mode: str
    cancel_requested: bool
    elapsed_seconds: float
    jobs: JobCounts
    pivots: PivotCounts


class PwnedPasswordRequest(BaseModel):
    password: str


class PwnedPasswordResponse(BaseModel):
    pwned: bool
    times_seen: int
    note: str = "HIBP Pwned Passwords checks the PASSWORD, not the email/account. It is not an email-breach lookup."


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    language: str = "en"
    safesearch: int = Field(default=0, ge=0, le=2)


class SearchResultOut(BaseModel):
    title: str
    url: str
    snippet: str
    engine: str
    category: str
    published_at: str | None = None


class SearchMetaOut(BaseModel):
    total: int
    successful_engines: list[str]
    failed_engines: list[str]


class SearchResponseOut(BaseModel):
    query: str
    results: list[SearchResultOut]
    meta: SearchMetaOut


class PhoneLookupRequest(BaseModel):
    phone: str = Field(min_length=1)


class EmailLookupRequest(BaseModel):
    email: str = Field(min_length=1)


class UsernameLookupRequest(BaseModel):
    username: str = Field(min_length=1)


class PersonLookupRequest(BaseModel):
    person_name: str = Field(min_length=1)


class DomainLookupRequest(BaseModel):
    domain: str = Field(min_length=1)


class LookupResultOut(BaseModel):
    """Response shape for the direct feature endpoints (/phone/lookup etc.)
    -- a synchronous, single-identifier-type slice of the same evidence/
    entity pipeline full investigations use, see routes/lookups.py."""

    investigation_id: str
    input_identifier: str
    identifier_type: IdentifierType
    normalized_identifier: str
    status: JobStatus
    jobs: list[JobOut]
    entities: list[EntityOut]
    evidence: list[EvidenceOut]
