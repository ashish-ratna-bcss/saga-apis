"""API-layer tests: build a minimal FastAPI app around the real routers with
dependency overrides, so these exercise routing/serialization/HTTP status
codes without the real Telethon client or a real on-disk database."""
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from telegram_app.api import routes_messages, routes_monitoring, routes_notifications, routes_sources
from telegram_app.api.deps import get_client_manager_dep, get_message_service, get_monitoring_service, get_notification_service, get_source_service
from telegram_app.database.database import get_db
from telegram_app.services.message_service import MessageService
from telegram_app.services.monitoring_service import MonitoringService
from telegram_app.services.notification_service import NotificationService
from telegram_app.services.source_service import SourceService
from tests.fakes import FakeChannel, FakeClient, StubClientManager


@pytest_asyncio.fixture
async def api_client(session, settings):
    channel = FakeChannel(id=500, title="Api News", username="apinews", broadcast=True, left=True)
    fake_client = FakeClient(entities={"apinews": channel}, messages_by_entity_id={500: []})
    manager = StubClientManager(fake_client)

    app = FastAPI()
    app.include_router(routes_sources.router)
    app.include_router(routes_monitoring.router)
    app.include_router(routes_messages.router)
    app.include_router(routes_notifications.router)

    async def _get_db_override():
        yield session

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_client_manager_dep] = lambda: manager
    app.dependency_overrides[get_source_service] = lambda: SourceService(session, manager)
    app.dependency_overrides[get_monitoring_service] = lambda: MonitoringService(session, manager, settings)
    app.dependency_overrides[get_message_service] = lambda: MessageService(session, settings)
    app.dependency_overrides[get_notification_service] = lambda: NotificationService(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_create_and_get_source(api_client):
    response = await api_client.post("/api/sources", json={"identifier": "@apinews", "monitoring_enabled": True})
    assert response.status_code == 201
    body = response.json()
    assert body["access_status"] == "PUBLIC_ACCESSIBLE"
    source_id = body["id"]

    get_response = await api_client.get(f"/api/sources/{source_id}")
    assert get_response.status_code == 200
    assert get_response.json()["username"] == "apinews"


async def test_create_duplicate_source_conflicts(api_client):
    await api_client.post("/api/sources", json={"identifier": "@apinews"})
    response = await api_client.post("/api/sources", json={"identifier": "@apinews"})
    assert response.status_code == 409


async def test_get_missing_source_404(api_client):
    response = await api_client.get("/api/sources/9999")
    assert response.status_code == 404


async def test_start_monitoring_and_list_messages(api_client):
    create = await api_client.post("/api/sources", json={"identifier": "@apinews"})
    source_id = create.json()["id"]

    start = await api_client.post(f"/api/sources/{source_id}/monitoring/start")
    assert start.status_code == 200
    assert start.json()["access_status"] == "MONITORING"

    messages = await api_client.get(f"/api/sources/{source_id}/messages")
    assert messages.status_code == 200
    assert messages.json() == []


async def test_notifications_list_starts_empty(api_client):
    response = await api_client.get("/api/notifications")
    assert response.status_code == 200
    assert response.json() == []


async def test_health_and_ready_not_wired_here_but_sources_list_works(api_client):
    response = await api_client.get("/api/sources")
    assert response.status_code == 200
    assert response.json() == []
