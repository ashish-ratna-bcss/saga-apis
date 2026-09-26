"""FastAPI dependencies.

Routes receive a :class:`RedditRssService` bound to the process-wide Reddit RSS
client. Routes never call the RSS client or httpx directly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from reddit_app.core.config import Settings, get_settings
from reddit_app.reddit.client import RedditClientManager
from reddit_app.services.reddit_rss_service import RedditRssService


def get_app_settings(request: Request) -> Settings:
    """The settings instance created at startup."""

    return getattr(request.app.state, "settings", None) or get_settings()


def get_rss_service(request: Request) -> RedditRssService:
    settings: Settings = get_app_settings(request)
    clients: RedditClientManager = request.app.state.clients
    return RedditRssService(
        clients.rss, clients.feed_cache, event_threshold=settings.reddit_rss_event_threshold
    )


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
RssServiceDep = Annotated[RedditRssService, Depends(get_rss_service)]
