"""Whole-surface endpoint smoke test.

Every path the real application publishes in its OpenAPI schema is called
here at least once against the real routers, with only Telethon and the
database faked. Two things are asserted:

1. No published endpoint 5xx's or unexpectedly 404s on its happy path - a
   route wired to the wrong prefix, or a response_model that cannot
   serialize what its service actually returns, fails here.
2. The set of endpoints exercised equals the set the real app publishes, so
   a newly added route cannot land without a smoke test and a deleted one
   cannot leave a stale test behind.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy import select
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

import telegram_app.services.backfill_service as backfill_module
from telegram_app.api import (
    routes_auth,
    routes_backfill,
    routes_bots,
    routes_messages,
    routes_monitoring,
    routes_notifications,
    routes_provider,
    routes_search,
    routes_sources,
)
from telegram_app.api.deps import (
    get_backfill_service,
    get_bot_service,
    get_client_manager_dep,
    get_message_service,
    get_monitoring_service,
    get_notification_service,
    get_provider_service,
    get_search_service,
    get_settings_dep,
    get_source_service,
)
from telegram_app.database.database import get_db
from telegram_app.database.models import Notification, ProcessingStatus, TelegramMessage
from telegram_app.services.backfill_service import BackfillService
from telegram_app.services.bot_service import BotService
from telegram_app.services.message_service import MessageService
from telegram_app.services.monitoring_service import MonitoringService
from telegram_app.services.notification_service import NotificationService
from telegram_app.services.provider_service import ProviderService
from telegram_app.services.search_service import SearchService
from telegram_app.services.source_service import SourceService
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import (
    FakeChannel,
    FakeClient,
    FakeContactsFound,
    FakeFile,
    FakeMessage,
    FakeSearchPostsFlood,
    FakeSearchResult,
    FakeUser,
    StubClientManager,
    make_chat_invite,
)

CHANNEL_ID = 700
CHANNEL_USERNAME = "smokechannel"
BOT_ID = 701
BOT_USERNAME = "smokebot"


def published_endpoints() -> set[tuple[str, str]]:
    """(METHOD, path) for every endpoint the real app serves."""
    from telegram_app.main import app as real_app

    schema = real_app.openapi()
    return {(verb.upper(), path) for path, ops in schema["paths"].items() for verb in ops}


@pytest_asyncio.fixture
async def smoke(session, settings, monkeypatch):
    from types import SimpleNamespace

    from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, GetFullChannelRequest, SearchPostsRequest
    from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
    from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest, SearchGlobalRequest

    channel = FakeChannel(id=CHANNEL_ID, title="Smoke Channel", username=CHANNEL_USERNAME, broadcast=True, left=True)
    bot_entity = FakeUser(id=BOT_ID, username=BOT_USERNAME, first_name="Smoke Bot", bot=True)
    messages = [
        FakeMessage(id=11, chat_id=CHANNEL_ID, message="alpha smoke hit", sender_username="poster"),
        FakeMessage(id=12, chat_id=CHANNEL_ID, message="beta smoke hit", sender_username="poster"),
        # A reply/comment on message 11 - covers the provider API's
        # MESSAGE_REPLIES endpoint (POST /api/telegram/message/replies).
        FakeMessage(id=14, chat_id=CHANNEL_ID, message="smoke reply", sender_username="poster", reply_to_msg_id=11),
        # A media-only post - covers the provider API's message-media
        # endpoint (GET /api/telegram/channels/{id}/messages/{id}/media).
        FakeMessage(id=15, chat_id=CHANNEL_ID, message="", sender_username="poster",
                    media_kind="photo", file=FakeFile(mime_type="image/jpeg", size=2048)),
    ]
    global_hit = FakeMessage(id=13, chat_id=CHANNEL_ID, message="global smoke hit", sender_username="poster")

    fake_client = FakeClient(
        entities={
            CHANNEL_USERNAME: channel,
            str(CHANNEL_ID): channel,
            CHANNEL_ID: channel,
            BOT_USERNAME: bot_entity,
            BOT_ID: bot_entity,
        },
        messages_by_entity_id={CHANNEL_ID: messages, BOT_ID: []},
        message_media={15: b"fake-jpeg-bytes"},
        rpc_responses={
            SearchGlobalRequest: FakeSearchResult(messages=[global_hit], chats=[channel]),
            SearchPostsRequest: FakeSearchResult(messages=[global_hit], chats=[channel]),
            CheckSearchPostsFloodRequest: FakeSearchPostsFlood(),
            ContactsSearchRequest: FakeContactsFound(chats=[channel]),
            CheckChatInviteRequest: make_chat_invite(title="Smoke Invite"),
            ImportChatInviteRequest: None,
            # Provider API's CHANNEL_INFO/RESOLVE_LINK best-effort enrichment
            # (app/services/provider_service.py's _entity_to_channel_info).
            GetFullChannelRequest: SimpleNamespace(full_chat=SimpleNamespace(about="Smoke channel about", participants_count=42)),
        },
        bot_auto_replies={BOT_ID: FakeMessage(id=900, chat_id=BOT_ID, message="Welcome to the smoke bot")},
    )
    manager = StubClientManager(fake_client)

    # Backfill queues a background asyncio task that opens its own session
    # against the configured database. This file is checking routing and
    # serialization, not the walk itself (tests/test_backfill_service.py owns
    # that), so the job body is stubbed out to keep the smoke run from
    # touching anything outside its in-memory session.
    async def _no_op_backfill_job(*args, **kwargs):
        return None

    monkeypatch.setattr(backfill_module, "_run_backfill_job", _no_op_backfill_job)

    app = FastAPI()
    for module in (
        routes_auth,
        routes_sources,
        routes_monitoring,
        routes_messages,
        routes_notifications,
        routes_search,
        routes_bots,
        routes_backfill,
        routes_provider,
    ):
        app.include_router(module.router)

    @app.exception_handler(TelegramSearchError)
    async def _search_error(request: Request, exc: TelegramSearchError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.get("/health")
    async def _health():
        return {"status": "ok"}

    @app.get("/ready")
    async def _ready():
        return {"status": "ready", "telegram_configured": manager.is_configured}

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_client_manager_dep] = lambda: manager
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_source_service] = lambda: SourceService(session, manager)
    app.dependency_overrides[get_monitoring_service] = lambda: MonitoringService(session, manager, settings)
    app.dependency_overrides[get_message_service] = lambda: MessageService(session, settings)
    app.dependency_overrides[get_notification_service] = lambda: NotificationService(session)
    app.dependency_overrides[get_search_service] = lambda: SearchService(session, manager, settings)
    app.dependency_overrides[get_bot_service] = lambda: BotService(session, manager, settings)
    app.dependency_overrides[get_backfill_service] = lambda: BackfillService(session, manager, settings)
    app.dependency_overrides[get_provider_service] = lambda: ProviderService(manager, settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, session, fake_client


async def test_every_published_endpoint_answers(smoke):
    client, session, fake_client = smoke
    called: set[tuple[str, str]] = set()

    async def hit(method, url, template, *, expect, **kwargs):
        response = await client.request(method, url, **kwargs)
        assert response.status_code == expect, (
            f"{method} {template} -> {response.status_code} (expected {expect}): {response.text[:400]}"
        )
        called.add((method, template))
        return response

    # --- liveness --------------------------------------------------------
    await hit("GET", "/health", "/health", expect=200)
    await hit("GET", "/ready", "/ready", expect=200)

    # --- telegram connection + non-interactive login ---------------------
    await hit("GET", "/api/telegram/status", "/api/telegram/status", expect=200)
    await hit("POST", "/api/telegram/auth/send-code", "/api/telegram/auth/send-code",
              expect=200, json={"phone": "+15551234567"})
    await hit("POST", "/api/telegram/auth/verify-code", "/api/telegram/auth/verify-code",
              expect=200, json={"phone": "+15551234567", "code": "12345"})
    await hit("POST", "/api/telegram/auth/verify-password", "/api/telegram/auth/verify-password",
              expect=200, json={"password": "smoke-password"})

    # --- source registration + CRUD --------------------------------------
    created = await hit("POST", "/api/sources", "/api/sources", expect=201,
                        json={"identifier": "@" + CHANNEL_USERNAME, "monitoring_enabled": True})
    source_id = created.json()["id"]

    await hit("GET", "/api/sources", "/api/sources", expect=200)
    await hit("GET", f"/api/sources/{source_id}", "/api/sources/{source_id}", expect=200)
    await hit("PATCH", f"/api/sources/{source_id}", "/api/sources/{source_id}",
              expect=200, json={"title": "Renamed By Smoke Test"})

    # --- access lifecycle -------------------------------------------------
    await hit("GET", f"/api/sources/{source_id}/access-status",
              "/api/sources/{source_id}/access-status", expect=200)
    await hit("POST", f"/api/sources/{source_id}/check-access",
              "/api/sources/{source_id}/check-access", expect=200)
    # A public channel needs no access request, so the endpoint's contract
    # here is a 409 that names the state - it only accepts a source sitting
    # in JOIN_REQUEST_REQUIRED.
    await hit("POST", f"/api/sources/{source_id}/request-access",
              "/api/sources/{source_id}/request-access", expect=409)

    # --- monitoring -------------------------------------------------------
    await hit("POST", f"/api/sources/{source_id}/monitoring/start",
              "/api/sources/{source_id}/monitoring/start", expect=200)
    await hit("GET", f"/api/sources/{source_id}/monitoring-status",
              "/api/sources/{source_id}/monitoring-status", expect=200)
    await hit("POST", f"/api/sources/{source_id}/monitoring/stop",
              "/api/sources/{source_id}/monitoring/stop", expect=200)

    # --- historical backfill ----------------------------------------------
    started = await hit("POST", f"/api/sources/{source_id}/backfill",
                        "/api/sources/{source_id}/backfill", expect=202,
                        json={"limit": 50})
    job_id = started.json()["id"]

    await hit("GET", f"/api/sources/{source_id}/backfill",
              "/api/sources/{source_id}/backfill", expect=200)
    await hit("GET", f"/api/sources/{source_id}/backfill/{job_id}",
              "/api/sources/{source_id}/backfill/{job_id}", expect=200)

    # --- stored messages --------------------------------------------------
    # Starting monitoring above already ran a collection pass, so rows exist
    # here as a result of the real pipeline rather than a hand-inserted
    # fixture. Fall back to inserting one only if that ever stops being true.
    stored = (
        await session.execute(select(TelegramMessage).order_by(TelegramMessage.id).limit(1))
    ).scalar_one_or_none()
    if stored is None:
        stored = TelegramMessage(
            source_id=source_id,
            telegram_message_id=9911,
            sender_id="1",
            sender_username="poster",
            sender_display_name="Poster",
            text="alpha smoke hit",
            processing_status=ProcessingStatus.RAW,
        )
        session.add(stored)
        await session.commit()
        await session.refresh(stored)

    await hit("GET", f"/api/sources/{source_id}/messages",
              "/api/sources/{source_id}/messages", expect=200)
    await hit("GET", "/api/messages?keyword=alpha", "/api/messages", expect=200)
    await hit("GET", f"/api/messages/{stored.id}", "/api/messages/{message_id}", expect=200)

    # --- notifications ----------------------------------------------------
    notification = Notification(event_type="SMOKE_TEST", source_id=source_id, payload={"note": "smoke"})
    session.add(notification)
    await session.commit()
    await session.refresh(notification)

    await hit("GET", "/api/notifications", "/api/notifications", expect=200)
    await hit("POST", f"/api/notifications/{notification.id}/read",
              "/api/notifications/{notification_id}/read", expect=200)

    # --- live search ------------------------------------------------------
    await hit("GET", f"/api/sources/{source_id}/search?q=smoke",
              "/api/sources/{source_id}/search", expect=200)
    await hit("GET", "/api/telegram/search/global?q=smoke",
              "/api/telegram/search/global", expect=200)
    await hit("POST", "/api/telegram/search/sources", "/api/telegram/search/sources",
              expect=200, json={"q": "smoke", "source_ids": [source_id]})
    await hit("GET", f"/api/telegram/channels/{CHANNEL_USERNAME}/search?q=smoke",
              "/api/telegram/channels/{channel_id}/search", expect=200)
    # The smoke channel's FakeClient has no profile_photos entry for it, so
    # this proves the endpoint resolves the entity and reports "no photo"
    # correctly rather than erroring - the real download path is covered by
    # test_search_service.py's get_channel_photo tests.
    await hit("GET", f"/api/telegram/channels/{CHANNEL_USERNAME}/photo",
              "/api/telegram/channels/{channel_id}/photo", expect=404)

    # --- provider API (Sockeye/Blugate contract, app/api/routes_provider.py) --
    # Storage-decoupled: no /api/sources row involved anywhere below.
    await hit("POST", "/api/telegram/channel", "/api/telegram/channel",
              expect=200, json={"username": CHANNEL_USERNAME})
    await hit("POST", "/api/telegram/channel/messages", "/api/telegram/channel/messages",
              expect=200, json={"username": CHANNEL_USERNAME, "limit": 10})
    await hit("POST", "/api/telegram/message", "/api/telegram/message",
              expect=200, json={"channel_id": CHANNEL_USERNAME, "message_id": "11"})
    await hit("POST", "/api/telegram/message/replies", "/api/telegram/message/replies",
              expect=200, json={"channel_id": CHANNEL_USERNAME, "message_id": "11"})
    await hit("GET", "/api/telegram/search/messages?q=smoke", "/api/telegram/search/messages", expect=200)
    await hit("GET", "/api/telegram/search/channels?q=smoke", "/api/telegram/search/channels", expect=200)
    await hit("POST", "/api/telegram/resolve", "/api/telegram/resolve",
              expect=200, json={"url": f"https://t.me/{CHANNEL_USERNAME}"})
    await hit("POST", "/api/telegram/channel/access", "/api/telegram/channel/access",
              expect=200, json={"username": CHANNEL_USERNAME})
    await hit("POST", "/api/telegram/invite/join", "/api/telegram/invite/join",
              expect=200, json={"invite": "https://t.me/+smokeinvitehash"})
    await hit("GET", f"/api/telegram/channels/{CHANNEL_USERNAME}/messages/15/media",
              "/api/telegram/channels/{channel_id}/messages/{message_id}/media", expect=200)

    # --- discovery ---------------------------------------------------------
    await hit("GET", "/api/telegram/channels/search?q=smoke",
              "/api/telegram/channels/search", expect=200)
    await hit("POST", "/api/telegram/discovery/extract-links",
              "/api/telegram/discovery/extract-links", expect=200,
              json={"text": f"see https://t.me/{CHANNEL_USERNAME} and https://example.com/ignored"})

    # --- promoting one search result into stored data ----------------------
    await hit("POST", "/api/telegram/search/results/save",
              "/api/telegram/search/results/save", expect=201,
              json={"identifier": "@" + CHANNEL_USERNAME, "telegram_message_id": 12})

    # --- bot + invite workflows --------------------------------------------
    bot_source = await client.post("/api/sources", json={"identifier": "@" + BOT_USERNAME, "monitoring_enabled": False})
    assert bot_source.status_code == 201, bot_source.text
    bot_source_id = bot_source.json()["id"]

    await hit("POST", f"/api/telegram/bots/{bot_source_id}/start",
              "/api/telegram/bots/{source_id}/start", expect=200)
    await hit("POST", "/api/telegram/invites/join", "/api/telegram/invites/join",
              expect=200, json={"identifier": "https://t.me/+smokeinvitehash"})

    # Already registered above by @username, so re-registering the same
    # channel by its numeric id must be rejected as a duplicate - that 409
    # is this endpoint's real contract, not a failure.
    await hit("POST", f"/api/telegram/channels/{CHANNEL_ID}/register",
              "/api/telegram/channels/{telegram_id}/register", expect=409)

    # --- deletion last, so nothing above depends on a removed row ----------
    await hit("DELETE", f"/api/sources/{bot_source_id}", "/api/sources/{source_id}", expect=204)

    missing = published_endpoints() - called
    assert not missing, f"published endpoints never exercised: {sorted(missing)}"
