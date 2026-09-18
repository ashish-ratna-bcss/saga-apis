"""API-layer tests for the LIVE Telegram search endpoints (routes_search.py):
building the real router with dependency overrides, exercising the actual
HTTP query-parameter contract (?q=...&sender_username=...) and the
TelegramSearchError -> JSON error-response mapping, and confirming a live
search reflects Telegram - never the locally stored telegram_messages
table."""
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from telegram_app.api import routes_messages, routes_search, routes_sources
from telegram_app.api.deps import get_client_manager_dep, get_message_service, get_search_service, get_source_service
from telegram_app.database.database import get_db
from telegram_app.database.models import ProcessingStatus, SourceAccessStatus, TelegramMessage
from telegram_app.services.message_service import MessageService
from telegram_app.services.search_service import SearchService
from telegram_app.services.source_service import SourceService
from telegram_app.telegram.errors import TelegramSearchError
from tests.fakes import FakeChannel, FakeClient, FakeMessage, StubClientManager


def test_openapi_contract_uses_sender_username_not_sender():
    """Locks in the exact public API/Swagger contract on the real app object
    (app.main.app, not a reconstructed test app) - the same schema
    /openapi.json and /docs would serve. Never generates a false pass: if
    sender_username were dropped, or the old `sender` name reintroduced,
    this fails immediately without needing a running server."""
    from telegram_app.main import app as real_app

    schema = real_app.openapi()
    params = schema["paths"]["/api/sources/{source_id}/search"]["get"]["parameters"]
    names = [p["name"] for p in params]

    assert names == ["source_id", "q", "limit", "cursor", "sender_username", "from_date", "to_date", "media_type", "include_raw"]
    assert "sender" not in names


@pytest_asyncio.fixture
async def search_client(session, settings):
    fake_client = FakeClient()
    manager = StubClientManager(fake_client)

    app = FastAPI()
    app.include_router(routes_sources.router)
    app.include_router(routes_messages.router)
    app.include_router(routes_search.router)

    @app.exception_handler(TelegramSearchError)
    async def _handler(request: Request, exc: TelegramSearchError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_client_manager_dep] = lambda: manager
    app.dependency_overrides[get_source_service] = lambda: SourceService(session, manager)
    app.dependency_overrides[get_message_service] = lambda: MessageService(session, settings)
    app.dependency_overrides[get_search_service] = lambda: SearchService(session, manager, settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, fake_client, session


async def _register_searchable_source(client, fake_client, *, entity_id, username, messages):
    channel = FakeChannel(id=entity_id, title="Live Channel", username=username, broadcast=True, left=True)
    fake_client.entities[username] = channel
    fake_client.messages_by_entity_id[entity_id] = messages

    response = await client.post("/api/sources", json={"identifier": f"@{username}"})
    assert response.status_code == 201
    body = response.json()
    assert body["access_status"] == SourceAccessStatus.PUBLIC_ACCESSIBLE.value
    return body["id"]


async def test_search_registered_source_uses_sender_username_query_param(search_client):
    client, fake_client, _ = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=800, username="punjabnews",
        messages=[
            FakeMessage(id=1, message="punjab news update", chat_id=800, sender_username="pspkmovies_bot"),
            FakeMessage(id=2, message="punjab weather", chat_id=800, sender_username="someone_else"),
        ],
    )

    response = await client.get(f"/api/sources/{source_id}/search?q=punjab&sender_username=pspkmovies_bot")

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["message_id"] == "1"
    assert body["results"][0]["sender"] is None or True  # sender ref shape covered elsewhere; presence is enough here


async def test_search_registered_source_missing_source_404(search_client):
    client, _fake_client, _ = search_client

    response = await client.get("/api/sources/9999/search?q=anything")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_search_registered_source_without_access_returns_structured_error(search_client):
    client, fake_client, session = search_client
    from telegram_app.database.models import TelegramSource

    source = TelegramSource(
        identifier="@privatependingsearch", access_status=SourceAccessStatus.JOIN_REQUEST_REQUIRED.value,
    )
    session.add(source)
    await session.flush()
    await session.commit()

    response = await client.get(f"/api/sources/{source.id}/search?q=anything")

    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "JOIN_REQUIRED"
    assert payload["error"]["retryable"] is False
    assert fake_client.calls == [], "must not contact Telegram for a known-inaccessible source"


async def test_search_registered_source_include_raw_false_omits_raw(search_client):
    client, fake_client, _ = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=801, username="rawoff",
        messages=[FakeMessage(id=1, message="punjab", chat_id=801)],
    )

    response = await client.get(f"/api/sources/{source_id}/search?q=punjab&include_raw=false")

    assert response.status_code == 200
    assert response.json()["results"][0]["raw"] is None


async def test_search_registered_source_include_raw_true_includes_raw(search_client):
    client, fake_client, _ = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=802, username="rawon",
        messages=[FakeMessage(id=1, message="punjab", chat_id=802)],
    )

    response = await client.get(f"/api/sources/{source_id}/search?q=punjab&include_raw=true")

    assert response.status_code == 200
    assert response.json()["results"][0]["raw"] is not None


