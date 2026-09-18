"""Configurable data retention (STEP 22) -- OFF by default (see
config.investigation_retention_days / audit_log_retention_days, both None =
keep forever). Investigations older than the configured window are deleted
along with their entities/evidence/jobs/pivots (ORM cascade="all,
delete-orphan", see models.py); AuditLog rows are pruned separately since
they're compliance/audit records, not investigation data, and may need a
different (often longer) window.

No built-in scheduler -- run via `python -m app.retention` from cron/systemd
timer, matching worker_main.py's standalone-entrypoint pattern.
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from osint_app.config import settings
from osint_app.models import AuditLog, Investigation

logger = logging.getLogger("retention")


def purge_expired(db: Session) -> dict[str, int]:
    deleted = {"investigations": 0, "audit_logs": 0}

    if settings.investigation_retention_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=settings.investigation_retention_days)
        stale = db.query(Investigation).filter(Investigation.created_at < cutoff).all()
        for investigation in stale:
            db.delete(investigation)  # cascades to jobs/entities/evidence/pivots
        deleted["investigations"] = len(stale)

    if settings.audit_log_retention_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=settings.audit_log_retention_days)
        # synchronize_session="fetch" (not False): keeps the session's identity
        # map consistent with the DB for any AuditLog rows already loaded in it,
        # not just correct for the next fresh query.
        deleted["audit_logs"] = db.query(AuditLog).filter(AuditLog.timestamp < cutoff).delete(
            synchronize_session="fetch"
        )

    db.commit()
    return deleted


if __name__ == "__main__":
    from osint_app.db import SessionLocal
    from osint_app.logging_config import configure_logging

    configure_logging()
    db = SessionLocal()
    try:
        result = purge_expired(db)
        logger.info("retention purge complete: %s", result)
    finally:
        db.close()
