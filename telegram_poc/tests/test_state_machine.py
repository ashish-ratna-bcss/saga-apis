import pytest

from telegram_app.database.models import SourceAccessStatus as S
from telegram_app.telegram.access_manager import InvalidStateTransition, transition


def test_discovered_to_public_accessible_allowed():
    assert transition(S.DISCOVERED, S.PUBLIC_ACCESSIBLE) == S.PUBLIC_ACCESSIBLE


def test_discovered_to_monitoring_blocked():
    """Cannot jump straight to MONITORING without passing through an accessible state."""
    with pytest.raises(InvalidStateTransition):
        transition(S.DISCOVERED, S.MONITORING)


def test_join_flow_full_path():
    assert transition(S.DISCOVERED, S.JOIN_REQUEST_REQUIRED) == S.JOIN_REQUEST_REQUIRED
    assert transition(S.JOIN_REQUEST_REQUIRED, S.JOIN_REQUEST_PENDING) == S.JOIN_REQUEST_PENDING
    assert transition(S.JOIN_REQUEST_PENDING, S.JOIN_REQUEST_APPROVED) == S.JOIN_REQUEST_APPROVED
    assert transition(S.JOIN_REQUEST_APPROVED, S.JOINED) == S.JOINED
    assert transition(S.JOINED, S.MONITORING) == S.MONITORING


def test_rejected_cannot_silently_retry_to_joined():
    assert transition(S.JOIN_REQUEST_PENDING, S.JOIN_REQUEST_REJECTED) == S.JOIN_REQUEST_REJECTED
    with pytest.raises(InvalidStateTransition):
        transition(S.JOIN_REQUEST_REJECTED, S.JOINED)


def test_rejected_can_only_reach_disabled_or_error():
    assert transition(S.JOIN_REQUEST_REJECTED, S.DISABLED) == S.DISABLED


def test_monitoring_can_error_and_disable():
    assert transition(S.MONITORING, S.ERROR) == S.ERROR
    assert transition(S.MONITORING, S.DISABLED) == S.DISABLED


def test_disabled_can_only_restart_from_discovered():
    assert transition(S.DISABLED, S.DISCOVERED) == S.DISCOVERED
    with pytest.raises(InvalidStateTransition):
        transition(S.DISABLED, S.MONITORING)


def test_accepts_string_values_too():
    assert transition("DISCOVERED", "ACCESSIBLE") == S.ACCESSIBLE
