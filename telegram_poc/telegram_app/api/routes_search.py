"""Global search, channel search, and channel discovery.

Every route here only ever calls SearchService or SourceService - never
Telethon, never a repository directly.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from telegram_app.api.deps import get_search_service, get_source_service
from telegram_app.schemas.search import (
    ExtractLinksRequest,
    ExtractLinksResponse,
    LinkedChannelCandidateOut,
    MultiSourceSearchRequest,
    MultiSourceSearchResponse,
    SaveSearchResultRequest,
    SaveSearchResultResponse,
    SearchChannelRefOut,
    SearchMediaRefOut,
    SearchPagination,
    SearchSenderRefOut,
    SkippedSourceOut,
    TelegramChannelDiscoveryResponse,
    TelegramChannelDiscoveryResult,
    TelegramChannelSearchResponse,
    TelegramGlobalSearchResponse,
    TelegramSearchResult,
)
from telegram_app.schemas.source import SourceOut
from telegram_app.services.search_service import SearchService
from telegram_app.services.source_service import SourceAlreadyExistsError, SourceService
from telegram_app.telegram.discovery.channel_discovery import TelegramChannelDiscoveryResult as DiscoveryResultDC
from telegram_app.telegram.discovery.global_search import TelegramSearchResultItem

router = APIRouter(tags=["search"])


def _to_search_result(item: TelegramSearchResultItem) -> TelegramSearchResult:
    return TelegramSearchResult(
        message_id=item.message_id,
        channel=SearchChannelRefOut(id=item.channel.id, title=item.channel.title, username=item.channel.username, type=item.channel.type),
        text=item.text,
        date=item.date,
        sender=SearchSenderRefOut(id=item.sender.id, username=item.sender.username, display_name=item.sender.display_name) if item.sender else None,
        media=SearchMediaRefOut(media_type=item.media.media_type, filename=item.media.filename, mime_type=item.media.mime_type, file_size=item.media.file_size) if item.media else None,
        url=item.url,
        source_type=item.source_type,
        visibility=item.visibility,
        collection_method=item.collection_method,
        raw=item.raw,
    )


def _to_discovery_result(item: DiscoveryResultDC) -> TelegramChannelDiscoveryResult:
    return TelegramChannelDiscoveryResult(
        telegram_id=item.telegram_id, access_hash=item.access_hash, title=item.title, username=item.username,
        type=item.type, public=item.public, verified=item.verified, member_count=item.member_count,
        access_status=item.access_status, source_url=item.source_url,
        description=item.description, photo_thumb_data_uri=item.photo_thumb_data_uri,
    )


@router.get(
    "/api/telegram/search/global",
    response_model=TelegramGlobalSearchResponse,
    summary="Live Telegram-wide search",
    description=(
        "LIVE Telegram search across public channels this account can see (channels."
        "searchPosts, falling back to messages.searchGlobal when the paid-search quota is "
        "exhausted) - never reads the locally stored telegram_messages table."
    ),
)
async def global_search(
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    channel_username: str | None = None,
    channel_id: str | None = None,
    media_type: str | None = None,
    sort: str = Query(default="date_desc", pattern="^(date_desc|relevance)$"),
    include_raw: bool = False,
    service: SearchService = Depends(get_search_service),
):
    outcome = await service.global_search(
        q, limit=limit, cursor=cursor, from_date=from_date, to_date=to_date,
        channel_username=channel_username, channel_id=channel_id, media_type=media_type,
        sort=sort, include_raw=include_raw,
    )
    return TelegramGlobalSearchResponse(
        results=[_to_search_result(i) for i in outcome.items],
        pagination=SearchPagination(limit=limit, next_cursor=outcome.next_cursor, has_more=outcome.has_more),
        method_used=outcome.method_used,
        quota=outcome.quota,
        warnings=outcome.warnings,
    )


@router.post(
    "/api/telegram/search/sources",
    response_model=MultiSourceSearchResponse,
    summary="Live Telegram search across a selected set of sources",
    description=(
        "LIVE Telegram search across the given `source_ids` (or, if omitted, "
        "every monitoring-enabled source) - reuses the same per-channel "
        "search and access rules as GET /api/sources/{id}/search. A source "
        "whose stored access status is not currently searchable (e.g. still "
        "JOIN_REQUEST_PENDING, or ACCESS_DENIED) is skipped, never queried, "
        "and listed in `skipped` with the reason - the call never fails just "
        "because some of the requested sources aren't accessible."
    ),
)
async def search_selected_sources(payload: MultiSourceSearchRequest, service: SearchService = Depends(get_search_service)):
    outcome = await service.search_selected_sources(
        payload.source_ids, payload.q, limit=payload.limit, cursor=payload.cursor,
        sender_username=payload.sender_username, from_date=payload.from_date, to_date=payload.to_date,
        media_type=payload.media_type, include_raw=payload.include_raw,
    )
    return MultiSourceSearchResponse(
        results=[_to_search_result(i) for i in outcome.items],
        searched_source_ids=outcome.searched_source_ids,
        skipped=[SkippedSourceOut(source_id=s.source_id, status=s.status, reason=s.reason) for s in outcome.skipped],
        warnings=outcome.warnings,
    )


@router.post(
    "/api/telegram/discovery/extract-links",
    response_model=ExtractLinksResponse,
    summary="Extract and classify Telegram links from text (read-only)",
    description=(
        "Scans `text` (e.g. one collected message's body) for t.me/telegram.me "
        "links, resolves each one's live access state, and returns a preview "
        "list - it never registers a source, submits a join request, or "
        "enables monitoring. Non-Telegram URLs in the text are classified and "
        "discarded, never fetched. To track a candidate, register it via the "
        "existing POST /api/sources."
    ),
)
async def extract_telegram_links(payload: ExtractLinksRequest, service: SourceService = Depends(get_source_service)):
    candidates = await service.discover_linked_channels(payload.text)
    return ExtractLinksResponse(
        candidates=[
            LinkedChannelCandidateOut(
                raw_url=c.raw_url, identifier=c.identifier, access_status=c.access_status,
                title=c.title, username=c.username, already_registered_source_id=c.already_registered_source_id,
                reason=c.reason,
            )
            for c in candidates
        ]
    )


@router.get(
    "/api/telegram/channels/search",
    response_model=TelegramChannelDiscoveryResponse,
    summary="Telegram channel/group discovery (not message search)",
    description=(
        "Finds candidate channels/groups matching `q` via Telegram's own discovery API "
        "(contacts.search) - returns channels, not messages. To search messages *within* "
        "one of the returned channels, register it (POST /api/telegram/channels/{telegram_id}"
        "/register) and use GET /api/sources/{source_id}/search, or search it directly via "
        "GET /api/telegram/channels/{channel_id}/search."
    ),
)
async def discover_channels(
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
    service: SearchService = Depends(get_search_service),
):
    outcome = await service.discover_channels(q, limit=limit)
    return TelegramChannelDiscoveryResponse(
        results=[_to_discovery_result(i) for i in outcome.items],
        pagination=SearchPagination(limit=limit, next_cursor=None, has_more=outcome.has_more),
        warnings=outcome.warnings,
    )


@router.get(
    "/api/telegram/channels/{channel_id}/photo",
    summary="Fetch a channel/group/user's real profile photo (on demand)",
    description=(
        "Downloads the full-resolution profile photo for `channel_id` (any identifier "
        "normalize_identifier accepts) directly from Telegram and returns the JPEG bytes. "
        "Distinct from the free `photo_thumb_data_uri` already included in channel discovery "
        "and search results - that thumbnail costs nothing (decoded from bytes Telegram already "
        "sent); this endpoint issues one additional Telegram RPC per call, cached to disk "
        "afterwards so repeat requests for the same entity cost nothing further. Returns 404 "
        "if the entity has no photo at all."
    ),
    responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "entity has no photo"}},
)
async def get_channel_photo(channel_id: str, service: SearchService = Depends(get_search_service)):
    photo = await service.get_channel_photo(channel_id)
    if photo is None:
        raise HTTPException(status_code=404, detail="this Telegram entity has no profile photo")
    return Response(content=photo, media_type="image/jpeg")


@router.get(
    "/api/telegram/channels/{channel_id}/search",
    response_model=TelegramChannelSearchResponse,
    summary="Live Telegram search within one arbitrary (not-yet-registered) channel",
    description=(
        "LIVE Telegram search - every call goes to Telegram via Telethon, never to the "
        "locally stored telegram_messages table. Does a fresh access probe against "
        "`channel_id` on every call, since it isn't backed by a stored source record. "
        "For an already-registered source, prefer GET /api/sources/{source_id}/search, "
        "which trusts the source's stored access status instead of re-probing every time."
    ),
)
async def search_channel_by_id(
    channel_id: str,
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    sender_username: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    media_type: str | None = None,
    include_raw: bool = False,
    service: SearchService = Depends(get_search_service),
):
    outcome = await service.channel_search_by_identifier(
        channel_id, q, limit=limit, cursor=cursor, sender_username=sender_username,
        from_date=from_date, to_date=to_date, media_type=media_type, include_raw=include_raw,
    )
    return TelegramChannelSearchResponse(
        results=[_to_search_result(i) for i in outcome.items],
        pagination=SearchPagination(limit=limit, next_cursor=outcome.next_cursor, has_more=outcome.has_more),
        warnings=outcome.warnings,
    )


@router.get(
    "/api/sources/{source_id}/search",
    response_model=TelegramChannelSearchResponse,
    summary="Live Telegram search within an already-registered source",
    description=(
        "LIVE Telegram search - every call queries Telegram directly via Telethon "
        "(messages.search on the resolved entity); it never reads the locally stored "
        "telegram_messages table. `sender_username`/`media_type`/`from_date`/`to_date` are "
        "applied to the live Telegram results (natively via Telegram's search RPC where "
        "supported, otherwise as a local pass over the same live results) - never against "
        "stored rows. For locally stored/collected messages instead, see "
        "GET /api/sources/{source_id}/messages."
    ),
)
async def search_registered_source(
    source_id: int,
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    sender_username: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    media_type: str | None = None,
    include_raw: bool = False,
    service: SearchService = Depends(get_search_service),
):
    outcome = await service.channel_search_by_source(
        source_id, q, limit=limit, cursor=cursor, sender_username=sender_username,
        from_date=from_date, to_date=to_date, media_type=media_type, include_raw=include_raw,
    )
    return TelegramChannelSearchResponse(
        results=[_to_search_result(i) for i in outcome.items],
        pagination=SearchPagination(limit=limit, next_cursor=outcome.next_cursor, has_more=outcome.has_more),
        warnings=outcome.warnings,
    )


@router.post("/api/telegram/channels/{telegram_id}/register", response_model=SourceOut, status_code=201)
async def register_discovered_channel(telegram_id: str, monitoring_enabled: bool = True, service: SourceService = Depends(get_source_service)):
    """Registers a channel discovered via GET /api/telegram/channels/search
    as a monitored source - reuses the exact same registration/dedup/
    access-check path as POST /api/sources (a numeric Telegram id is one of
    the identifier shapes normalize_identifier already recognizes)."""
    try:
        return await service.register_source(telegram_id, monitoring_enabled=monitoring_enabled)
    except SourceAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/api/telegram/search/results/save", response_model=SaveSearchResultResponse, status_code=201)
async def save_search_result(
    payload: SaveSearchResultRequest,
    search_service: SearchService = Depends(get_search_service),
):
    """Promotes one search result into a registered source (creating it if
    needed) and persists the exact message it pointed at - re-fetched live
    from Telegram by (source, telegram_message_id), since the client only
    resubmits the identifying pair, not the full message body (see
    SaveSearchResultRequest). This is the one and only path a search result
    is ever written to the database through: search itself stays read-only/
    ephemeral (see README "Search result persistence"); nothing is
    persisted just by being returned from GET /api/telegram/search/global
    or /api/telegram/channels/{id}/search."""
    try:
        source, message, inserted = await search_service.save_search_result(
            payload.identifier,
            payload.telegram_message_id,
            collection_method=payload.collection_method,
            monitoring_enabled=payload.monitoring_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return SaveSearchResultResponse(source=source, message=message, inserted=inserted)
