"""Covers Scenario A (public source) and the registration/join-request half
of Scenario B (private source requiring approval) from the acceptance
criteria, at the service layer."""
import pytest
from telethon.errors import AuthKeyUnregisteredError, ChannelPrivateError, InviteRequestSentError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest

from telegram_app.database.models import AccessRequestStatus, SourceAccessStatus, TelegramSource
from telegram_app.services.source_service import (
    InvalidAccessRequestStateError,
    SourceAlreadyExistsError,
    SourceService,
)
from tests.fakes import (
    FakeChannel,
    FakeClient,
    StubClientManager as _StubClientManager,
    make_chat_invite,
    make_chat_invite_already,
)


async def test_register_public_source_becomes_public_accessible(session):
    channel = FakeChannel(id=100, title="Public News", username="publicnews", broadcast=True, left=True)
    fake_client = FakeClient(entities={"publicnews": channel}, messages_by_entity_id={100: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@publicnews")

    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value
    assert source.telegram_entity_id == "100"
    assert source.username == "publicnews"


async def test_register_duplicate_source_rejected(session):
    channel = FakeChannel(id=101, title="Dup", username="dupchan", left=True)
    fake_client = FakeClient(entities={"dupchan": channel}, messages_by_entity_id={101: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    await service.register_source("@dupchan")
    with pytest.raises(SourceAlreadyExistsError):
        await service.register_source("@dupchan")


async def test_register_private_source_reaches_join_request_required(session):
    channel = FakeChannel(id=102, title="Private Club", username="privclub", left=True)
    fake_client = FakeClient(
        entities={"privclub": channel},
        probe_raises={102: ChannelPrivateError(None)},
    )
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@privclub")

    assert source.access_status == SourceAccessStatus.JOIN_REQUEST_REQUIRED.value


async def test_request_access_does_not_submit_twice(session):
    channel = FakeChannel(id=103, title="Needs Approval", username="needsapproval", left=True)
    fake_client = FakeClient(
        entities={"needsapproval": channel},
        probe_raises={103: ChannelPrivateError(None)},
        rpc_raises={JoinChannelRequest: InviteRequestSentError(None)},
    )
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@needsapproval")
    assert source.access_status == SourceAccessStatus.JOIN_REQUEST_REQUIRED.value

    first_request = await service.request_access(source.id)
    assert first_request.status == AccessRequestStatus.PENDING.value

    join_rpc_calls_before = sum(1 for c in fake_client.calls if c[0] == "rpc")
    second_request = await service.request_access(source.id)
    join_rpc_calls_after = sum(1 for c in fake_client.calls if c[0] == "rpc")

    assert second_request.id == first_request.id, "must return the existing pending request, not create a new one"
    assert join_rpc_calls_after == join_rpc_calls_before, "must not submit a second join request while one is pending"

    refreshed = await service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value


async def test_request_access_rejected_from_wrong_state(session):
    channel = FakeChannel(id=104, title="Already Public", username="alreadypublic", left=True)
    fake_client = FakeClient(entities={"alreadypublic": channel}, messages_by_entity_id={104: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@alreadypublic")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    with pytest.raises(InvalidAccessRequestStateError):
        await service.request_access(source.id)


async def test_auth_key_unregistered_on_check_access_disables_monitoring_and_marks_session_invalid(session):
    """Reproduces the reported bug end-to-end: a source that was already
    PUBLIC_ACCESSIBLE (monitoring_enabled could be True) must never end up
    ERROR + monitoring_enabled=True after an AuthKeyUnregisteredError, and
    must never be misclassified as ACCESS_DENIED/private."""
    channel = FakeChannel(id=105, title="Deals", username="deals365days", broadcast=True, left=True)
    fake_client = FakeClient(entities={"deals365days": channel}, messages_by_entity_id={105: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@deals365days")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    # Simulate the exact reported failure mode: the pre-flight authorization
    # check still looks fine (is_user_authorized() can return a stale cached
    # True - see TelegramClientManager docstring), but the account's auth key
    # has actually been unregistered/revoked, so the real RPC
    # (ResolveUsernameRequest, here get_entity) fails.
    fake_client.entity_raises["deals365days"] = AuthKeyUnregisteredError(None)

    result = await service.check_access(source.id)

    assert result.access_status == SourceAccessStatus.ERROR.value
    assert result.monitoring_enabled is False
    assert result.access_status != SourceAccessStatus.ACCESS_DENIED.value
    assert manager.session_invalid is True


async def test_invalid_session_does_not_hammer_telegram_on_repeated_check_access(session):
    channel = FakeChannel(id=106, title="News", username="hammertest", broadcast=True, left=True)
    fake_client = FakeClient(entities={"hammertest": channel}, messages_by_entity_id={106: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)
    source = await service.register_source("@hammertest")

    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")
    fake_client._authorized = False

    calls_before = len(fake_client.calls)
    await service.check_access(source.id)
    await service.check_access(source.id)
    calls_after = len(fake_client.calls)

    # The pre-flight verify_authorized() short-circuit means neither call
    # ever reached get_entity/ResolveUsernameRequest.
    assert calls_after == calls_before
    refreshed = await service.get_source(source.id)
    assert refreshed.access_status == SourceAccessStatus.ERROR.value
    assert refreshed.monitoring_enabled is False


async def test_reauthentication_restores_operations_after_session_invalid(session):
    channel = FakeChannel(id=107, title="Recoverable", username="recoverable", broadcast=True, left=True)
    fake_client = FakeClient(entities={"recoverable": channel}, messages_by_entity_id={107: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@recoverable")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    manager.mark_session_invalid("AuthKeyUnregisteredError: The key is not registered in the system")
    fake_client._authorized = False

    blocked = await service.check_access(source.id)
    assert blocked.access_status == SourceAccessStatus.ERROR.value
    assert blocked.monitoring_enabled is False

    # Re-authentication succeeds (mirrors authentication.verify_password's
    # manager.clear_session_invalid() call on success).
    fake_client._authorized = True
    manager.clear_session_invalid()

    restored = await service.check_access(source.id)
    assert restored.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value


# -- probe status vs. operational status (separated fields) ------------------- #


async def test_check_access_on_monitoring_source_does_not_downgrade_but_records_probe(session):
    """The exact scenario from the spec: MONITORING + a live probe reporting
    PUBLIC_ACCESSIBLE must never downgrade the operational status - the
    state machine already blocks that transition - but the probe result
    itself must still be visible as a separate, independently recorded fact."""
    channel = FakeChannel(id=200, title="Watched", username="watchedchan", broadcast=True, left=True)
    fake_client = FakeClient(entities={"watchedchan": channel}, messages_by_entity_id={200: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = TelegramSource(
        identifier="@watchedchan", telegram_entity_id="200", username="watchedchan",
        access_status=SourceAccessStatus.MONITORING.value, monitoring_enabled=True,
    )
    session.add(source)
    await session.flush()
    await session.commit()

    result = await service.check_access(source.id)

    assert result.access_status == SourceAccessStatus.MONITORING.value, "operational status must not be downgraded by a probe"
    assert result.monitoring_enabled is True
    assert result.last_probe_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value, "the probe result must still be recorded"
    assert result.last_probe_at is not None


async def test_check_access_on_accessible_source_is_idempotent(session):
    """Re-checking an already-ACCESSIBLE source must be a safe no-op, not an
    error and not a duplicate join attempt."""
    channel = FakeChannel(id=202, title="Member", username="alreadymember", left=False)
    fake_client = FakeClient(entities={"alreadymember": channel}, messages_by_entity_id={202: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@alreadymember")
    assert source.access_status == SourceAccessStatus.ACCESSIBLE.value

    calls_before = len(fake_client.calls)
    result = await service.check_access(source.id)
    calls_after = len(fake_client.calls)

    assert result.access_status == SourceAccessStatus.ACCESSIBLE.value
    assert result.last_probe_status == SourceAccessStatus.ACCESSIBLE.value
    assert calls_after > calls_before, "a re-check still probes Telegram"
    join_calls = [c for c in fake_client.calls if c[0] == "rpc"]
    assert join_calls == [], "an already-accessible source must never trigger a join attempt"


async def test_public_source_registration_never_creates_access_request(session):
    channel = FakeChannel(id=201, title="NoApproval", username="noapproval", broadcast=True, left=True)
    fake_client = FakeClient(entities={"noapproval": channel}, messages_by_entity_id={201: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    source = await service.register_source("@noapproval")
    assert source.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value

    pending = await service.access_requests.get_active_for_source(source.id)
    assert pending is None, "a public source must never get a join/approval request"


# -- linked-channel discovery (read-only) -------------------------------------- #


async def test_discover_linked_channels_extracts_and_resolves_candidate_without_registering(session):
    channel = FakeChannel(id=300, title="Linked", username="linkedchan", broadcast=True, left=True)
    fake_client = FakeClient(entities={"linkedchan": channel}, messages_by_entity_id={300: []})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    text = "found this: https://t.me/linkedchan also see https://example.com/unrelated"
    candidates = await service.discover_linked_channels(text)

    assert len(candidates) == 1
    assert candidates[0].identifier == "@linkedchan"
    assert candidates[0].access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value
    assert candidates[0].already_registered_source_id is None

    all_sources = await service.list_sources()
    assert all_sources == [], "discovery must never register a source on its own"


async def test_discover_linked_channels_ignores_non_telegram_links(session):
    manager = _StubClientManager(FakeClient())
    service = SourceService(session, manager)

    text = "nothing telegram here: https://example.com https://bit.ly/xyz"
    candidates = await service.discover_linked_channels(text)

    assert candidates == []


# -- explicit invite-link join -------------------------------------------------- #


async def test_join_by_invite_rejects_non_invite_identifier(session):
    manager = _StubClientManager(FakeClient())
    service = SourceService(session, manager)

    with pytest.raises(ValueError):
        await service.join_by_invite("@somechannel")


async def test_join_by_invite_immediate_success(session):
    from telethon.tl.functions.messages import ImportChatInviteRequest

    chat = FakeChannel(id=400, title="Invited", username=None, left=True)
    fake_client = FakeClient(
        rpc_responses={
            CheckChatInviteRequest: make_chat_invite(title="Invited", request_needed=False),
            ImportChatInviteRequest: chat,
        },
    )
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    outcome = await service.join_by_invite("https://t.me/+joinme")

    assert outcome.status == "joined"
    assert outcome.source.access_status == SourceAccessStatus.JOINED.value


async def test_join_by_invite_requires_approval_then_pending(session):
    from telethon.errors import InviteRequestSentError
    from telethon.tl.functions.messages import ImportChatInviteRequest

    fake_client = FakeClient(
        rpc_responses={CheckChatInviteRequest: make_chat_invite(title="Needs Approval", request_needed=True)},
        rpc_raises={ImportChatInviteRequest: InviteRequestSentError(None)},
    )
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    outcome = await service.join_by_invite("https://t.me/+needsapproval")

    assert outcome.status == "pending_approval"
    assert outcome.source.access_status == SourceAccessStatus.JOIN_REQUEST_PENDING.value


async def test_join_by_invite_already_pending_does_not_resubmit(session):
    from telethon.errors import InviteRequestSentError
    from telethon.tl.functions.messages import ImportChatInviteRequest

    fake_client = FakeClient(
        rpc_responses={CheckChatInviteRequest: make_chat_invite(title="Needs Approval", request_needed=True)},
        rpc_raises={ImportChatInviteRequest: InviteRequestSentError(None)},
    )
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    first = await service.join_by_invite("https://t.me/+repeatinvite")
    assert first.status == "pending_approval"

    import_calls_before = sum(1 for c in fake_client.calls if c[0] == "rpc" and c[1].__class__.__name__ == "ImportChatInviteRequest")
    second = await service.join_by_invite("https://t.me/+repeatinvite")
    import_calls_after = sum(1 for c in fake_client.calls if c[0] == "rpc" and c[1].__class__.__name__ == "ImportChatInviteRequest")

    assert second.status == "already_pending"
    assert import_calls_after == import_calls_before, "must never submit a second join attempt for a still-pending invite"


async def test_join_by_invite_invalid_invite_reports_failure(session):
    from telethon.errors import InviteHashInvalidError

    fake_client = FakeClient(rpc_raises={CheckChatInviteRequest: InviteHashInvalidError(None)})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    outcome = await service.join_by_invite("https://t.me/+badinvite")

    assert outcome.status == "failed"


async def test_join_by_invite_already_member_skips_join_rpc(session):
    from telethon.tl.functions.messages import ImportChatInviteRequest

    chat = FakeChannel(id=401, title="Already In", username=None, left=False)
    fake_client = FakeClient(rpc_responses={CheckChatInviteRequest: make_chat_invite_already(chat)})
    manager = _StubClientManager(fake_client)
    service = SourceService(session, manager)

    outcome = await service.join_by_invite("https://t.me/+alreadyin")

    assert outcome.status == "already_member"
    import_calls = [c for c in fake_client.calls if c[0] == "rpc" and c[1].__class__.__name__ == "ImportChatInviteRequest"]
    assert import_calls == [], "an already-accessible invite must never trigger a join RPC"
