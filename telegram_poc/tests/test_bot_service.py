"""End-to-end coverage of the bot-start workflow (BotService.start_bot):
    bot source -> /start -> reply -> extract Telegram links (bounded) ->
    register (or refresh) as a DORMANT source - never joined, never
    monitored, never a duplicate, never touching a non-Telegram URL.
"""
from telethon.tl.functions.messages import CheckChatInviteRequest

from telegram_app.database.models import SourceAccessStatus, SourceType, TelegramSource
from telegram_app.database.repositories.audit_repository import AuditRepository
from telegram_app.services.bot_service import BotNotFoundError, BotService, SourceIsNotABotError
from telegram_app.services.source_service import SourceService
from tests.fakes import (
    FakeChannel,
    FakeClient,
    FakeMessage,
    FakeUser,
    StubClientManager,
    make_chat_invite,
    make_chat_invite_already,
    make_url_button_markup,
)


async def _register_bot(session, manager, *, bot_id=600, username="pspkmovies_bot"):
    source_service = SourceService(session, manager)
    return await source_service.register_source(f"@{username}")


async def test_start_bot_not_found(session, settings):
    manager = StubClientManager(FakeClient())
    service = BotService(session, manager, settings)

    try:
        await service.start_bot(9999)
        assert False, "expected BotNotFoundError"
    except BotNotFoundError:
        pass


async def test_start_bot_rejects_non_bot_source(session, settings):
    channel = FakeChannel(id=601, title="Not A Bot", username="notabot", broadcast=True, left=True)
    fake_client = FakeClient(entities={"notabot": channel}, messages_by_entity_id={601: []})
    manager = StubClientManager(fake_client)
    source_service = SourceService(session, manager)
    source = await source_service.register_source("@notabot")
    assert source.source_type == SourceType.CHANNEL.value

    service = BotService(session, manager, settings)
    try:
        await service.start_bot(source.id)
        assert False, "expected SourceIsNotABotError"
    except SourceIsNotABotError:
        pass


async def test_start_bot_discovers_and_registers_channel_from_response(session, settings):
    bot_entity = FakeUser(id=600, username="pspkmovies_bot", first_name="PSPK Movies", bot=True)
    discovered_channel = FakeChannel(id=700, title="Discovered", username="discoveredchan", broadcast=True, left=True)
    fake_client = FakeClient(
        entities={"pspkmovies_bot": bot_entity, "discoveredchan": discovered_channel},
        messages_by_entity_id={700: []},
        bot_auto_replies={
            600: FakeMessage(
                id=0,
                message="Thanks for using me! Check out https://t.me/discoveredchan and also https://example.com/promo",
            )
        },
    )
    manager = StubClientManager(fake_client)
    bot_source = await _register_bot(session, manager)
    assert bot_source.source_type == SourceType.BOT.value

    service = BotService(session, manager, settings)
    result = await service.start_bot(bot_source.id)

    assert result.status == "started"
    assert result.response_text.startswith("Thanks for using me!")
    assert len(result.discovered) == 1, "the external example.com link must never become a discovered source"
    discovered = result.discovered[0]
    assert discovered.identifier == "@discoveredchan"
    assert discovered.access_status == SourceAccessStatus.PUBLIC_ACCESSIBLE.value
    assert discovered.already_existed is False

    stored = await session.get(TelegramSource, discovered.source_id)
    assert stored.discovered_from_source_id == bot_source.id
    assert stored.discovery_type == "BOT_RESPONSE"
    assert stored.monitoring_enabled is False, "discovery must never imply monitoring"


