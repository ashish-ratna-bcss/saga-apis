from telethon.errors import AuthKeyUnregisteredError, ChannelPrivateError, UsernameNotOccupiedError
from telethon.tl.functions.messages import CheckChatInviteRequest

from telegram_app.database.models import SourceAccessStatus as S
from telegram_app.database.models import SourceType
from telegram_app.telegram import access_manager
from telegram_app.telegram.discovery import normalize_identifier
from tests.fakes import FakeChannel, FakeClient, FakeUser, make_chat_invite, make_chat_invite_already


async def test_bot_entity_is_classified_bot_not_channel():
    """The exact bug this fixes: @Pspkmovies_bot (and any bot) must never be
    classified as a channel/user - it's ACCESSIBLE (a bot profile can always
    be opened) but its source_type must say BOT, so downstream search logic
    can refuse to treat it as a message-search source."""
    bot = FakeUser(id=10, username="pspkmovies_bot", first_name="PSPK Movies", bot=True)
    client = FakeClient(entities={"pspkmovies_bot": bot})

    result = await access_manager.check_access(client, normalize_identifier("@pspkmovies_bot"))

    assert result.status == S.ACCESSIBLE
    assert result.source_type == SourceType.BOT


async def test_plain_user_is_still_classified_user():
    user = FakeUser(id=11, username="realuser", first_name="Real", bot=False)
    client = FakeClient(entities={"realuser": user})

    result = await access_manager.check_access(client, normalize_identifier("@realuser"))

    assert result.status == S.ACCESSIBLE
    assert result.source_type == SourceType.USER


async def test_public_channel_readable_without_membership_is_public_accessible():
    channel = FakeChannel(id=1, title="News", username="news", broadcast=True, left=True)
    client = FakeClient(entities={"news": channel}, messages_by_entity_id={1: []})

    result = await access_manager.check_access(client, normalize_identifier("@news"))

    assert result.status == S.PUBLIC_ACCESSIBLE
    assert result.telegram_entity_id == "1"


async def test_already_member_channel_is_accessible():
    channel = FakeChannel(id=2, title="MyGroup", username="mygroup", left=False)
    client = FakeClient(entities={"mygroup": channel}, messages_by_entity_id={2: []})

    result = await access_manager.check_access(client, normalize_identifier("@mygroup"))

    assert result.status == S.ACCESSIBLE


async def test_username_not_found_is_not_found():
    client = FakeClient(entity_raises={"ghost": UsernameNotOccupiedError(None)})

    result = await access_manager.check_access(client, normalize_identifier("@ghost"))

    assert result.status == S.NOT_FOUND


async def test_private_public_username_channel_requires_join():
    channel = FakeChannel(id=3, title="Private Club", username="privateclub", left=True)
    client = FakeClient(entities={"privateclub": channel}, probe_raises={3: ChannelPrivateError(None)})

    result = await access_manager.check_access(client, normalize_identifier("@privateclub"))

    assert result.status == S.JOIN_REQUEST_REQUIRED


async def test_private_channel_with_no_username_is_access_denied():
    channel = FakeChannel(id=4, title="Secret", username=None, left=True)
    client = FakeClient(entities={"4": channel}, probe_raises={4: ChannelPrivateError(None)})

    result = await access_manager.check_access(client, normalize_identifier("4"))

    assert result.status == S.ACCESS_DENIED


async def test_invite_hash_already_member():
    chat = FakeChannel(id=5, title="Joined Already", username=None, left=False)
    client = FakeClient(rpc_responses={CheckChatInviteRequest: make_chat_invite_already(chat)})

    result = await access_manager.check_access(client, normalize_identifier("https://t.me/+abc123"))

    assert result.status == S.ACCESSIBLE
    assert result.telegram_entity_id == "5"


async def test_invite_hash_requires_approval():
    invite = make_chat_invite(title="Needs Approval", request_needed=True)
    client = FakeClient(rpc_responses={CheckChatInviteRequest: invite})

    result = await access_manager.check_access(client, normalize_identifier("https://t.me/+needsapproval"))

    assert result.status == S.JOIN_REQUEST_REQUIRED
    assert result.invite_hash == "needsapproval"


async def test_auth_key_unregistered_during_resolve_is_session_invalid_not_denied_or_not_found():
    """Reproduces the reported bug: ResolveUsernameRequest (via get_entity)
    raising AuthKeyUnregisteredError must never be classified as
    NOT_FOUND/ACCESS_DENIED - it means the session itself needs
    re-authentication."""
    client = FakeClient(entity_raises={"deals365days": AuthKeyUnregisteredError(None)})

    result = await access_manager.check_access(client, normalize_identifier("@deals365days"))

    assert result.status == S.ERROR
    assert result.session_invalid is True
    assert result.status != S.ACCESS_DENIED
    assert result.status != S.NOT_FOUND


async def test_auth_key_unregistered_during_membership_probe_is_session_invalid():
    channel = FakeChannel(id=6, title="Some Channel", username="somechan", left=True)
    client = FakeClient(entities={"somechan": channel}, probe_raises={6: AuthKeyUnregisteredError(None)})

    result = await access_manager.check_access(client, normalize_identifier("@somechan"))

    assert result.status == S.ERROR
    assert result.session_invalid is True
    assert result.status != S.JOIN_REQUEST_REQUIRED
    assert result.status != S.ACCESS_DENIED


async def test_auth_key_unregistered_on_invite_hash_is_session_invalid():
    client = FakeClient(rpc_raises={CheckChatInviteRequest: AuthKeyUnregisteredError(None)})

    result = await access_manager.check_access(client, normalize_identifier("https://t.me/+someinvite"))

    assert result.status == S.ERROR
    assert result.session_invalid is True
