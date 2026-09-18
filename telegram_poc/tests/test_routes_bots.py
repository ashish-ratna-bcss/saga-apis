"""HTTP-layer tests for POST /api/telegram/bots/{id}/start and
POST /api/telegram/invites/join (routes_bots.py) - builds the real router
with dependency overrides, exercising the actual request/response contract."""
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from telethon.errors import InviteRequestSentError
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

from telegram_app.api import routes_bots, routes_sources
from telegram_app.api.deps import get_bot_service, get_client_manager_dep, get_source_service
from telegram_app.database.database import get_db
from telegram_app.services.bot_service import BotService
from telegram_app.services.source_service import SourceService
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeClient, FakeMessage, FakeUser, StubClientManager, make_chat_invite


@pytest_asyncio.fixture
async def bots_client(session, settings):
    fake_client = FakeClient()
    manager = StubClientManager(fake_client)

    app = FastAPI()
    app.include_router(routes_sources.router)
    app.include_router(routes_bots.router)

    @app.exception_handler(TelegramSearchError)
    async def _handler(request: Request, exc: TelegramSearchError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_client_manager_dep] = lambda: manager
    app.dependency_overrides[get_source_service] = lambda: SourceService(session, manager)
    app.dependency_overrides[get_bot_service] = lambda: BotService(session, manager, settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, fake_client, session


async def test_start_bot_endpoint_returns_404_for_missing_source(bots_client):
    client, _fake_client, _session = bots_client

    response = await client.post("/api/telegram/bots/9999/start")

    assert response.status_code == 404


async def test_start_bot_endpoint_rejects_non_bot_source(bots_client):
    client, fake_client, _session = bots_client
    channel = FakeChannel(id=700, title="Channel", username="realchannel", broadcast=True, left=True)
    fake_client.entities["realchannel"] = channel
    fake_client.messages_by_entity_id[700] = []

    create = await client.post("/api/sources", json={"identifier": "@realchannel"})
    source_id = create.json()["id"]

    response = await client.post(f"/api/telegram/bots/{source_id}/start")

    assert response.status_code == 422


async def test_start_bot_endpoint_full_flow(bots_client):
    client, fake_client, _session = bots_client
    bot_entity = FakeUser(id=701, username="flowbot", first_name="Flow Bot", bot=True)
    discovered = FakeChannel(id=800, title="Discovered", username="discoveredviabot", broadcast=True, left=True)
    fake_client.entities["flowbot"] = bot_entity
    fake_client.entities["discoveredviabot"] = discovered
    fake_client.messages_by_entity_id[800] = []
    fake_client.bot_auto_replies[701] = FakeMessage(id=0, message="Try https://t.me/discoveredviabot")

    create = await client.post("/api/sources", json={"identifier": "@flowbot"})
    assert create.json()["source_type"] == "bot"
    source_id = create.json()["id"]

    response = await client.post(f"/api/telegram/bots/{source_id}/start")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert len(body["discovered"]) == 1
    assert body["discovered"][0]["identifier"] == "@discoveredviabot"


async def test_join_by_invite_endpoint_rejects_non_invite_identifier(bots_client):
    client, _fake_client, _session = bots_client

    response = await client.post("/api/telegram/invites/join", json={"identifier": "@somechannel"})

    assert response.status_code == 422


async def test_join_by_invite_endpoint_pending_approval(bots_client):
    client, fake_client, _session = bots_client
    fake_client.rpc_responses[CheckChatInviteRequest] = make_chat_invite(title="Needs Approval", request_needed=True)
    fake_client.rpc_raises[ImportChatInviteRequest] = InviteRequestSentError(None)

    response = await client.post("/api/telegram/invites/join", json={"identifier": "https://t.me/+viahttpinvite"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_approval"
    assert body["source"]["access_status"] == "JOIN_REQUEST_PENDING"
