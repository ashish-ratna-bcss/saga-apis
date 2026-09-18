from telethon.errors import (
    AuthKeyUnregisteredError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

from telegram_app.database.models import SourceAccessStatus as S
from telegram_app.telegram import join_request_manager
from tests.fakes import FakeChannel, FakeClient, make_chat_invite, make_chat_invite_already


async def test_submit_invite_hash_joins_instantly_when_no_approval_needed():
    client = FakeClient(rpc_responses={ImportChatInviteRequest: None})

    result = await join_request_manager.submit_join_request(client, username=None, invite_hash="abc")

    assert result.status == S.JOINED


async def test_submit_invite_hash_pending_when_approval_required():
    client = FakeClient(rpc_raises={ImportChatInviteRequest: InviteRequestSentError(None)})

    result = await join_request_manager.submit_join_request(client, username=None, invite_hash="abc")

    assert result.status == S.JOIN_REQUEST_PENDING


async def test_submit_username_joins_instantly():
    client = FakeClient(rpc_responses={JoinChannelRequest: None})

    result = await join_request_manager.submit_join_request(client, username="news", invite_hash=None)

    assert result.status == S.JOINED


async def test_submit_username_pending_when_approval_required():
    client = FakeClient(rpc_raises={JoinChannelRequest: InviteRequestSentError(None)})

    result = await join_request_manager.submit_join_request(client, username="news", invite_hash=None)

    assert result.status == S.JOIN_REQUEST_PENDING


async def test_check_pending_invite_still_pending():
    client = FakeClient(rpc_responses={CheckChatInviteRequest: make_chat_invite(request_needed=True)})

    result = await join_request_manager.check_pending_status(client, username=None, invite_hash="abc")

    assert result.status == S.JOIN_REQUEST_PENDING


async def test_check_pending_invite_approved():
    chat = FakeChannel(id=7, title="Now A Member", left=False)
    client = FakeClient(rpc_responses={CheckChatInviteRequest: make_chat_invite_already(chat)})

    result = await join_request_manager.check_pending_status(client, username=None, invite_hash="abc")

    assert result.status == S.JOIN_REQUEST_APPROVED
    assert result.telegram_entity_id == "7"


async def test_check_pending_invite_expired_is_rejected():
    client = FakeClient(rpc_raises={CheckChatInviteRequest: InviteHashExpiredError(None)})

    result = await join_request_manager.check_pending_status(client, username=None, invite_hash="abc")

    assert result.status == S.JOIN_REQUEST_REJECTED


async def test_check_pending_invite_invalid_is_rejected():
    client = FakeClient(rpc_raises={CheckChatInviteRequest: InviteHashInvalidError(None)})

    result = await join_request_manager.check_pending_status(client, username=None, invite_hash="abc")

    assert result.status == S.JOIN_REQUEST_REJECTED


async def test_check_pending_username_still_pending_when_not_yet_member():
    channel = FakeChannel(id=8, title="Waiting Room", username="waitingroom", left=True)
    client = FakeClient(entities={"waitingroom": channel})

    result = await join_request_manager.check_pending_status(client, username="waitingroom", invite_hash=None)

    assert result.status == S.JOIN_REQUEST_PENDING


async def test_check_pending_username_approved_once_member():
    channel = FakeChannel(id=9, title="Welcomed In", username="welcomedin", left=False)
    client = FakeClient(entities={"welcomedin": channel})

    result = await join_request_manager.check_pending_status(client, username="welcomedin", invite_hash=None)

    assert result.status == S.JOIN_REQUEST_APPROVED


async def test_submit_join_request_auth_key_unregistered_is_session_invalid():
    client = FakeClient(rpc_raises={JoinChannelRequest: AuthKeyUnregisteredError(None)})

    result = await join_request_manager.submit_join_request(client, username="news", invite_hash=None)

    assert result.status == S.ERROR
    assert result.session_invalid is True


async def test_check_pending_username_auth_key_unregistered_is_session_invalid_not_rejected():
    client = FakeClient(entity_raises={"waitingroom": AuthKeyUnregisteredError(None)})

    result = await join_request_manager.check_pending_status(client, username="waitingroom", invite_hash=None)

    assert result.status == S.ERROR
    assert result.session_invalid is True
    assert result.status != S.JOIN_REQUEST_REJECTED


async def test_check_pending_invite_auth_key_unregistered_is_session_invalid():
    client = FakeClient(rpc_raises={CheckChatInviteRequest: AuthKeyUnregisteredError(None)})

    result = await join_request_manager.check_pending_status(client, username=None, invite_hash="abc")

    assert result.status == S.ERROR
    assert result.session_invalid is True
