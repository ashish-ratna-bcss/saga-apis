"""Covers search_service's access-verification boundary: a channel search
must never silently return empty results for an inaccessible source, and
must never even attempt a Telegram call when a registered source's stored
status already says it can't be searched."""
from pathlib import Path

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest

from telegram_app.database.models import SourceAccessStatus, TelegramSource
from telegram_app.database.repositories.audit_repository import AuditRepository
from telegram_app.services.search_service import SearchService
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeClient, FakeContactsFound, FakeMessage, FakeSearchPostsFlood, FakeSearchResult, StubClientManager


async def _make_source(session, *, status, entity_id=700, username="searchable"):
    source = TelegramSource(
        identifier=f"@{username}", telegram_entity_id=str(entity_id), username=username, access_status=status.value,
    )
    session.add(source)
    await session.flush()
    await session.commit()
    return source


async def test_channel_search_by_source_rejects_join_required_without_calling_telegram(session, settings):
    source = await _make_source(session, status=SourceAccessStatus.JOIN_REQUEST_REQUIRED)
    fake_client = FakeClient()
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_source(source.id, "attack")

    assert exc_info.value.code == "JOIN_REQUIRED"
    assert fake_client.calls == [], "must not contact Telegram for a known-inaccessible source"


async def test_channel_search_by_source_succeeds_when_monitoring(session, settings):
    source = await _make_source(session, status=SourceAccessStatus.MONITORING)
    channel = FakeChannel(id=700, username="searchable")
    messages = [FakeMessage(id=1, message="an attack happened", chat_id=700)]
    fake_client = FakeClient(entities={"searchable": channel}, messages_by_entity_id={700: messages})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    outcome = await service.channel_search_by_source(source.id, "attack")

    assert len(outcome.items) == 1


async def test_channel_search_by_source_missing_source_404(session, settings):
    manager = StubClientManager(FakeClient())
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_source(9999, "attack")
    assert exc_info.value.http_status == 404


async def test_channel_search_by_identifier_does_live_probe_and_denies_private(session, settings):
    channel = FakeChannel(id=701, username="privatechan", left=True)
    fake_client = FakeClient(entities={"privatechan": channel}, probe_raises={701: ChannelPrivateError(None)})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_identifier("@privatechan", "attack")

    assert exc_info.value.code in ("JOIN_REQUIRED", "ACCESS_DENIED")


async def test_channel_search_by_identifier_succeeds_for_public_channel(session, settings):
    channel = FakeChannel(id=702, username="publicchan", left=True)
    messages = [FakeMessage(id=1, message="an attack happened", chat_id=702)]
    fake_client = FakeClient(entities={"publicchan": channel}, messages_by_entity_id={702: messages})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    outcome = await service.channel_search_by_identifier("@publicchan", "attack")

    assert len(outcome.items) == 1


async def test_channel_search_by_source_rejects_join_pending_without_calling_telegram(session, settings):
    source = await _make_source(session, status=SourceAccessStatus.JOIN_REQUEST_PENDING, entity_id=703, username="pendingsearch")
    fake_client = FakeClient()
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_source(source.id, "attack")

    assert exc_info.value.code == "JOIN_PENDING"
    assert exc_info.value.http_status == 403
    assert fake_client.calls == [], "must not contact Telegram for a source still awaiting approval"


async def test_channel_search_by_source_flood_wait_translates_to_stable_error(session, settings):
    source = await _make_source(session, status=SourceAccessStatus.MONITORING, entity_id=704, username="floodysearch")
    channel = FakeChannel(id=704, username="floodysearch")
    fake_client = FakeClient(entities={"floodysearch": channel}, probe_raises={704: FloodWaitError(None, capture=9)})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_source(source.id, "attack")

    assert exc_info.value.code == "FLOOD_WAIT"
    assert exc_info.value.retryable is True
    assert exc_info.value.retry_after_seconds == 9


