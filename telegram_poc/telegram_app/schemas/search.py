"""Response schemas for the search/discovery API. Never expose SQLAlchemy
models directly here - these are hand-shaped from app/telegram/discovery/*
dataclasses (see app/api/routes_search.py's _to_* converters)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from telegram_app.schemas.message import MessageOut
from telegram_app.schemas.source import SourceOut


class SearchChannelRefOut(BaseModel):
    id: str | None = None
    title: str | None = None
    username: str | None = None
    type: str


class SearchSenderRefOut(BaseModel):
    id: str | None = None
    username: str | None = None
    display_name: str | None = None


class SearchMediaRefOut(BaseModel):
    media_type: str
    filename: str | None = None
    mime_type: str | None = None
    file_size: int | None = None


class TelegramSearchResult(BaseModel):
    message_id: str
    channel: SearchChannelRefOut
    text: str | None = None
    date: datetime | None = None
    sender: SearchSenderRefOut | None = None
    media: SearchMediaRefOut | None = None
    url: str | None = None
    source_type: str = "telegram"
    visibility: str
    collection_method: str
    raw: dict | None = None


class SearchPagination(BaseModel):
    limit: int
    next_cursor: str | None = None
    has_more: bool


class TelegramGlobalSearchResponse(BaseModel):
    results: list[TelegramSearchResult]
    pagination: SearchPagination
    method_used: str
    quota: dict | None = None
    warnings: list[str] = []


# Same result shape as global search - kept as a distinct name per the spec's
# schema list, since channel-scoped search may diverge later (e.g. thread info).
TelegramChannelSearchResult = TelegramSearchResult


class TelegramChannelSearchResponse(BaseModel):
    results: list[TelegramSearchResult]
    pagination: SearchPagination
    warnings: list[str] = []


class TelegramChannelDiscoveryResult(BaseModel):
    telegram_id: str
    access_hash: str | None = None
    title: str | None = None
    username: str | None = None
    type: str
    public: bool
    verified: bool | None = None
    member_count: int | None = None
    access_status: str
    source_url: str | None = None
    description: str | None = Field(
        default=None,
        description="Only populated when the caller opted into the extra per-result Telegram call (the provider API's SEARCH_CHANNELS does; this console-facing endpoint does not).",
    )
    photo_thumb_data_uri: str | None = Field(
        default=None,
        description=(
            "Free low-res preview decoded from the discovery response itself - no extra Telegram call. "
            "None if the entity has no photo. For the real photo, GET /api/telegram/channels/{channel_id}/photo."
        ),
    )


class TelegramChannelDiscoveryResponse(BaseModel):
    results: list[TelegramChannelDiscoveryResult]
    pagination: SearchPagination
    warnings: list[str] = []


class SaveSearchResultRequest(BaseModel):
    """The client resubmits the search result it already received (or just
    enough of it) rather than the server caching every search hit by id -
    see README "Search result persistence" for why."""

    identifier: str  # @username / t.me link / numeric id of the source channel
    telegram_message_id: int
    collection_method: str = "manual_collection"
    monitoring_enabled: bool = False


class SaveSearchResultResponse(BaseModel):
    """Result of POST /api/telegram/search/results/save: the (possibly
    newly-registered) source, the persisted message row, and whether this
    call actually inserted it (False if it was already stored, e.g. saved
    twice or already collected by monitoring/backfill)."""

    source: SourceOut
    message: MessageOut | None
    inserted: bool


class TelegramErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = None


class ExtractLinksRequest(BaseModel):
    """Free text (e.g. one collected message's body) to scan for Telegram
    links. Non-Telegram URLs found in `text` are classified and ignored -
    never fetched - see app/telegram/discovery/url_classifier.py."""

    text: str


class LinkedChannelCandidateOut(BaseModel):
    raw_url: str
    identifier: str | None = None
    access_status: str | None = None
    title: str | None = None
    username: str | None = None
    already_registered_source_id: int | None = None
    reason: str | None = None


class ExtractLinksResponse(BaseModel):
    """Read-only preview: nothing here is a registered source. To track a
    candidate, register it via the existing POST /api/sources."""

    candidates: list[LinkedChannelCandidateOut]


class MultiSourceSearchRequest(BaseModel):
    source_ids: list[int] | None = Field(
        default=None, description="Sources to search. Omit to search every monitoring-enabled source."
    )
    q: str
    limit: int = 20
    cursor: str | None = None
    sender_username: str | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    media_type: str | None = None
    include_raw: bool = False


class SkippedSourceOut(BaseModel):
    source_id: int
    status: str | None = None
    reason: str


class MultiSourceSearchResponse(BaseModel):
    results: list[TelegramSearchResult]
    searched_source_ids: list[int]
    skipped: list[SkippedSourceOut]
    warnings: list[str] = []


class TelegramSearchErrorResponse(BaseModel):
    """Maps to the spec's `TelegramSearchError` schema - named with an
    `...Response` suffix here to avoid colliding with the Python exception
    class of (deliberately) the same conceptual name in app/telegram/errors.py."""

    error: TelegramErrorDetail
