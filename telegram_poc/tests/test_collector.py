from telethon.errors import FloodWaitError

from telegram_app.telegram import collector
from tests.fakes import FakeChannel, FakeClient, FakeFile, FakeMessage, FakeSender


async def test_normalize_message_captures_core_fields():
    sender = FakeSender(id=1, username="alice", first_name="Alice", last_name="A")
    message = FakeMessage(id=10, sender_id=1, message="hello world", views=5, forwards=2, sender=sender)

    normalized = await collector.normalize_message(message, source_username="news")

    assert normalized.telegram_message_id == 10
    assert normalized.text == "hello world"
    assert normalized.sender_username == "alice"
    assert normalized.sender_display_name == "Alice A"
    assert normalized.source_url == "https://t.me/news/10"
    assert normalized.raw_data_hash
    assert normalized.media == []


async def test_normalize_message_classifies_media_and_metadata():
    message = FakeMessage(id=11, media_kind="document", file=FakeFile(name="report.pdf", mime_type="application/pdf", size=1024))

    normalized = await collector.normalize_message(message, source_username=None)

    assert normalized.media_type == "document"
    assert len(normalized.media) == 1
    assert normalized.media[0].filename == "report.pdf"
    assert normalized.media[0].file_size == 1024
    assert normalized.source_url is None  # no username - no permalink possible


async def test_collect_new_messages_only_returns_messages_after_checkpoint():
    channel = FakeChannel(id=1, username="news")
    messages = [FakeMessage(id=i, message=f"msg {i}") for i in range(1, 6)]
    client = FakeClient(messages_by_entity_id={1: messages})

    outcome = await collector.collect_new_messages(client, channel, min_id=3, limit=100, source_username="news")

    assert outcome.error is None
    assert [m.telegram_message_id for m in outcome.messages] == [4, 5]


async def test_collect_new_messages_respects_limit():
    channel = FakeChannel(id=1, username="news")
    messages = [FakeMessage(id=i) for i in range(1, 11)]
    client = FakeClient(messages_by_entity_id={1: messages})

    outcome = await collector.collect_new_messages(client, channel, min_id=0, limit=3, source_username="news")

    assert [m.telegram_message_id for m in outcome.messages] == [1, 2, 3]


async def test_collect_new_messages_handles_flood_wait_without_raising():
    channel = FakeChannel(id=1, username="news")
    client = FakeClient(messages_by_entity_id={1: []}, probe_raises={1: FloodWaitError(None, capture=30)})

    outcome = await collector.collect_new_messages(client, channel, min_id=0, limit=100, source_username="news")

    assert outcome.error is not None
    assert "FloodWaitError" in outcome.error
    assert outcome.flood_wait_seconds == 30
    assert outcome.messages == []
