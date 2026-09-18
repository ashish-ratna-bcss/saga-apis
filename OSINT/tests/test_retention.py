from datetime import UTC, datetime, timedelta

from osint_app.enums import IdentifierType
from osint_app.models import AuditLog, Investigation
from osint_app.retention import purge_expired


def _make_investigation(db_session, *, created_at) -> str:
    inv = Investigation(
        input_identifier="rahul_123", identifier_type=IdentifierType.USERNAME, normalized_identifier="rahul_123",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.query(Investigation).filter_by(id=inv.id).update({"created_at": created_at})
    db_session.commit()
    return inv.id


def test_retention_disabled_by_default_keeps_everything(db_session):
    old_id = _make_investigation(db_session, created_at=datetime.now(UTC) - timedelta(days=9999))
    deleted = purge_expired(db_session)
    assert deleted == {"investigations": 0, "audit_logs": 0}
    assert db_session.get(Investigation, old_id) is not None


def test_retention_purges_only_investigations_older_than_window(db_session, monkeypatch):
    from osint_app import retention

    monkeypatch.setattr(retention.settings, "investigation_retention_days", 30)

    old_id = _make_investigation(db_session, created_at=datetime.now(UTC) - timedelta(days=60))
    recent_id = _make_investigation(db_session, created_at=datetime.now(UTC) - timedelta(days=1))

    deleted = purge_expired(db_session)

    assert deleted["investigations"] == 1
    assert db_session.get(Investigation, old_id) is None
    assert db_session.get(Investigation, recent_id) is not None


def test_retention_purges_old_audit_logs_independently(db_session, monkeypatch):
    from osint_app import retention

    monkeypatch.setattr(retention.settings, "audit_log_retention_days", 7)

    old_log = AuditLog(
        request_id="r1", operation="GET /x", method="GET", path="/x", status_code=200, duration_ms=1.0,
    )
    db_session.add(old_log)
    db_session.commit()
    db_session.query(AuditLog).filter_by(id=old_log.id).update({"timestamp": datetime.now(UTC) - timedelta(days=30)})
    db_session.commit()

    recent_log = AuditLog(
        request_id="r2", operation="GET /y", method="GET", path="/y", status_code=200, duration_ms=1.0,
    )
    db_session.add(recent_log)
    db_session.commit()

    old_log_id, recent_log_id = old_log.id, recent_log.id
    deleted = purge_expired(db_session)

    assert deleted["audit_logs"] == 1
    assert db_session.query(AuditLog).filter_by(id=old_log_id).first() is None
    assert db_session.query(AuditLog).filter_by(id=recent_log_id).first() is not None
