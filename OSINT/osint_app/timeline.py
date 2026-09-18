"""Machine-readable investigation timeline -- lets a consuming application
build its own UI/history view later (see ITERATION 9)."""
from sqlalchemy.orm import Session

from osint_app.models import Entity, Investigation, Pivot, SearchJob


def build_timeline(db: Session, investigation: Investigation) -> list[dict]:
    events: list[dict] = [
        {
            "timestamp": investigation.created_at.isoformat(),
            "event_type": "investigation_created",
            "identifier_type": investigation.identifier_type,
        }
    ]

    for job in db.query(SearchJob).filter_by(investigation_id=investigation.id).all():
        if job.started_at:
            events.append(
                {"timestamp": job.started_at.isoformat(), "event_type": "job_started", "source": job.source_name, "job_id": job.id}
            )
        if job.finished_at:
            events.append(
                {
                    "timestamp": job.finished_at.isoformat(),
                    "event_type": f"job_{job.status}",
                    "source": job.source_name,
                    "job_id": job.id,
                    "result_count": job.result_count,
                }
            )

    for entity in db.query(Entity).filter_by(investigation_id=investigation.id).all():
        events.append(
            {
                "timestamp": entity.created_at.isoformat(),
                "event_type": "entity_discovered",
                "entity_id": entity.id,
                "entity_type": entity.entity_type,
                "confidence": entity.confidence,
            }
        )

    for pivot in db.query(Pivot).filter_by(investigation_id=investigation.id).all():
        events.append(
            {
                "timestamp": pivot.created_at.isoformat(),
                "event_type": "pivot_decided",
                "status": pivot.status,
                "identifier_type": pivot.identifier_type,
                "parent_entity_id": pivot.parent_entity_id,
                "pivot_entity_id": pivot.pivot_entity_id,
                "depth": pivot.depth,
                "reason": pivot.reason,
                "source": pivot.extraction_method,
            }
        )

    events.sort(key=lambda e: e["timestamp"])
    return events
