"""FastAPI dependency wiring. Routes only ever depend on services - never on
Telethon or SQLAlchemy sessions directly (the session is injected here and
handed to a service, which is what routes actually receive)."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_app.config import Settings, get_settings
from telegram_app.database.database import get_db
from telegram_app.services.backfill_service import BackfillService
from telegram_app.services.bot_service import BotService
from telegram_app.services.message_service import MessageService
from telegram_app.services.monitoring_service import MonitoringService
from telegram_app.services.notification_service import NotificationService
from telegram_app.services.provider_service import ProviderService
from telegram_app.services.search_service import SearchService
from telegram_app.services.source_service import SourceService
from telegram_app.telegram.client import TelegramClientManager, get_client_manager


def get_settings_dep() -> Settings:
    return get_settings()


def get_client_manager_dep() -> TelegramClientManager:
    return get_client_manager()


async def get_source_service(
    session: AsyncSession = Depends(get_db),
    client_manager: TelegramClientManager = Depends(get_client_manager_dep),
) -> AsyncGenerator[SourceService, None]:
    yield SourceService(session, client_manager)


async def get_monitoring_service(
    session: AsyncSession = Depends(get_db),
    client_manager: TelegramClientManager = Depends(get_client_manager_dep),
    settings: Settings = Depends(get_settings_dep),
) -> AsyncGenerator[MonitoringService, None]:
    yield MonitoringService(session, client_manager, settings)


async def get_message_service(
    session: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> AsyncGenerator[MessageService, None]:
    yield MessageService(session, settings)


async def get_notification_service(session: AsyncSession = Depends(get_db)) -> AsyncGenerator[NotificationService, None]:
    yield NotificationService(session)


async def get_search_service(
    session: AsyncSession = Depends(get_db),
    client_manager: TelegramClientManager = Depends(get_client_manager_dep),
    settings: Settings = Depends(get_settings_dep),
) -> AsyncGenerator[SearchService, None]:
    yield SearchService(session, client_manager, settings)


async def get_bot_service(
    session: AsyncSession = Depends(get_db),
    client_manager: TelegramClientManager = Depends(get_client_manager_dep),
    settings: Settings = Depends(get_settings_dep),
) -> AsyncGenerator[BotService, None]:
    yield BotService(session, client_manager, settings)


def get_provider_service(
    client_manager: TelegramClientManager = Depends(get_client_manager_dep),
    settings: Settings = Depends(get_settings_dep),
) -> ProviderService:
    return ProviderService(client_manager, settings)


async def get_backfill_service(
    session: AsyncSession = Depends(get_db),
    client_manager: TelegramClientManager = Depends(get_client_manager_dep),
    settings: Settings = Depends(get_settings_dep),
) -> AsyncGenerator[BackfillService, None]:
    yield BackfillService(session, client_manager, settings)
