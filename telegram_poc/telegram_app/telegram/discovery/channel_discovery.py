"""Discover public channels/groups by keyword, before registering any of
them as a monitored source.

Uses `contacts.search` (Telethon: `telethon.tl.functions.contacts.SearchRequest`),
the general Telegram entity-search RPC available to any account. Telegram
gives this call no pagination mechanism (no offset/cursor parameter) - it is
a single bounded call, so `has_more` is always False here, and that
limitation is documented rather than faked with a cursor that would go
nowhere.

Results are TENTATIVE: `access_status` here is a coarse hint derived only
from entity flags already present in the search response (cheap - no extra
RPC per result). It is NOT the verified status the state machine produces.
Callers must still go through `POST /api/sources` + `POST
/api/sources/{id}/check-access` for an authoritative answer before treating
a discovered channel as accessible.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from telethon.errors import RPCError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import Channel, Chat

from telegram_app.telegram.avatars import stripped_thumb_data_uri
from telegram_app.telegram.discovery.pagination import clamp_limit
from telegram_app.telegram.errors import TelegramSearchError


@dataclass
class TelegramChannelDiscoveryResult:
    telegram_id: str
    access_hash: str | None
    title: str | None
    username: str | None
    type: str  # "channel" | "group"
    public: bool
    verified: bool | None
    member_count: int | None
    access_status: str
    source_url: str | None
    # Free - decoded from bytes contacts.search already returned, no extra
    # Telegram call. None when the entity has no photo. The full-resolution
    # photo is a deliberately separate, on-demand fetch - see
    # GET /api/telegram/channels/{channel_id}/photo.
    photo_thumb_data_uri: str | None = None
    # None unless discover_channels(..., include_descriptions=True) - contacts.search
    # itself never returns `about`, so populating this costs one extra
    # messages.getFullChannel/getFullChat RPC per result (see discover_channels).
    description: str | None = None


@dataclass
class ChannelDiscoveryOutcome:
    items: list[TelegramChannelDiscoveryResult] = field(default_factory=list)
    has_more: bool = False
    warnings: list[str] = field(default_factory=list)


def _coarse_access_status(entity) -> str:
    """A cheap, unverified hint only - see module docstring."""
    if getattr(entity, "left", None) is False:
        return "ALREADY_MEMBER"
    if getattr(entity, "username", None):
        return "PUBLIC_LIKELY"
    return "ACCESS_UNKNOWN"


def _normalize_discovered_chat(entity) -> TelegramChannelDiscoveryResult | None:
    if isinstance(entity, Channel):
        entity_type = "channel" if getattr(entity, "broadcast", False) else "group"
    elif isinstance(entity, Chat):
        entity_type = "group"
    else:
        return None

    username = getattr(entity, "username", None)
    return TelegramChannelDiscoveryResult(
        telegram_id=str(entity.id),
        access_hash=str(getattr(entity, "access_hash", None)) if getattr(entity, "access_hash", None) is not None else None,
        title=getattr(entity, "title", None),
        username=username,
        type=entity_type,
        public=bool(username),
        verified=getattr(entity, "verified", None),
        member_count=getattr(entity, "participants_count", None),
        access_status=_coarse_access_status(entity),
        source_url=f"https://t.me/{username}" if username else None,
        photo_thumb_data_uri=stripped_thumb_data_uri(entity),
    )


async def _fetch_description(client, chat) -> str | None:
    """Best-effort only - a single entity's full-info call failing (rights,
    flood wait, transient RPC error) must never break the rest of the page."""
    try:
        if isinstance(chat, Channel):
            full = await client(GetFullChannelRequest(chat))
            return full.full_chat.about or None
        if isinstance(chat, Chat):
            full = await client(GetFullChatRequest(chat.id))
            return full.full_chat.about or None
    except RPCError:
        pass
    return None


async def discover_channels(
    client, query: str, *, limit: int | None = None, include_descriptions: bool = False,
) -> ChannelDiscoveryOutcome:
    if not query or not query.strip():
        raise TelegramSearchError(code="INVALID_QUERY", message="query must not be empty", http_status=422)
    if len(query) > 256:
        raise TelegramSearchError(code="INVALID_QUERY", message="query must be at most 256 characters", http_status=422)

    page_size = clamp_limit(limit)
    result = await client(ContactsSearchRequest(q=query, limit=page_size))

    seen_ids: set[str] = set()
    items: list[TelegramChannelDiscoveryResult] = []
    matched_chats = []
    for chat in result.chats:
        normalized = _normalize_discovered_chat(chat)
        if normalized is None or normalized.telegram_id in seen_ids:
            continue
        seen_ids.add(normalized.telegram_id)
        items.append(normalized)
        matched_chats.append(chat)

    # Opt-in: contacts.search itself is free (module docstring), but `about`
    # requires one extra RPC per result - never issued unless a caller
    # explicitly asks for it (see app/services/provider_service.py, which
    # opts in; the internal discovery endpoint console.html uses does not).
    if include_descriptions and matched_chats:
        descriptions = await asyncio.gather(*(_fetch_description(client, chat) for chat in matched_chats))
        for item, description in zip(items, descriptions):
            item.description = description

    warnings = ["contacts.search has no native pagination - this is Telegram's complete response for this query."]
    return ChannelDiscoveryOutcome(items=items, has_more=False, warnings=warnings)
