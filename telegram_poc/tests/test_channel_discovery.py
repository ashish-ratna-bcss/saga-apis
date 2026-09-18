from types import SimpleNamespace

import pytest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.functions.messages import GetFullChatRequest

from telegram_app.telegram.discovery.channel_discovery import discover_channels
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeChat, FakeClient, FakeContactsFound


async def test_empty_query_rejected():
    client = FakeClient()
    with pytest.raises(TelegramSearchError) as exc_info:
        await discover_channels(client, "")
    assert exc_info.value.code == "INVALID_QUERY"


async def test_discovers_channels_and_groups():
    channel = FakeChannel(id=1, title="News Channel", username="newschan", broadcast=True, left=True)
    group = FakeChat(id=2, title="Discussion Group")
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel, group])})

    outcome = await discover_channels(client, "news")

    assert len(outcome.items) == 2
    types = {i.type for i in outcome.items}
    assert types == {"channel", "group"}
    assert outcome.has_more is False


async def test_never_fabricates_missing_fields():
    channel = FakeChannel(id=1, title="Bare Channel", username=None, broadcast=True, left=True)
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel])})

    outcome = await discover_channels(client, "bare")

    result = outcome.items[0]
    assert result.username is None
    assert result.source_url is None
    assert result.public is False
    assert result.member_count is None  # FakeChannel never sets participants_count - must stay None, not guessed


async def test_deduplicates_repeated_results():
    channel = FakeChannel(id=1, title="Dup", username="dup")
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel, channel])})

    outcome = await discover_channels(client, "dup")

    assert len(outcome.items) == 1


async def test_already_member_status_hint():
    channel = FakeChannel(id=1, title="Joined", username="joined", left=False)
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel])})

    outcome = await discover_channels(client, "joined")

    assert outcome.items[0].access_status == "ALREADY_MEMBER"


async def test_public_username_gives_public_likely_hint():
    channel = FakeChannel(id=1, title="Public", username="pub", left=True)
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel])})

    outcome = await discover_channels(client, "pub")

    assert outcome.items[0].access_status == "PUBLIC_LIKELY"
    assert outcome.items[0].source_url == "https://t.me/pub"


async def test_description_omitted_by_default_and_issues_no_extra_rpc():
    channel = FakeChannel(id=1, title="Quiet", username="quiet")
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel])})

    outcome = await discover_channels(client, "quiet")

    assert outcome.items[0].description is None
    assert client.call_count(GetFullChannelRequest) == 0


async def test_include_descriptions_fetches_channel_about_per_result():
    channel = FakeChannel(id=1, title="Chatty", username="chatty")
    client = FakeClient(rpc_responses={
        ContactsSearchRequest: FakeContactsFound(chats=[channel]),
        GetFullChannelRequest: SimpleNamespace(full_chat=SimpleNamespace(about="Chatty channel about", participants_count=10)),
    })

    outcome = await discover_channels(client, "chatty", include_descriptions=True)

    assert outcome.items[0].description == "Chatty channel about"


async def test_include_descriptions_fetches_group_about_per_result():
    group = FakeChat(id=2, title="Group Chat")
    client = FakeClient(rpc_responses={
        ContactsSearchRequest: FakeContactsFound(chats=[group]),
        GetFullChatRequest: SimpleNamespace(full_chat=SimpleNamespace(about="Group about", participants=None)),
    })

    outcome = await discover_channels(client, "group", include_descriptions=True)

    assert outcome.items[0].description == "Group about"


async def test_include_descriptions_best_effort_on_rpc_failure():
    from telethon.errors import ChatAdminRequiredError

    channel = FakeChannel(id=1, title="Locked", username="locked")
    client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel])}, rpc_raises={GetFullChannelRequest: ChatAdminRequiredError(None)})

    outcome = await discover_channels(client, "locked", include_descriptions=True)

    assert outcome.items[0].description is None
    assert len(outcome.items) == 1  # the failed enrichment must not drop the result itself
