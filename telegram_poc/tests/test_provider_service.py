"""Covers app/services/provider_service.py - the storage-decoupled Telegram
provider API built for an external consumer (Sockeye/Blugate) to poll.
Every method here must work without a database session or a registered
TelegramSource row; these tests never touch app/database/*."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, InviteHashExpiredError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from telegram_app.services.provider_service import ProviderService
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeClient, FakeFile, FakeMessage, StubClientManager


def _service(fake_client: FakeClient, settings) -> ProviderService:
    return ProviderService(StubClientManager(fake_client), settings)


async def test_channel_info_enriches_with_full_channel_details(settings):
    channel = FakeChannel(id=900, title="News Room", username="newsroom")
    fake_client = FakeClient(
        entities={"newsroom": channel},
        rpc_responses={GetFullChannelRequest: SimpleNamespace(full_chat=SimpleNamespace(about="Breaking news", participants_count=5000))},
    )
    service = _service(fake_client, settings)

    info = await service.channel_info(username="newsroom")

    assert info["id"] == "900"
    assert info["title"] == "News Room"
    assert info["type"] == "channel"
    assert info["description"] == "Breaking news"
    assert info["members_count"] == 5000
    assert info["is_public"] is True
    assert info["url"] == "https://t.me/newsroom"
    assert info["photo_path"] is None  # FakeChannel builds photo=None


async def test_channel_info_requires_at_least_one_identifier(settings):
    service = _service(FakeClient(), settings)
    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_info()
    assert exc_info.value.code == "INVALID_REQUEST"


async def test_channel_messages_returns_recent_history_with_cursor(settings):
    channel = FakeChannel(id=901, username="feed")
    messages = [FakeMessage(id=i, message=f"post {i}", chat_id=901) for i in range(1, 4)]
    fake_client = FakeClient(entities={"feed": channel}, messages_by_entity_id={901: messages})
    service = _service(fake_client, settings)

    page = await service.channel_messages(username="feed", limit=2)

    assert [item["id"] for item in page.items] == ["3", "2"]
    assert page.cursor is not None

    next_page = await service.channel_messages(username="feed", limit=2, cursor=page.cursor)
    assert [item["id"] for item in next_page.items] == ["1"]
    assert next_page.cursor is None


async def test_channel_messages_media_item_carries_a_fetch_path(settings):
    channel = FakeChannel(id=909, username="mediachan")
    photo_msg = FakeMessage(id=21, message="", chat_id=909, media_kind="photo", file=FakeFile(mime_type="image/jpeg", size=1234))
    fake_client = FakeClient(entities={"mediachan": channel}, messages_by_entity_id={909: [photo_msg]})
    service = _service(fake_client, settings)

    page = await service.channel_messages(username="mediachan", limit=5)

    media = page.items[0]["media"]
    assert len(media) == 1
    assert media[0]["type"] == "photo"
    assert media[0]["url_path"] == "/api/telegram/channels/909/messages/21/media"


async def test_message_by_url_parses_channel_and_message_id(settings):
    channel = FakeChannel(id=902, username="feed2")
    msg = FakeMessage(id=55, message="hello world", chat_id=902)
    fake_client = FakeClient(entities={"feed2": channel}, messages_by_entity_id={902: [msg]})
    service = _service(fake_client, settings)

    result = await service.message(url="https://t.me/feed2/55")
    assert result["id"] == "55"
    assert result["text"] == "hello world"
    assert result["channel_id"] == "902"


async def test_message_not_found_returns_none(settings):
    channel = FakeChannel(id=903, username="feed3")
    fake_client = FakeClient(entities={"feed3": channel}, messages_by_entity_id={903: []})
    service = _service(fake_client, settings)

    result = await service.message(channel_id="feed3", message_id=999)
    assert result is None


async def test_message_rejects_malformed_url(settings):
    service = _service(FakeClient(), settings)
    with pytest.raises(TelegramSearchError) as exc_info:
        await service.message(url="https://example.com/not-telegram")
    assert exc_info.value.code == "INVALID_REQUEST"


async def test_message_replies_filters_by_reply_to(settings):
    channel = FakeChannel(id=904, username="commented")
    root = FakeMessage(id=10, message="root post", chat_id=904)
    reply1 = FakeMessage(id=11, message="first reply", chat_id=904, reply_to_msg_id=10)
    reply2 = FakeMessage(id=12, message="second reply", chat_id=904, reply_to_msg_id=10)
    unrelated = FakeMessage(id=13, message="different thread", chat_id=904, reply_to_msg_id=999)
    fake_client = FakeClient(entities={"commented": channel}, messages_by_entity_id={904: [root, reply1, reply2, unrelated]})
    service = _service(fake_client, settings)

    page = await service.message_replies(channel_id="commented", message_id=10)

    assert [item["id"] for item in page.items] == ["12", "11"]


async def test_resolve_link_message_kind(settings):
    channel = FakeChannel(id=905, username="resolvechan")
    msg = FakeMessage(id=77, message="resolved", chat_id=905)
    fake_client = FakeClient(
        entities={"resolvechan": channel},
        messages_by_entity_id={905: [msg]},
        rpc_responses={GetFullChannelRequest: SimpleNamespace(full_chat=SimpleNamespace(about=None, participants_count=1))},
    )
    service = _service(fake_client, settings)

    result = await service.resolve_link("https://t.me/resolvechan/77")
    assert result["kind"] == "message"
    assert result["channel"]["username"] == "resolvechan"
    assert result["message"]["id"] == "77"


async def test_resolve_link_invite_kind_never_calls_telegram(settings):
    service = _service(FakeClient(), settings)
    result = await service.resolve_link("https://t.me/+AbCdEfGhIj")
    assert result == {"kind": "invite", "invite": "AbCdEfGhIj"}


async def test_check_access_maps_public_status(settings):
    channel = FakeChannel(id=906, username="publicaccess", left=True)
    fake_client = FakeClient(entities={"publicaccess": channel})
    service = _service(fake_client, settings)

    result = await service.check_access(username="publicaccess")
    assert result["state"] == "public"
    assert result["accessible"] is True


async def test_check_access_maps_private_no_username_to_denied(settings):
    # access_manager only reports ACCESS_DENIED (vs. JOIN_REQUEST_REQUIRED)
    # for a private entity with no known username - see access_manager.py's
    # ChannelPrivateError handling.
    channel = FakeChannel(id=907, username=None, left=True)
    fake_client = FakeClient(entities={907: channel}, probe_raises={907: ChannelPrivateError(None)})
    service = _service(fake_client, settings)

    result = await service.check_access(channel_id="907")
    assert result["state"] == "denied"
    assert result["accessible"] is False


async def test_check_access_maps_private_with_username_to_invite_required(settings):
    channel = FakeChannel(id=908, username="joinme", left=True)
    fake_client = FakeClient(entities={"joinme": channel}, probe_raises={908: ChannelPrivateError(None)})
    service = _service(fake_client, settings)

    result = await service.check_access(username="joinme")
    assert result["state"] == "invite_required"
    assert result["accessible"] is False


async def test_join_invite_rejects_non_invite_identifier(settings):
    service = _service(FakeClient(), settings)
    with pytest.raises(TelegramSearchError) as exc_info:
        await service.join_invite("@notaninvite")
    assert exc_info.value.code == "INVALID_REQUEST"


async def test_join_invite_translates_expired_invite(settings):
    fake_client = FakeClient(rpc_raises={ImportChatInviteRequest: InviteHashExpiredError(None)})
    service = _service(fake_client, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.join_invite("https://t.me/+expiredhash")
    assert exc_info.value.http_status == 410


# -- get_message_media --------------------------------------------------- #

async def test_get_message_media_downloads_and_caches_to_disk(settings):
    channel = FakeChannel(id=910, username="picchan")
    msg = FakeMessage(id=30, message="", chat_id=910, media_kind="photo", file=FakeFile(mime_type="image/jpeg", size=1234))
    fake_client = FakeClient(
        entities={"picchan": channel}, messages_by_entity_id={910: [msg]}, message_media={30: b"raw-jpeg-bytes"},
    )
    service = _service(fake_client, settings)

    result = await service.get_message_media(channel_id="picchan", message_id=30)
    assert result == (b"raw-jpeg-bytes", "image/jpeg")
    assert fake_client.calls.count(("download_media", msg, bytes)) == 1

    cached = list(Path(settings.message_media_cache_path).glob("910_30.*"))
    assert len(cached) == 1

    # Second call must be served from disk - no second Telegram download.
    result_again = await service.get_message_media(channel_id="picchan", message_id=30)
    assert result_again == (b"raw-jpeg-bytes", "image/jpeg")
    assert fake_client.calls.count(("download_media", msg, bytes)) == 1


async def test_get_message_media_returns_none_when_message_has_no_media(settings):
    channel = FakeChannel(id=911, username="textchan")
    msg = FakeMessage(id=31, message="just text", chat_id=911)
    fake_client = FakeClient(entities={"textchan": channel}, messages_by_entity_id={911: [msg]})
    service = _service(fake_client, settings)

    result = await service.get_message_media(channel_id="textchan", message_id=31)
    assert result is None


async def test_get_message_media_returns_none_when_message_not_found(settings):
    channel = FakeChannel(id=912, username="emptychan")
    fake_client = FakeClient(entities={"emptychan": channel}, messages_by_entity_id={912: []})
    service = _service(fake_client, settings)

    result = await service.get_message_media(channel_id="emptychan", message_id=999)
    assert result is None


async def test_get_message_media_rejects_files_over_the_configured_limit(settings):
    channel = FakeChannel(id=913, username="hugechan")
    msg = FakeMessage(
        id=32, message="", chat_id=913, media_kind="video",
        file=FakeFile(mime_type="video/mp4", size=settings.message_media_max_size_bytes + 1),
    )
    fake_client = FakeClient(entities={"hugechan": channel}, messages_by_entity_id={913: [msg]})
    service = _service(fake_client, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.get_message_media(channel_id="hugechan", message_id=32)
    assert exc_info.value.code == "MEDIA_TOO_LARGE"
    assert not any(c[0] == "download_media" for c in fake_client.calls)
