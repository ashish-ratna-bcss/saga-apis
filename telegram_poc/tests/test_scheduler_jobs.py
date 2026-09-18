"""Covers Scenario B (approval -> auto monitoring) and Scenario C (rejection)
from the acceptance criteria, driven through the actual scheduler job
functions against an in-memory DB and a monkeypatched session factory."""
from telethon.errors import ChannelPrivateError, InviteHashInvalidError, InviteRequestSentError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

import telegram_app.scheduler.jobs as jobs_module
from telegram_app.database.models import SourceAccessStatus
from telegram_app.services.notification_service import EVENT_ACCESS_GRANTED, EVENT_ACCESS_REJECTED, NotificationService
from telegram_app.services.source_service import SourceService
from tests.fakes import FakeChannel, FakeClient, SingleSessionFactory as _SingleSessionFactory, StubClientManager, make_chat_invite


async def _register_pending_username_source(session):
    channel = FakeChannel(id=300, title="Waiting Room", username="waitingroom", left=True)
    fake_client = FakeClient(
        entities={"waitingroom": channel},
        probe_raises={300: ChannelPrivateError(None)},
        rpc_raises={JoinChannelRequest: InviteRequestSentError(None)},
    )
    manager = StubClientManager(fake_client)
    source_service = SourceService(session, manager)
    source = await source_service.register_source("@waitingroom")
    assert source.access_status == SourceAccessStatus.JOIN_REQUEST_REQUIRED.value
    await source_service.request_access(source.id)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value
    return refreshed, manager, fake_client


async def _register_pending_invite_source(session, *, invite_hash="pendinginvite"):
    fake_client = FakeClient(
        rpc_responses={CheckChatInviteRequest: make_chat_invite(title="Invite Only", request_needed=True)},
        rpc_raises={ImportChatInviteRequest: InviteRequestSentError(None)},
    )
    manager = StubClientManager(fake_client)
    source_service = SourceService(session, manager)
    source = await source_service.register_source(f"https://t.me/+{invite_hash}")
    assert source.access_status == SourceAccessStatus.JOIN_REQUEST_REQUIRED.value
    await source_service.request_access(source.id)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value
    return refreshed, manager, fake_client


async def test_scenario_b_reconciliation_detects_approval_and_starts_monitoring(session, settings, monkeypatch):
    source, manager, fake_client = await _register_pending_username_source(session)

    # Telegram now reports the account as an actual member (left=False) -
    # i.e. the join request was approved.
    fake_client.entities["waitingroom"].left = False
    fake_client.messages_by_entity_id[300] = []
    del fake_client.probe_raises[300]

    monkeypatch.setattr(jobs_module, "async_session_factory", _SingleSessionFactory(session))

    await jobs_module.reconcile_pending_access(manager, settings)

    source_service = SourceService(session, manager)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.MONITORING.value

    notifications = await NotificationService(session).list_notifications()
    assert any(n.event_type == EVENT_ACCESS_GRANTED for n in notifications)


async def test_scenario_c_reconciliation_detects_rejection_and_does_not_start_monitoring(session, settings, monkeypatch):
    source, manager, fake_client = await _register_pending_invite_source(session, invite_hash="deniedinvite")
    # Simulate the invite becoming invalid (declined/revoked) while pending.
    fake_client.rpc_raises[CheckChatInviteRequest] = InviteHashInvalidError(None)

    monkeypatch.setattr(jobs_module, "async_session_factory", _SingleSessionFactory(session))

    await jobs_module.reconcile_pending_access(manager, settings)

    source_service = SourceService(session, manager)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_REJECTED.value
    assert refreshed.monitoring_enabled is False

    notifications = await NotificationService(session).list_notifications()
    assert any(n.event_type == EVENT_ACCESS_REJECTED for n in notifications)


async def test_reconciliation_still_pending_makes_no_state_change(session, settings, monkeypatch):
    source, manager, fake_client = await _register_pending_invite_source(session, invite_hash="stillpending")

    monkeypatch.setattr(jobs_module, "async_session_factory", _SingleSessionFactory(session))
    await jobs_module.reconcile_pending_access(manager, settings)

    source_service = SourceService(session, manager)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value


async def test_reconciliation_skips_and_does_not_hammer_when_session_invalid(session, settings, monkeypatch):
    """Scheduler (requirement 9): if Telegram authentication is invalid, do
    not repeatedly hammer Telegram, record the failure, and leave the
    pending request/source untouched/unmisclassified for recovery."""
    source, manager, fake_client = await _register_pending_username_source(session)
    monkeypatch.setattr(jobs_module, "async_session_factory", _SingleSessionFactory(session))

    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")
    fake_client._authorized = False

    rpc_calls_before = len(fake_client.calls)
    await jobs_module.reconcile_pending_access(manager, settings)
    rpc_calls_after = len(fake_client.calls)

    # verify_authorized() short-circuited before any per-request Telegram call.
    assert rpc_calls_after == rpc_calls_before

    source_service = SourceService(session, manager)
    refreshed = await source_service.get_source(source.id)
    # Still pending - must not have been reclassified as rejected/denied.
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value


async def test_reconciliation_recovers_after_reauthentication(session, settings, monkeypatch):
    source, manager, fake_client = await _register_pending_username_source(session)
    monkeypatch.setattr(jobs_module, "async_session_factory", _SingleSessionFactory(session))

    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")
    fake_client._authorized = False
    await jobs_module.reconcile_pending_access(manager, settings)

    source_service = SourceService(session, manager)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value

    # Re-authentication succeeds, and the join request is now actually approved.
    fake_client._authorized = True
    manager.clear_session_invalid()
    fake_client.entities["waitingroom"].left = False
    fake_client.messages_by_entity_id[300] = []
    del fake_client.probe_raises[300]

    await jobs_module.reconcile_pending_access(manager, settings)

    refreshed2 = await source_service.get_source(source.id)
    assert refreshed2.access_status == SourceAccessStatus.MONITORING.value
