"""SourceAdapter ABC + the normalized result shape every adapter must return.

No adapter, anywhere in the app, is allowed to leak its own tool-specific
response shape past this boundary (see SOURCE ADAPTER ARCHITECTURE).
"""
import abc
import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime

from osint_app.enums import ClaimType, EntityType


class AdapterStatus(enum.StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"  # tool not installed / dependency missing -- never faked
    ERROR = "error"


@dataclass
class AdapterEvidence:
    url: str | None
    title: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AdapterResult:
    """The one normalized shape every adapter emits, regardless of backing tool."""

    source: str
    query: str
    entity_type: EntityType
    value: str
    status: AdapterStatus
    claim_type: ClaimType = ClaimType.PUBLIC_ASSOCIATION
    confidence: float = 0.0
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence: AdapterEvidence | None = None
    extraction_method: str = "adapter"


class SourceUnavailable(Exception):
    """Raised by an adapter when its backing tool/dependency isn't usable.

    Caught by the orchestrator and turned into a JobStatus.UNAVAILABLE job --
    never silently swallowed into a fake/empty success result.

    `retryable` distinguishes a transient condition (network blip, timeout --
    worth a bounded retry) from a permanent one (CAPTCHA-gated, blocked,
    misconfigured -- retrying can never help). Defaults to True since most
    SourceUnavailable raises in this codebase wrap a network error; an
    adapter that is *deliberately* always-blocked would pass retryable=False
    explicitly instead."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class SourceAdapter(abc.ABC):
    #: unique key used in SourceHealth, Evidence.source_name, confidence weighting
    name: str
    #: EntityType this adapter's identifier applies to (PHONE/EMAIL/USERNAME/DOMAIN)
    accepts: EntityType

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Cheap check: is the backing tool/dependency importable & usable?"""

    @abc.abstractmethod
    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        """Execute the source. Raise SourceUnavailable if it can't run at all.
        Return [] (not an exception) when the tool ran fine but found nothing."""