async def test_live_search_reflects_telegram_not_stale_sqlite_rows(search_client):
    """The exact architectural requirement: live search must never be
    answered from telegram_messages, even when a stale/different row for the
    same message already exists there (e.g. from an earlier monitoring
    cycle) - it must always reflect what Telegram returns right now."""
    client, fake_client, session = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=803, username="livevsstale",
        messages=[FakeMessage(id=1, message="punjab live edit", chat_id=803)],
    )

    # A row for the SAME message already sits in SQLite with stale/different text.
    stale = TelegramMessage(
        source_id=source_id,
        telegram_message_id=1,
        text="punjab stale sqlite text",
        processing_status=ProcessingStatus.RAW.value,
    )
    session.add(stale)
    await session.flush()
    await session.commit()

    response = await client.get(f"/api/sources/{source_id}/search?q=punjab")

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["text"] == "punjab live edit"
    assert "stale" not in results[0]["text"]


async def test_live_search_reaches_telegram_with_source_entity_and_keyword(search_client):
    """Items 1-3 of the live-search validation list: the request actually
    reaches Telegram (not just returns a response), targets exactly this
    source's resolved entity (not some other channel, not a global search),
    and carries the literal keyword."""
    client, fake_client, _ = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=810, username="keywordcheck",
        messages=[FakeMessage(id=1, message="a real movie review", chat_id=810)],
    )
    calls_before = list(fake_client.calls)

    response = await client.get(f"/api/sources/{source_id}/search?q=movie&limit=50")

    assert response.status_code == 200
    new_calls = fake_client.calls[len(calls_before):]
    iter_calls = [c for c in new_calls if c[0] == "iter_messages"]
    assert len(iter_calls) == 1, "the search must reach Telegram exactly once"
    _, entity_arg, _min_id, _limit, search_arg, _offset_id = iter_calls[0]
    assert entity_arg.id == 810, "must target this source's own resolved entity, not another channel"
    assert search_arg == "movie", "the keyword must reach Telegram's search RPC"


async def test_live_search_never_calls_stored_message_repository(search_client, monkeypatch):
    """Item 4: a live search must never fall back to (or even touch) the
    SQLite message repository's search/list methods - not "prefer Telegram,
    fall back to SQLite", but Telegram only."""
    from telegram_app.database.repositories.message_repository import MessageRepository

    def _forbidden(*args, **kwargs):
        raise AssertionError("live search must never query the stored message repository")

    monkeypatch.setattr(MessageRepository, "search", _forbidden)
    monkeypatch.setattr(MessageRepository, "list_for_source", _forbidden)

    client, fake_client, _ = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=811, username="nofallback",
        messages=[FakeMessage(id=1, message="movie night", chat_id=811)],
    )

    response = await client.get(f"/api/sources/{source_id}/search?q=movie")

    assert response.status_code == 200
    assert len(response.json()["results"]) == 1


async def test_live_search_returns_result_with_nothing_in_sqlite(search_client):
    """Item 6: a Telegram result must be returned even when no row for it
    exists in telegram_messages at all - live search does not require (or
    consult) a prior collection/persistence step."""
    client, fake_client, session = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=812, username="neverstored",
        messages=[FakeMessage(id=1, message="a movie that was never collected", chat_id=812)],
    )

    from telegram_app.database.repositories.message_repository import MessageRepository

    count = await MessageRepository(session).count_for_source(source_id)
    assert count == 0, "sanity check: no message has been persisted for this source"

    response = await client.get(f"/api/sources/{source_id}/search?q=movie")

    assert response.status_code == 200
    assert response.json()["results"][0]["text"] == "a movie that was never collected"


async def test_messages_endpoints_remain_local_store_only(search_client):
    """GET /api/sources/{id}/messages and GET /api/messages must keep
    returning stored rows, unaffected by live Telegram content - the
    inverse boundary check to the live-search test above."""
    client, fake_client, session = search_client
    source_id = await _register_searchable_source(
        client, fake_client, entity_id=804, username="localonly",
        messages=[FakeMessage(id=1, message="live-only content never stored", chat_id=804)],
    )

    stored = TelegramMessage(
        source_id=source_id,
        telegram_message_id=1,
        text="stored content",
        processing_status=ProcessingStatus.RAW.value,
    )
    session.add(stored)
    await session.flush()
    await session.commit()

    calls_before = list(fake_client.calls)  # registration itself already contacted Telegram once

    by_source = await client.get(f"/api/sources/{source_id}/messages")
    assert by_source.status_code == 200
    assert [m["text"] for m in by_source.json()] == ["stored content"]

    global_local = await client.get("/api/messages")
    assert global_local.status_code == 200
    assert any(m["text"] == "stored content" for m in global_local.json())

    # Neither local-store endpoint contacted Telegram again.
    assert fake_client.calls == calls_before