async def test_start_bot_discovers_invite_link_from_button(session, settings):
    bot_entity = FakeUser(id=602, username="invitebot", first_name="Invite Bot", bot=True)
    invite_chat = FakeChannel(id=800, title="Invited Group", username=None, left=True)
    markup = make_url_button_markup([("Join Channel", "https://t.me/+abc123XYZ")])
    fake_client = FakeClient(
        entities={"invitebot": bot_entity},
        bot_auto_replies={602: FakeMessage(id=0, message="Tap below", reply_markup=markup)},
        rpc_responses={CheckChatInviteRequest: make_chat_invite_already(invite_chat)},
    )
    manager = StubClientManager(fake_client)
    bot_source = await _register_bot(session, manager, bot_id=602, username="invitebot")

    service = BotService(session, manager, settings)
    result = await service.start_bot(bot_source.id)

    assert len(result.discovered) == 1
    discovered = result.discovered[0]
    assert discovered.identifier == "https://t.me/+abc123XYZ"
    assert discovered.access_status == SourceAccessStatus.ACCESSIBLE.value


async def test_start_bot_deduplicates_already_registered_discovery(session, settings):
    bot_entity = FakeUser(id=603, username="dedupbot", first_name="Dedup Bot", bot=True)
    existing_channel = FakeChannel(id=900, title="Already Known", username="alreadyknown", broadcast=True, left=True)
    fake_client = FakeClient(
        entities={"dedupbot": bot_entity, "alreadyknown": existing_channel},
        messages_by_entity_id={900: []},
        bot_auto_replies={603: FakeMessage(id=0, message="See https://t.me/alreadyknown")},
    )
    manager = StubClientManager(fake_client)
    bot_source = await _register_bot(session, manager, bot_id=603, username="dedupbot")

    source_service = SourceService(session, manager)
    pre_existing = await source_service.register_source("@alreadyknown")

    service = BotService(session, manager, settings)
    result = await service.start_bot(bot_source.id)

    assert len(result.discovered) == 1
    assert result.discovered[0].source_id == pre_existing.id
    assert result.discovered[0].already_existed is True

    all_sources = await source_service.list_sources(limit=100)
    matching = [s for s in all_sources if s.identifier == "@alreadyknown"]
    assert len(matching) == 1, "must never create a duplicate source for an already-registered discovery"


async def test_start_bot_timed_out_reports_status_and_audits_failure(session, settings):
    bot_entity = FakeUser(id=604, username="silentbot", first_name="Silent Bot", bot=True)
    fake_client = FakeClient(entities={"silentbot": bot_entity})  # no auto-reply configured
    manager = StubClientManager(fake_client)
    bot_source = await _register_bot(session, manager, bot_id=604, username="silentbot")

    fast_settings = settings.model_copy(update={"bot_start_timeout_seconds": 0.05, "bot_start_poll_interval_seconds": 0.01})
    service = BotService(session, manager, fast_settings)

    result = await service.start_bot(bot_source.id)

    assert result.status == "timed_out"
    assert result.discovered == []

    audit_repo = AuditRepository(session)
    entries = await audit_repo.list_recent()
    assert any(e.event_type == "TELEGRAM_BOT_START_FAILED" and not e.success for e in entries)


async def test_start_bot_audits_the_full_sequence(session, settings):
    bot_entity = FakeUser(id=605, username="auditbot", first_name="Audit Bot", bot=True)
    discovered = FakeChannel(id=1000, title="Seen", username="seenchan", broadcast=True, left=True)
    fake_client = FakeClient(
        entities={"auditbot": bot_entity, "seenchan": discovered},
        messages_by_entity_id={1000: []},
        bot_auto_replies={605: FakeMessage(id=0, message="Try https://t.me/seenchan")},
    )
    manager = StubClientManager(fake_client)
    bot_source = await _register_bot(session, manager, bot_id=605, username="auditbot")

    service = BotService(session, manager, settings)
    await service.start_bot(bot_source.id)

    audit_repo = AuditRepository(session)
    entries = await audit_repo.list_recent(limit=100)
    event_types = {e.event_type for e in entries}
    for expected in (
        "TELEGRAM_BOT_DETECTED",
        "TELEGRAM_BOT_START_REQUESTED",
        "TELEGRAM_BOT_START_COMPLETED",
        "TELEGRAM_LINK_DISCOVERED",
    ):
        assert expected in event_types, f"missing audit event {expected}"
