import enum


class IdentifierType(enum.StrEnum):
    PHONE = "PHONE"
    EMAIL = "EMAIL"
    USERNAME = "USERNAME"
    PERSON_NAME = "PERSON_NAME"
    DOMAIN = "DOMAIN"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.CANCELLED, JobStatus.UNAVAILABLE}
)

# Investigation status state machine (STEP 6): guards against a worker whose
# lease was reclaimed (see job_queue.py) writing a stale result after
# another worker already finished the same investigation -- terminal
# statuses have no outgoing edges, so once set they can never be overwritten.
ALLOWED_STATUS_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.QUEUED, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.UNAVAILABLE, JobStatus.CANCELLED}
    ),
}


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    if current == target:
        return True  # idempotent re-write (e.g. cancel requested twice) is not an error
    return target in ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())


class EntityType(enum.StrEnum):
    PERSON = "Person"
    PHONE = "Phone"
    EMAIL = "Email"
    USERNAME = "Username"
    SOCIAL_PROFILE = "SocialProfile"
    COMPANY = "Company"
    DOMAIN = "Domain"
    URL = "URL"
    DOCUMENT = "Document"
    ORGANIZATION = "Organization"
    LOCATION = "Location"
    BREACH = "Breach"


class RelationshipType(enum.StrEnum):
    HAS_PHONE = "HAS_PHONE"
    HAS_EMAIL = "HAS_EMAIL"
    USES_USERNAME = "USES_USERNAME"
    HAS_PROFILE = "HAS_PROFILE"
    ASSOCIATED_WITH = "ASSOCIATED_WITH"
    OWNS_DOMAIN = "OWNS_DOMAIN"
    MENTIONED_IN = "MENTIONED_IN"
    SUPPORTED_BY = "SUPPORTED_BY"
    DISCOVERED_FROM = "DISCOVERED_FROM"  # pivot edge: candidate entity <- extracted from parent entity's evidence


class PivotStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_LOW_CONFIDENCE = "skipped_low_confidence"
    SKIPPED_LIMIT_REACHED = "skipped_limit_reached"
    SKIPPED_SELF_REFERENCE = "skipped_self_reference"


class ClaimType(enum.StrEnum):
    """Distinguishes what kind of claim a piece of data represents.

    TECHNICAL: derived deterministically from the identifier itself (e.g.
    phonenumbers carrier/region lookup) -- not evidence of who owns it.
    PUBLIC_ASSOCIATION: an entity/relationship inferred from public-source
    correlation, always carries a confidence score below certainty.
    IDENTITY_CLAIM: an authoritative, verified identity match. Phase 1 has no
    source capable of producing this -- reserved for Phase 2 providers.
    """

    TECHNICAL = "TECHNICAL"
    PUBLIC_ASSOCIATION = "PUBLIC_ASSOCIATION"
    IDENTITY_CLAIM = "IDENTITY_CLAIM"
