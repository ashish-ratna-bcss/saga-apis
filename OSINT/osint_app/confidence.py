"""Explainable confidence engine.

Every score is the sum of named, capped factors -- never an opaque number.
Formula follows the spec's example weights; tune SOURCE_RELIABILITY and the
weights below as real-world precision/recall data comes in.
"""
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

RECENT_OBSERVATION_WINDOW = timedelta(days=90)

# Per-source trust prior (0..1), used for the "Source reliability" factor.
# Deliberately conservative for broad-scan tools (sherlock/maigret produce
# more false positives than a deterministic library like phonenumbers).
SOURCE_RELIABILITY: dict[str, float] = {
    "phonenumbers": 0.99,
    "hibp_pwned_passwords": 0.95,
    "holehe": 0.80,
    "maigret": 0.65,
    "sherlock": 0.60,
    "public_web": 0.50,
}
DEFAULT_SOURCE_RELIABILITY = 0.50
RELIABILITY_THRESHOLD = 0.70


@dataclass
class ConfidenceFactor:
    label: str
    weight: float
    detail: str


@dataclass
class ConfidenceResult:
    score: float
    factors: list[ConfidenceFactor] = field(default_factory=list)

    def explanation(self) -> list[str]:
        return [f"✓ {f.label} (+{f.weight:.2f}) — {f.detail}" for f in self.factors]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "factors": [{"label": f.label, "weight": f.weight, "detail": f.detail} for f in self.factors],
            "explanation": self.explanation(),
        }


def compute_confidence(
    *,
    source_names: list[str],
    exact_match: bool = False,
    name_company_agreement: bool = False,
    most_recent_observation: datetime | None = None,
    now: datetime | None = None,
) -> ConfidenceResult:
    """source_names: one entry per corroborating piece of evidence (source_name
    of each). Independence is measured by distinct source names."""
    now = now or datetime.now(UTC)
    distinct_sources = sorted(set(source_names))
    factors: list[ConfidenceFactor] = []
    score = 0.0

    if len(distinct_sources) >= 1:
        factors.append(ConfidenceFactor("Direct public source", 0.30, f"observed via {distinct_sources[0]}"))
        score += 0.30
    if len(distinct_sources) >= 2:
        factors.append(
            ConfidenceFactor("Independent second source", 0.20, f"corroborated by {distinct_sources[1]}")
        )
        score += 0.20
    if len(distinct_sources) >= 3:
        factors.append(
            ConfidenceFactor(
                "Third independent source", 0.15, f"{len(distinct_sources)} distinct sources corroborate"
            )
        )
        score += 0.15
    if exact_match:
        factors.append(ConfidenceFactor("Exact phone/email match", 0.15, "normalized value matched exactly"))
        score += 0.15
    if name_company_agreement:
        factors.append(ConfidenceFactor("Name + company agreement", 0.10, "name and company co-occur in a source"))
        score += 0.10
    if most_recent_observation is not None:
        observed = most_recent_observation
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        if now - observed <= RECENT_OBSERVATION_WINDOW:
            factors.append(
                ConfidenceFactor("Recent observation", 0.05, f"observed within {RECENT_OBSERVATION_WINDOW.days}d")
            )
            score += 0.05

    reliabilities = [SOURCE_RELIABILITY.get(s, DEFAULT_SOURCE_RELIABILITY) for s in distinct_sources]
    if reliabilities and (sum(reliabilities) / len(reliabilities)) >= RELIABILITY_THRESHOLD:
        factors.append(ConfidenceFactor("Source reliability", 0.05, "average source trust ≥ 0.70"))
        score += 0.05

    return ConfidenceResult(score=round(min(score, 1.0), 4), factors=factors)
