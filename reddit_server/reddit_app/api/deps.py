"""FastAPI dependencies.

Routes receive a :class:`ProviderService` bound to the process-wide Reddit client.
Routes never call the Reddit REST client or httpx directly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from reddit_app.core.config import Settings, get_settings
from reddit_app.reddit.client import RedditClientManager
from reddit_app.services.provider_service import ProviderService
from reddit_app.services.reddit_rss_service import RedditRssService


def get_app_settings(request: Request) -> Settings:
    """The settings instance created at startup."""

    return getattr(request.app.state, "settings", None) or get_settings()


def get_client_manager(request: Request) -> RedditClientManager:
    return request.app.state.clients


def get_provider_service(request: Request) -> ProviderService:
    return ProviderService(request.app.state.clients)


def get_rss_service(request: Request) -> RedditRssService:
    settings: Settings = get_app_settings(request)
    clients: RedditClientManager = request.app.state.clients
    return RedditRssService(
        clients.rss, clients.feed_cache, event_threshold=settings.reddit_rss_event_threshold
    )


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ClientsDep = Annotated[RedditClientManager, Depends(get_client_manager)]
ProviderServiceDep = Annotated[ProviderService, Depends(get_provider_service)]
RssServiceDep = Annotated[RedditRssService, Depends(get_rss_service)]
