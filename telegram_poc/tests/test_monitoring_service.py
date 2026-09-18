"""Covers Scenario A (public channel: register -> monitor -> collect ->
"restart" -> incremental collection with no duplicates) and Scenario D
(collection-time error handling) from the acceptance criteria."""
import pytest

from telegram_app.database.models import SourceAccessStatus
from telegram_app.services.message_service import MessageService
from telegram_app.services.monitoring_service import MonitoringService
from telegram_app.services.source_service import SourceService
from tests.fakes import FakeChannel, FakeClient, FakeMessage, StubClientManager


async def test_scenario_a_public_channel_full_lifecycle(session, settings):
    channel = FakeChannel(id=200, title="Public News", username="publicnews", broadcast=True, left=True)
    initial_messages = [FakeMessage(id=i, message=f"msg {i}") for i in range(1, 4)]
    fake_client = FakeClient(entities={"publicnews": channel}, messages_by_entity_id={200: initial_messages})
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@publicnews")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    monitoring_service = MonitoringService(session, manager, settings)
    source = await monitoring_service.start_monitoring(source.id)

    assert source.access_status == SourceAccessStatus.MONITORING.value
    assert source.last_message_id == 3

    message_service = MessageService(session, settings)
    count = await message_service.messages.count_for_source(source.id)
    assert count == 3

    # "Restart": new service instances over the same underlying data, re-run
    # collection with no new messages available - nothing should duplicate.
    monitoring_service_2 = MonitoringService(session, manager, settings)
    result = await monitoring_service_2.run_collection_for_source(source.id)
    assert result["messages_inserted"] == 0
    assert result["messages_skipped"] == 0  # nothing to skip either - iter_messages(min_id=3) returns nothing

    count_after_restart = await message_service.messages.count_for_source(source.id)
    assert count_after_restart == 3

    # New messages arrive - incremental collection picks up only the new ones.
    fake_client.messages_by_entity_id[200].extend([FakeMessage(id=4, message="msg 4"), FakeMessage(id=5, message="msg 5")])
    result2 = await monitoring_service_2.run_collection_for_source(source.id)
    assert result2["messages_inserted"] == 2

    final_count = await message_service.messages.count_for_source(source.id)
    assert final_count == 5

    refreshed = await source_service.get_source(source.id)
    assert refreshed.last_message_id == 5


async def test_start_monitoring_rejected_before_access_verified(session, settings):
    from telegram_app.services.monitoring_service import MonitoringStateError

    channel = FakeChannel(id=201, title="Needs Join", username="needsjoin", left=True)
    fake_client = FakeClient(entities={"needsjoin": channel}, probe_raises={})
    from telethon.errors import ChannelPrivateError

    fake_client.probe_raises[201] = ChannelPrivateError(None)
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@needsjoin")
    assert source.access_status == SourceAccessStatus.JOIN_REQUEST_REQUIRED.value

    monitoring_service = MonitoringService(session, manager, settings)
    try:
        await monitoring_service.start_monitoring(source.id)
        assert False, "should not be able to start monitoring before access is verified"
    except MonitoringStateError:
        pass


async def test_scenario_d_flood_wait_during_collection_does_not_crash_and_preserves_checkpoint(session, settings):
    from telethon.errors import FloodWaitError

    channel = FakeChannel(id=202, title="Flaky", username="flaky", left=True)
    messages = [FakeMessage(id=1, message="ok")]
    fake_client = FakeClient(entities={"flaky": channel}, messages_by_entity_id={202: messages})
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@flaky")
    monitoring_service = MonitoringService(session, manager, settings)
    source = await monitoring_service.start_monitoring(source.id)
    assert source.last_message_id == 1

    # Now Telegram starts flood-waiting this source's channel.
    fake_client.probe_raises[202] = FloodWaitError(None, capture=20)
    result = await monitoring_service.run_collection_for_source(source.id)

    assert result["error"] is not None
    assert "FloodWaitError" in result["error"]
    refreshed = await source_service.get_source(source.id)
    # Checkpoint/status must remain consistent - not advanced, not crashed to a bad state.
    assert refreshed.last_message_id == 1
    assert refreshed.access_status == SourceAccessStatus.MONITORING.value