async def test_get_channel_photo_downloads_and_caches_to_disk(session, settings):
    channel = FakeChannel(id=800, username="photochan")
    fake_client = FakeClient(entities={"photochan": channel}, profile_photos={800: b"real-jpeg-bytes"})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    photo = await service.get_channel_photo("@photochan")
    assert photo == b"real-jpeg-bytes"
    assert fake_client.calls.count(("download_profile_photo", channel, bytes, True)) == 1

    cached_file = Path(settings.avatar_cache_path) / "800.jpg"
    assert cached_file.read_bytes() == b"real-jpeg-bytes"

    # A second call must be served from disk - no second Telegram RPC.
    photo_again = await service.get_channel_photo("@photochan")
    assert photo_again == b"real-jpeg-bytes"
    assert fake_client.calls.count(("download_profile_photo", channel, bytes, True)) == 1


async def test_get_channel_photo_returns_none_without_caching_when_entity_has_no_photo(session, settings):
    channel = FakeChannel(id=801, username="nophotochan")
    fake_client = FakeClient(entities={"nophotochan": channel}, profile_photos={801: None})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    photo = await service.get_channel_photo("@nophotochan")
    assert photo is None
    assert not (Path(settings.avatar_cache_path) / "801.jpg").exists()


async def test_get_channel_photo_flood_wait_translates_to_stable_error(session, settings):
    channel = FakeChannel(id=802, username="floodyphoto")
    fake_client = FakeClient(entities={"floodyphoto": channel}, profile_photo_raises={802: FloodWaitError(None, capture=7)})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.get_channel_photo("@floodyphoto")

    assert exc_info.value.code == "AVATAR_DOWNLOAD_FAILED"
    assert exc_info.value.retryable is True


async def test_global_search_audits_success(session, settings):
    chat = FakeChannel(id=1, title="News", username="news")
    result = FakeSearchResult(messages=[FakeMessage(id=1, message="attack reported", chat_id=1)], chats=[chat])
    fake_client = FakeClient(
        rpc_responses={
            CheckSearchPostsFloodRequest: FakeSearchPostsFlood(remains=5, total_daily=5, stars_amount=0),
            SearchPostsRequest: result,
        }
    )
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    outcome = await service.global_search("attack")
    assert len(outcome.items) == 1

    audit_repo = AuditRepository(session)
    entries = await audit_repo.list_recent()
    assert any(e.event_type == "SEARCH_GLOBAL" and e.success for e in entries)


async def test_global_search_not_authenticated_raises_stable_error(session, settings):
    manager = StubClientManager(FakeClient(), configured=False)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.global_search("attack")
    assert exc_info.value.http_status == 401


async def test_search_selected_sources_only_searches_searchable_ones(session, settings):
    """Section 23's exact scenario: Channel A=MONITORING, B=ACCESSIBLE,
    C=JOIN_REQUEST_PENDING (approval pending), D=ACCESS_DENIED (inaccessible).
    Searching 'attack' must reach Telegram for A+B only; C and D must be
    skipped based on their stored status alone, never contacted, and no
    result gets written to SQL."""
    source_a = await _make_source(session, status=SourceAccessStatus.MONITORING, entity_id=900, username="channela")
    source_b = await _make_source(session, status=SourceAccessStatus.ACCESSIBLE, entity_id=901, username="channelb")
    source_c = await _make_source(session, status=SourceAccessStatus.JOIN_REQUEST_PENDING, entity_id=902, username="channelc")
    source_d = await _make_source(session, status=SourceAccessStatus.ACCESS_DENIED, entity_id=903, username="channeld")

    channel_a = FakeChannel(id=900, username="channela")
    channel_b = FakeChannel(id=901, username="channelb")
    fake_client = FakeClient(
        entities={"channela": channel_a, "channelb": channel_b},
        messages_by_entity_id={
            900: [FakeMessage(id=1, message="an attack happened here", chat_id=900)],
            901: [FakeMessage(id=1, message="another attack reported", chat_id=901)],
        },
    )
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    outcome = await service.search_selected_sources([source_a.id, source_b.id, source_c.id, source_d.id], "attack")

    assert sorted(outcome.searched_source_ids) == sorted([source_a.id, source_b.id])
    assert len(outcome.items) == 2

    reasons = {s.source_id: s.reason for s in outcome.skipped}
    assert set(reasons) == {source_c.id, source_d.id}
    assert "JOIN_REQUEST_PENDING" in reasons[source_c.id], "C must be skipped for its actual stored status, not a Telegram-call failure"
    assert "ACCESS_DENIED" in reasons[source_d.id], "D must be skipped for its actual stored status, not a Telegram-call failure"

    from telegram_app.database.repositories.message_repository import MessageRepository

    assert await MessageRepository(session).count_for_source(source_a.id) == 0
    assert await MessageRepository(session).count_for_source(source_b.id) == 0


