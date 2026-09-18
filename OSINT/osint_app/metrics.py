"""In-process counters exposed at GET /metrics in Prometheus text exposition
format -- see STEP 18. Deliberately per-process, not cross-process: that's
the standard Prometheus model (each instance exposes its own counters, a
Prometheus server aggregates across scrapes), not a limitation to fix.

No `prometheus_client` dependency -- the exposition format is a handful of
plain-text lines, not worth a new dependency for.
"""
from collections import Counter

_counters: Counter[str] = Counter()

COUNTER_NAMES = (
    "investigations_total",
    "investigations_completed",
    "investigations_failed",
    "investigations_cancelled",
    "investigations_partial",
    "investigations_unavailable",
    "jobs_total",
    "jobs_completed",
    "jobs_failed",
    "jobs_unavailable",
    "jobs_timed_out",
    "source_success_total",
    "source_failure_total",
    "pivots_total",
    "entities_total",
)


def increment(name: str, value: int = 1) -> None:
    _counters[name] += value


def render_prometheus_text() -> str:
    lines = []
    for name in COUNTER_NAMES:
        lines.append(f"# TYPE osint_{name} counter")
        lines.append(f"osint_{name} {_counters.get(name, 0)}")
    return "\n".join(lines) + "\n"


def snapshot() -> dict[str, int]:
    """For tests -- avoids depending on exact text formatting."""
    return dict(_counters)
