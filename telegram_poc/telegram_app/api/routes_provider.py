"""Telegram provider API - a stable, storage-decoupled JSON contract for an
external system (originally built for Sockeye/Blugate) to fetch Telegram
data over. The consuming system owns polling/storage/alerts/UI; every route
here only ever reads live from Telegram and returns JSON - none of them
touch app/database/* or require the /api/sources or .../monitoring/
start|stop lifecycle app/api/routes_search.py and routes_sources.py use.

HEALTH (`GET /health`), READY (`GET /ready`) and STATUS (`GET
/api/telegram/status`) already exist at exactly those paths (see
app/main.py, app/api/routes_auth.py) and are intentionally not duplicated
here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from telegram_app.api.deps import get_provider_service, get_search_service
from telegram_app.schemas.provider import (
    ChannelIdentifierRequest,
    ChannelMessagesRequest,
    JoinInviteRequest,
    MessageRepliesRequest,
    MessageRequest,
    ProviderAccessResponse,
    ProviderChannelOut,
    ProviderChannelSummaryOut,
    ProviderJoinResponse,
    ProviderMessageOut,
    ProviderMessagesResponse,
    ProviderRepliesResponse,
    ProviderResolveResponse,
    ProviderSearchChannelsResponse,
    ProviderSearchMessagesResponse,
    ResolveLinkRequest,
)
from telegram_app.services.provider_service import MessagePage, ProviderService
from telegram_app.services.search_service import SearchService
from telegram_app.telegram.discovery.channel_discovery import TelegramChannelDiscoveryResult
from telegram_app.telegram.discovery.global_search import TelegramSearchResultItem

router = APIRouter(prefix="/api/telegram", tags=["provider"])


def _photo_url(request: Request, photo_path: str | None) -> str | None:
    if not photo_path:
        return None
    return str(request.base_url).rstrip("/") + photo_path


def _finalize_channel(channel: dict | None, request: Request) -> dict | None:
    if channel is None:
        return None
    channel = dict(channel)
    channel["photo_url"] = _photo_url(request, channel.pop("photo_path", None))
    return channel


def _finalize_message(message: dict, request: Request) -> dict:
    message = dict(message)
    finalized_media = []
    for m in message.get("media", []):
        m = dict(m)
        m["url"] = _photo_url(request, m.pop("url_path", None))
        finalized_media.append(m)
    message["media"] = finalized_media
    return message


def _messages_response(page: MessagePage, request: Request) -> ProviderMessagesResponse:
    return ProviderMessagesResponse(
        items=[ProviderMessageOut(**_finalize_message(item, request)) for item in page.items], cursor=page.cursor,
    )


def _search_item_to_message(item: TelegramSearchResultItem) -> dict:
    author = None
    if item.sender:
        author = {"id": item.sender.id, "username": item.sender.username, "name": item.sender.display_name}
    media = []
    if item.media and item.channel and item.channel.id:
        media = [{
            "type": item.media.media_type, "filename": item.media.filename,
            "mime_type": item.media.mime_type, "file_size": item.media.file_size,
            "url_path": f"/api/telegram/channels/{item.channel.id}/messages/{item.message_id}/media",
        }]
    return {
        "id": item.message_id,
        "channel_id": item.channel.id if item.channel else None,
        "text": item.text,
        "date": item.date,
        "url": item.url,
        "views": None,
        "forwards": None,
        "replies_count": None,
        "media": media,
        "author": author,
    }


def _discovery_item_to_channel(item: TelegramChannelDiscoveryResult, request: Request) -> ProviderChannelSummaryOut:
    photo_path = f"/api/telegram/channels/{item.telegram_id}/photo" if item.photo_thumb_data_uri else None
    return ProviderChannelSummaryOut(
        id=item.telegram_id, username=item.username, title=item.title, type=item.type,
        description=item.description, members_count=item.member_count,
        photo_url=_photo_url(request, photo_path), url=item.source_url,
    )


@router.post("/channel", response_model=ProviderChannelOut, summary="[Provider] Resolve channel/group/bot metadata")
async def provider_channel_info(
    payload: ChannelIdentifierRequest, request: Request, service: ProviderService = Depends(get_provider_service),
):
    result = await service.channel_info(username=payload.username, url=payload.url, channel_id=payload.channel_id)
    return _finalize_channel(result, request)


@router.post(
    "/channel/messages", response_model=ProviderMessagesResponse,
    summary="[Provider] Recent messages for a channel (live, not a keyword search)",
)
async def provider_channel_messages(
    payload: ChannelMessagesRequest, request: Request, service: ProviderService = Depends(get_provider_service),
):
    page = await service.channel_messages(
        username=payload.username, channel_id=payload.channel_id, limit=payload.limit, cursor=payload.cursor,
    )
    return _messages_response(page, request)


@router.post(
    "/message", response_model=ProviderMessageOut, summary="[Provider] Single message by URL or (channel_id, message_id)",
    responses={404: {"description": "message not found"}},
)
async def provider_message(payload: MessageRequest, request: Request, service: ProviderService = Depends(get_provider_service)):
    result = await service.message(url=payload.url, channel_id=payload.channel_id, message_id=payload.message_id)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found")
    return _finalize_message(result, request)


@router.post(
    "/message/replies", response_model=ProviderRepliesResponse,
    summary="[Provider] Replies/comments under a message (requires the channel's discussion group)",
)
async def provider_message_replies(payload: MessageRepliesRequest, service: ProviderService = Depends(get_provider_service)):
    page = await service.message_replies(
        channel_id=payload.channel_id, message_id=payload.message_id, limit=payload.limit, cursor=payload.cursor,
    )
    return ProviderRepliesResponse(items=page.items, cursor=page.cursor)


@router.get(
    "/search/messages", response_model=ProviderSearchMessagesResponse,
    summary="[Provider] Keyword search across Telegram messages",
)
async def provider_search_messages(
    request: Request,
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = None,
    service: SearchService = Depends(get_search_service),
):
    outcome = await service.global_search(q, limit=limit, cursor=cursor)
    items = [_finalize_message(_search_item_to_message(i), request) for i in outcome.items]
    return ProviderSearchMessagesResponse(items=items, cursor=outcome.next_cursor)


@router.get(
    "/search/channels", response_model=ProviderSearchChannelsResponse,
    summary="[Provider] Discover channels/groups by keyword",
)
async def provider_search_channels(
    request: Request,
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(default=20, ge=1, le=100),
    service: SearchService = Depends(get_search_service),
):
    outcome = await service.discover_channels(q, limit=limit, include_descriptions=True)
    return ProviderSearchChannelsResponse(items=[_discovery_item_to_channel(i, request) for i in outcome.items])


@router.post("/resolve", response_model=ProviderResolveResponse, summary="[Provider] Parse any t.me/... link")
async def provider_resolve_link(payload: ResolveLinkRequest, request: Request, service: ProviderService = Depends(get_provider_service)):
    result = await service.resolve_link(payload.url)
    result["channel"] = _finalize_channel(result.get("channel"), request)
    if result.get("message") is not None:
        result["message"] = _finalize_message(result["message"], request)
    return result


@router.post("/channel/access", response_model=ProviderAccessResponse, summary="[Provider] Can this account read the channel?")
async def provider_check_access(payload: ChannelIdentifierRequest, service: ProviderService = Depends(get_provider_service)):
    return await service.check_access(username=payload.username, url=payload.url, channel_id=payload.channel_id)


@router.post("/invite/join", response_model=ProviderJoinResponse, summary="[Provider] Join a channel/group via invite link")
async def provider_join_invite(payload: JoinInviteRequest, request: Request, service: ProviderService = Depends(get_provider_service)):
    result = await service.join_invite(payload.invite)
    result["channel"] = _finalize_channel(result.get("channel"), request)
    return result


@router.get(
    "/channels/{channel_id}/messages/{message_id}/media",
    summary="[Provider] Fetch a message's attached photo/video/document (on demand)",
    description=(
        "Downloads the real media bytes attached to one message and returns them directly - "
        "this is what every `media[].url` in CHANNEL_MESSAGES/MESSAGE/SEARCH_MESSAGES points at. "
        "Issues one Telegram RPC per call, cached to disk afterwards so repeat requests for the "
        "same message cost nothing further. Returns 404 if the message doesn't exist or has no "
        "media, 413 if the file is over this service's configured size limit."
    ),
    responses={200: {"content": {"*/*": {}}}, 404: {"description": "message not found, or has no media"}},
)
async def provider_message_media(channel_id: str, message_id: str, service: ProviderService = Depends(get_provider_service)):
    result = await service.get_message_media(channel_id=channel_id, message_id=message_id)
    if result is None:
        raise HTTPException(status_code=404, detail="message not found, or this message has no media")
    content, content_type = result
    return Response(content=content, media_type=content_type)