async def test_search_selected_sources_skips_unknown_source_id(session, settings):
    manager = StubClientManager(FakeClient())
    service = SearchService(session, manager, settings)

    outcome = await service.search_selected_sources([99999], "attack")

    assert outcome.searched_source_ids == []
    assert outcome.skipped[0].source_id == 99999
    assert outcome.skipped[0].reason == "source not found"


async def test_search_selected_sources_defaults_to_monitoring_enabled_sources(session, settings):
    channel = FakeChannel(id=910, username="defaultsrc")
    fake_client = FakeClient(
        entities={"defaultsrc": channel},
        messages_by_entity_id={910: [FakeMessage(id=1, message="attack in the news", chat_id=910)]},
    )
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    source = TelegramSource(
        identifier="@defaultsrc", telegram_entity_id="910", username="defaultsrc",
        access_status=SourceAccessStatus.MONITORING.value, monitoring_enabled=True,
    )
    session.add(source)
    await session.flush()
    await session.commit()

    outcome = await service.search_selected_sources(None, "attack")

    assert outcome.searched_source_ids == [source.id]
    assert len(outcome.items) == 1


async def test_channel_search_by_source_rejects_bot_without_calling_telegram(session, settings):
    """The exact bug this fixes: a registered bot source must never be
    silently searched as an empty channel (200 + []) - it must be refused
    with a clear reason, and never even reach Telegram to do it."""
    from telegram_app.database.models import SourceType

    source = TelegramSource(
        identifier="@pspkmovies_bot", telegram_entity_id="600", username="pspkmovies_bot",
        source_type=SourceType.BOT.value, access_status=SourceAccessStatus.ACCESSIBLE.value,
    )
    session.add(source)
    await session.flush()
    await session.commit()

    fake_client = FakeClient()
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_source(source.id, "punjab")

    assert exc_info.value.code == "NOT_A_CHANNEL"
    assert fake_client.calls == [], "must not contact Telegram for a known-bot source"


async def test_channel_search_by_identifier_rejects_bot(session, settings):
    from tests.fakes import FakeUser

    bot = FakeUser(id=650, username="livecheckbot", first_name="Live Check", bot=True)
    fake_client = FakeClient(entities={"livecheckbot": bot})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    with pytest.raises(TelegramSearchError) as exc_info:
        await service.channel_search_by_identifier("@livecheckbot", "punjab")

    assert exc_info.value.code == "NOT_A_CHANNEL"
    assert exc_info.value.http_status == 422


async def test_discover_channels_audits_success(session, settings):
    channel = FakeChannel(id=1, title="News Channel", username="newschan")
    fake_client = FakeClient(rpc_responses={ContactsSearchRequest: FakeContactsFound(chats=[channel])})
    manager = StubClientManager(fake_client)
    service = SearchService(session, manager, settings)

    outcome = await service.discover_channels("news")
    assert len(outcome.items) == 1

    audit_repo = AuditRepository(session)
    entries = await audit_repo.list_recent()
    assert any(e.event_type == "CHANNEL_DISCOVERY" and e.success for e in entries)
