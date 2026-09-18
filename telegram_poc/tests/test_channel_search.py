import pytest
from telethon.errors import FloodWaitError

from telegram_app.telegram.discovery.channel_search import search_channel
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeClient, FakeMessage


async def test_empty_query_rejected():
    channel = FakeChannel(id=1, username="news")
    client = FakeClient()
    with pytest.raises(TelegramSearchError) as exc_info:
        await search_channel(client, channel, "", channel_title="News", channel_username="news")
    assert exc_info.value.code == "INVALID_QUERY"


async def test_finds_matching_messages_only():
    channel = FakeChannel(id=1, username="news")
    messages = [
        FakeMessage(id=1, message="an attack was reported", chat_id=1),
        FakeMessage(id=2, message="completely unrelated", chat_id=1),
        FakeMessage(id=3, message="another attack story", chat_id=1),
    ]
    client = FakeClient(messages_by_entity_id={1: messages})

    outcome = await search_channel(client, channel, "attack", channel_title="News", channel_username="news")

    assert {i.message_id for i in outcome.items} == {"1", "3"}
    assert all(i.collection_method == "channel_search" for i in outcome.items)
    assert all(i.channel.username == "news" for i in outcome.items)


async def test_sender_filter():
    channel = FakeChannel(id=1, username="news")
    messages = [
        FakeMessage(id=1, message="attack", chat_id=1, sender_username="alice"),
        FakeMessage(id=2, message="attack", chat_id=1, sender_username="bob"),
    ]
    client = FakeClient(messages_by_entity_id={1: messages})

    outcome = await search_channel(client, channel, "attack", channel_title="News", channel_username="news", sender_username="alice")

    assert len(outcome.items) == 1
    assert outcome.items[0].message_id == "1"


async def test_media_type_filter():
    channel = FakeChannel(id=1, username="news")
    messages = [
        FakeMessage(id=1, message="attack photo", chat_id=1, media_kind="photo"),
        FakeMessage(id=2, message="attack video", chat_id=1, media_kind="video"),
    ]
    client = FakeClient(messages_by_entity_id={1: messages})

    outcome = await search_channel(client, channel, "attack", channel_title="News", channel_username="news", media_type="video")

    assert len(outcome.items) == 1
    assert outcome.items[0].media.media_type == "video"


async def test_date_range_filter():
    from datetime import datetime, timezone

    channel = FakeChannel(id=1, username="news")
    messages = [
        FakeMessage(id=1, message="old attack", chat_id=1, date=datetime(2024, 1, 1, tzinfo=timezone.utc)),
        FakeMessage(id=2, message="recent attack", chat_id=1, date=datetime(2026, 6, 1, tzinfo=timezone.utc)),
    ]
    client = FakeClient(messages_by_entity_id={1: messages})

    outcome = await search_channel(
        client, channel, "attack", channel_title="News", channel_username="news",
        from_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert len(outcome.items) == 1
    assert outcome.items[0].message_id == "2"


async def test_to_date_filter_passed_natively_to_telegram():
    """to_date maps to Telethon's offset_date, so Telegram itself excludes
    anything newer than the cutoff (rather than the app filtering it out of
    a larger live result set after the fact)."""
    from datetime import datetime, timezone

    channel = FakeChannel(id=1, username="news")
    messages = [
        FakeMessage(id=1, message="attack before cutoff", chat_id=1, date=datetime(2025, 6, 1, tzinfo=timezone.utc)),
        FakeMessage(id=2, message="attack after cutoff", chat_id=1, date=datetime(2026, 6, 1, tzinfo=timezone.utc)),
    ]
    client = FakeClient(messages_by_entity_id={1: messages})
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)

    outcome = await search_channel(client, channel, "attack", channel_title="News", channel_username="news", to_date=cutoff)

    assert len(outcome.items) == 1
    assert outcome.items[0].message_id == "1"


async def test_pagination_via_cursor():
    channel = FakeChannel(id=1, username="news")
    messages = [FakeMessage(id=i, message="attack", chat_id=1) for i in range(1, 6)]
    client = FakeClient(messages_by_entity_id={1: messages})

    first_page = await search_channel(client, channel, "attack", channel_title="News", channel_username="news", limit=2)
    assert [i.message_id for i in first_page.items] == ["5", "4"]
    assert first_page.has_more is True
    assert first_page.next_cursor is not None

    second_page = await search_channel(
        client, channel, "attack", channel_title="News", channel_username="news", limit=2, cursor=first_page.next_cursor
    )
    assert [i.message_id for i in second_page.items] == ["3", "2"]


async def test_flood_wait_propagates_as_real_exception():
    """search_channel itself never swallows FloodWaitError - it's
    search_service's job to translate it into a stable FLOOD_WAIT
    TelegramSearchError (see test_search_service.py)."""
    channel = FakeChannel(id=1, username="news")
    client = FakeClient(probe_raises={1: FloodWaitError(None, capture=12)})

    with pytest.raises(FloodWaitError):
        await search_channel(client, channel, "attack", channel_title="News", channel_username="news")


async def test_no_results_for_inaccessible_is_not_this_modules_job():
    """search_channel itself never checks access - it's given an entity and
    searches it. Access denial is search_service's responsibility (tested
    separately) - this just documents that boundary."""
    channel = FakeChannel(id=1, username="news")
    client = FakeClient(messages_by_entity_id={1: []})

    outcome = await search_channel(client, channel, "attack", channel_title="News", channel_username="news")

    assert outcome.items == []