async def test_start_monitoring_rejected_when_access_status_error(session, settings):
    """A source that is already ERROR (for any reason, not necessarily a
    session problem) must never be allowed into MONITORING."""
    channel = FakeChannel(id=203, title="Erroring", username="erroring", left=True)
    fake_client = FakeClient(entities={"erroring": channel}, messages_by_entity_id={203: []})
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@erroring")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    # Force ERROR through a plain generic failure - the Telegram session
    # itself is fine (manager.session_invalid stays False).
    await source_service.sources.update(
        source, access_status=SourceAccessStatus.ERROR.value, monitoring_enabled=False, status_reason="generic RPC error"
    )
    await session.commit()
    assert manager.session_invalid is False

    monitoring_service = MonitoringService(session, manager, settings)
    from telegram_app.services.monitoring_service import MonitoringStateError

    with pytest.raises(MonitoringStateError):
        await monitoring_service.start_monitoring(source.id)


async def test_start_monitoring_rejected_when_join_request_pending(session, settings):
    from telethon.errors import ChannelPrivateError, InviteRequestSentError
    from telethon.tl.functions.channels import JoinChannelRequest

    channel = FakeChannel(id=204, title="Pending", username="pendingchan", left=True)
    fake_client = FakeClient(
        entities={"pendingchan": channel},
        probe_raises={204: ChannelPrivateError(None)},
        rpc_raises={JoinChannelRequest: InviteRequestSentError(None)},
    )
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@pendingchan")
    assert source.access_status == SourceAccessStatus.JOIN_REQUEST_REQUIRED.value
    await source_service.request_access(source.id)
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value

    monitoring_service = MonitoringService(session, manager, settings)
    from telegram_app.services.monitoring_service import MonitoringStateError

    with pytest.raises(MonitoringStateError):
        await monitoring_service.start_monitoring(source.id)


async def test_start_monitoring_rejected_when_session_invalid(session, settings):
    """Even if access_status looks fine, monitoring must not start while the
    Telegram session itself is known invalid."""
    channel = FakeChannel(id=205, title="Fine Access", username="finelooking", left=True)
    fake_client = FakeClient(entities={"finelooking": channel}, messages_by_entity_id={205: []})
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@finelooking")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")
    fake_client._authorized = False

    monitoring_service = MonitoringService(session, manager, settings)
    from telegram_app.services.monitoring_service import MonitoringStateError

    with pytest.raises(MonitoringStateError):
        await monitoring_service.start_monitoring(source.id)


async def test_auth_key_unregistered_during_collection_disables_monitoring(session, settings):
    from telethon.errors import AuthKeyUnregisteredError

    channel = FakeChannel(id=206, title="Deals", username="deals365days", broadcast=True, left=True)
    fake_client = FakeClient(entities={"deals365days": channel}, messages_by_entity_id={206: []})
    manager = StubClientManager(fake_client)

    source_service = SourceService(session, manager)
    source = await source_service.register_source("@deals365days")
    monitoring_service = MonitoringService(session, manager, settings)
    source = await monitoring_service.start_monitoring(source.id)
    assert source.access_status == SourceAccessStatus.MONITORING.value
    assert source.monitoring_enabled is True

    # Telegram now reports this account's session as unregistered/revoked
    # mid-monitoring (exactly the reported AuthKeyUnregisteredError /
    # ResolveUsernameRequest failure).
    fake_client.entity_raises["deals365days"] = AuthKeyUnregisteredError(None)

    result = await monitoring_service.run_collection_for_source(source.id)

    assert result["error"] is not None
    refreshed = await source_service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.ERROR.value
    assert refreshed.monitoring_enabled is False
    assert refreshed.access_status != SourceAccessStatus.ACCESS_DENIED.value
    assert manager.session_invalid is True

    # Must not keep hammering Telegram on subsequent cycles.
    get_entity_calls_before = sum(1 for c in fake_client.calls if c[0] == "get_entity")
    await monitoring_service.run_collection_for_source(source.id)
    get_entity_calls_after = sum(1 for c in fake_client.calls if c[0] == "get_entity")
    assert get_entity_calls_after == get_entity_calls_before
