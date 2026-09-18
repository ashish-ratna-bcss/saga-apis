"""Search within one specific, already access-verified channel/group.

This module never checks access itself - by design (see module docstring
in `__init__.py`), access verification is access_manager's job. Callers
(search_service.py) must confirm the source/channel is actually searchable
by this account *before* calling `search_channel`, and must return an
explicit access error instead of calling this function when it is not.
Silently returning an empty result for an inaccessible channel is exactly
what the project requirements prohibit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from telethon.tl.types import (
    InputMessagesFilterDocument,
    InputMessagesFilterEmpty,
    InputMessagesFilterMusic,
    InputMessagesFilterPhotos,
    InputMessagesFilterVideo,
    InputMessagesFilterVoice,
)

from telegram_app.telegram.collector import build_message_url, classify_media, extract_media_metadata
from telegram_app.telegram.discovery.global_search import SearchChannelRef, SearchMediaRef, SearchSenderRef, TelegramSearchResultItem
from telegram_app.telegram.discovery.pagination import clamp_limit, decode_cursor, encode_cursor
from telegram_app.telegram.errors import TelegramSearchError

_MEDIA_FILTERS = {
    "photo": InputMessagesFilterPhotos,
    "video": InputMessagesFilterVideo,
    "document": InputMessagesFilterDocument,
    "voice": InputMessagesFilterVoice,
    "audio": InputMessagesFilterMusic,
}


@dataclass
class ChannelSearchOutcome:
    items: list[TelegramSearchResultItem] = field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    warnings: list[str] = field(default_factory=list)


async def search_channel(
    client,
    entity,
    query: str,
    *,
    channel_title: str | None,
    channel_username: str | None,
    limit: int | None = None,
    cursor: str | None = None,
    sender_username: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    media_type: str | None = None,
    include_raw: bool = False,
) -> ChannelSearchOutcome:
    if not query or not query.strip():
        raise TelegramSearchError(code="INVALID_QUERY", message="query must not be empty", http_status=422)
    if len(query) > 256:
        raise TelegramSearchError(code="INVALID_QUERY", message="query must be at most 256 characters", http_status=422)

    page_size = clamp_limit(limit)
    state = decode_cursor(cursor)
    offset_id = state.get("offset_id", 0)

    media_filter = _MEDIA_FILTERS.get(media_type)
    kwargs: dict = {
        "search": query,
        "limit": page_size,
        "offset_id": offset_id,
        "filter": media_filter() if media_filter else InputMessagesFilterEmpty(),
        "reverse": False,
    }
    if sender_username:
        kwargs["from_user"] = sender_username
    if to_date:
        kwargs["offset_date"] = to_date

    messages = []
    async for message in client.iter_messages(entity, **kwargs):
        if from_date and message.date and message.date.astimezone(timezone.utc) < from_date:
            break
        messages.append(message)
        if len(messages) >= page_size:
            break

    channel_ref = SearchChannelRef(
        id=str(getattr(entity, "id", None)), title=channel_title, username=channel_username,
        type="channel" if getattr(entity, "broadcast", False) else "group",
    )

    items = []
    for message in messages:
        sender_ref = None
        if message.sender_id is not None:
            sender_ref = SearchSenderRef(id=str(message.sender_id), username=None, display_name=None)

        media_type_found = classify_media(message)
        media_ref = None
        if media_type_found:
            extracted = extract_media_metadata(message)
            if extracted:
                m = extracted[0]
                media_ref = SearchMediaRef(media_type=m.media_type, filename=m.filename, mime_type=m.mime_type, file_size=m.file_size)

        items.append(
            TelegramSearchResultItem(
                message_id=str(message.id),
                channel=channel_ref,
                text=message.message or None,
                date=message.date.astimezone(timezone.utc) if message.date else None,
                sender=sender_ref,
                media=media_ref,
                url=build_message_url(channel_username, message.id),
                visibility="public" if channel_username else "member",
                collection_method="channel_search",
                raw=message.to_dict() if include_raw else None,
            )
        )

    has_more = len(messages) >= page_size
    next_cursor = encode_cursor({"offset_id": messages[-1].id}) if has_more and messages else None

    return ChannelSearchOutcome(items=items, next_cursor=next_cursor, has_more=has_more)
