"""Global public-post search across Telegram.

Reuses the two real MTProto search mechanisms already proven out in
`poc/main.py` (this codebase's own prior art):

- `channels.searchPosts` - Telegram's genuine cross-account public post
  search (per Telegram's own docs, it explicitly reaches channels the
  authenticated account has not joined), gated by a small free daily quota
  (checked via `channels.checkSearchPostsFlood`) then Telegram Stars/Premium.
  This service NEVER authorizes a Stars payment automatically - if the free
  quota is exhausted, it falls back rather than spending anything.
- `messages.searchGlobal` - always free, but scoped to what Telegram's
  server-side index already associates with the authenticated account (in
  practice, usually channels it has joined) - NOT an unrestricted scan of
  every public channel. Used as the fallback, and results are locally
  re-verified for a literal keyword match since this index can be
  token/fuzzy-based.

Neither call is equivalent to "search all of Telegram." See README.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from telethon.errors import FloodWaitError, PremiumAccountRequiredError, RPCError
from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import (
    InputMessagesFilterDocument,
    InputMessagesFilterEmpty,
    InputMessagesFilterMusic,
    InputMessagesFilterPhotos,
    InputMessagesFilterVideo,
    InputMessagesFilterVoice,
    InputPeerChannel,
    InputPeerChat,
    InputPeerEmpty,
    InputPeerUser,
    PeerChannel,
    PeerChat,
    PeerUser,
)

from telegram_app.telegram.collector import build_message_url, classify_media, extract_media_metadata
from telegram_app.telegram.discovery.pagination import clamp_limit, decode_cursor, encode_cursor
from telegram_app.telegram.errors import TelegramSearchError

logger = logging.getLogger("telegram_service.telegram.discovery.global_search")

_MEDIA_FILTERS = {
    "photo": InputMessagesFilterPhotos,
    "video": InputMessagesFilterVideo,
    "document": InputMessagesFilterDocument,
    "voice": InputMessagesFilterVoice,
    "audio": InputMessagesFilterMusic,
}


@dataclass
class SearchChannelRef:
    id: str | None
    title: str | None
    username: str | None
    type: str


@dataclass
class SearchSenderRef:
    id: str | None
    username: str | None
    display_name: str | None


@dataclass
class SearchMediaRef:
    media_type: str
    filename: str | None
    mime_type: str | None
    file_size: int | None


@dataclass
class TelegramSearchResultItem:
    message_id: str
    channel: SearchChannelRef
    text: str | None
    date: datetime | None
    sender: SearchSenderRef | None
    media: SearchMediaRef | None
    url: str | None
    visibility: str
    collection_method: str
    source_type: str = "telegram"
    raw: dict | None = None


@dataclass
class GlobalSearchOutcome:
    items: list[TelegramSearchResultItem] = field(default_factory=list)
    method_used: str = "messages.searchGlobal"
    next_cursor: str | None = None
    has_more: bool = False
    quota: dict | None = None
    warnings: list[str] = field(default_factory=list)


def _build_entity_indexes(chats, users):
    return {c.id: c for c in chats}, {u.id: u for u in users}


def _resolve_peer(peer, chats_by_id, users_by_id):
    if isinstance(peer, PeerChannel):
        entity = chats_by_id.get(peer.channel_id)
        kind = "channel"
    elif isinstance(peer, PeerChat):
        entity = chats_by_id.get(peer.chat_id)
        kind = "group"
    elif isinstance(peer, PeerUser):
        entity = users_by_id.get(peer.user_id)
        kind = "user"
    else:
        return None, None, None, "unknown"

    if entity is None:
        return None, None, None, kind

    title = getattr(entity, "title", None)
    if title is None:
        first = getattr(entity, "first_name", None)
        last = getattr(entity, "last_name", None)
        title = " ".join(p for p in (first, last) if p) or None
    if kind == "channel" and getattr(entity, "broadcast", True) is False:
        kind = "group"  # megagroups arrive as Channel too; broadcast=False means it's really a group
    return entity, title, getattr(entity, "username", None), kind


def _input_peer_from_message(message, chats_by_id, users_by_id):
    peer = message.peer_id
    if isinstance(peer, PeerChannel):
        entity = chats_by_id.get(peer.channel_id)
        if entity is not None:
            return InputPeerChannel(channel_id=peer.channel_id, access_hash=getattr(entity, "access_hash", 0) or 0)
    elif isinstance(peer, PeerChat):
        return InputPeerChat(chat_id=peer.chat_id)
    elif isinstance(peer, PeerUser):
        entity = users_by_id.get(peer.user_id)
        if entity is not None:
            return InputPeerUser(user_id=peer.user_id, access_hash=getattr(entity, "access_hash", 0) or 0)
    return InputPeerEmpty()


def _normalize_result_message(message, chats_by_id, users_by_id, *, visibility: str, include_raw: bool) -> TelegramSearchResultItem:
    entity, title, username, kind = _resolve_peer(message.peer_id, chats_by_id, users_by_id)
    channel_ref = SearchChannelRef(
        id=str(getattr(entity, "id", None)) if entity is not None else None, title=title, username=username, type=kind
    )

    sender_ref = None
    if message.sender_id is not None:
        sender_entity = users_by_id.get(message.sender_id) or chats_by_id.get(message.sender_id)
        sender_username = getattr(sender_entity, "username", None) if sender_entity else None
        sender_display = title if sender_entity is None else (
            " ".join(p for p in (getattr(sender_entity, "first_name", None), getattr(sender_entity, "last_name", None)) if p)
            or getattr(sender_entity, "title", None)
        )
        sender_ref = SearchSenderRef(id=str(message.sender_id), username=sender_username, display_name=sender_display)

    media_type = classify_media(message)
    media_ref = None
    if media_type:
        extracted = extract_media_metadata(message)
        if extracted:
            m = extracted[0]
            media_ref = SearchMediaRef(media_type=m.media_type, filename=m.filename, mime_type=m.mime_type, file_size=m.file_size)

    return TelegramSearchResultItem(
        message_id=str(message.id),
        channel=channel_ref,
        text=message.message or None,
        date=message.date.astimezone(timezone.utc) if message.date else None,
        sender=sender_ref,
        media=media_ref,
        url=build_message_url(username, message.id),
        visibility=visibility,
        collection_method="global_search",
        raw=message.to_dict() if include_raw else None,
    )


async def _call_search_global(client, query, *, limit, min_date, max_date, offset_rate, offset_peer, offset_id, media_filter):
    return await client(
        SearchGlobalRequest(
            q=query,
            filter=media_filter or InputMessagesFilterEmpty(),
            min_date=min_date,
            max_date=max_date,
            offset_rate=offset_rate,
            offset_peer=offset_peer,
            offset_id=offset_id,
            limit=limit,
            broadcasts_only=True,
        )
    )


async def _call_search_posts(client, query, *, limit, offset_rate, offset_peer, offset_id):
    return await client(
        SearchPostsRequest(
            offset_rate=offset_rate, offset_peer=offset_peer, offset_id=offset_id, limit=limit, hashtag=None, query=query,
        )
    )


def _apply_local_filters(items: list[TelegramSearchResultItem], *, query, channel_username, channel_id, media_type, from_date, to_date, verify_keyword: bool):
    filtered = []
    for item in items:
        if verify_keyword and query.lower() not in (item.text or "").lower():
            continue
        if channel_username and (item.channel.username or "").lower() != channel_username.lower():
            continue
        if channel_id and item.channel.id != str(channel_id):
            continue
        if media_type and (item.media.media_type if item.media else None) != media_type:
            continue
        if from_date and item.date and item.date < from_date:
            continue
        if to_date and item.date and item.date > to_date:
            continue
        filtered.append(item)
    return filtered


async def search_global(
    client,
    query: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    channel_username: str | None = None,
    channel_id: str | None = None,
    media_type: str | None = None,
    sort: str = "date_desc",
    include_raw: bool = False,
    prefer_channel_posts: bool = True,
) -> GlobalSearchOutcome:
    """Runs a global public-post search. Prefers `channels.searchPosts`
    (the genuine cross-membership search) and falls back to
    `messages.searchGlobal` when the former is unavailable/unaffordable -
    never paying Telegram Stars automatically."""
    if not query or not query.strip():
        raise TelegramSearchError(code="INVALID_QUERY", message="query must not be empty", http_status=422)
    if len(query) > 256:
        raise TelegramSearchError(code="INVALID_QUERY", message="query must be at most 256 characters", http_status=422)

    page_size = clamp_limit(limit)
    state = decode_cursor(cursor)
    offset_rate = state.get("offset_rate", 0)
    offset_id = state.get("offset_id", 0)
    offset_peer_state = state.get("offset_peer")
    if offset_peer_state and offset_peer_state.get("kind") == "channel":
        offset_peer = InputPeerChannel(channel_id=offset_peer_state["id"], access_hash=offset_peer_state.get("access_hash") or 0)
    elif offset_peer_state and offset_peer_state.get("kind") == "chat":
        offset_peer = InputPeerChat(chat_id=offset_peer_state["id"])
    elif offset_peer_state and offset_peer_state.get("kind") == "user":
        offset_peer = InputPeerUser(user_id=offset_peer_state["id"], access_hash=offset_peer_state.get("access_hash") or 0)
    else:
        offset_peer = InputPeerEmpty()

    warnings: list[str] = []
    quota_info = None
    method_used = "messages.searchGlobal"
    result = None

    if prefer_channel_posts:
        try:
            flood = await client(CheckSearchPostsFloodRequest(query=query))
            is_free = getattr(flood, "query_is_free", False) or flood.remains > 0
            quota_info = {
                "remaining": flood.remains, "total_daily": flood.total_daily,
                "stars_required": 0 if is_free else flood.stars_amount,
            }
            if is_free:
                result = await _call_search_posts(client, query, limit=page_size, offset_rate=offset_rate, offset_peer=offset_peer, offset_id=offset_id)
                method_used = "channels.searchPosts"
            else:
                warnings.append(
                    f"channels.searchPosts free quota exhausted (would cost {flood.stars_amount} Telegram Stars) - "
                    "falling back to messages.searchGlobal; no payment was made."
                )
        except PremiumAccountRequiredError:
            warnings.append("channels.searchPosts is unavailable for this account - falling back to messages.searchGlobal.")
        except FloodWaitError:
            raise
        except RPCError as exc:
            logger.warning("channels.checkSearchPostsFlood failed (%s) - falling back", exc.__class__.__name__)
            warnings.append(f"could not check channels.searchPosts quota ({exc.__class__.__name__}) - falling back.")

    if result is None:
        media_filter = _MEDIA_FILTERS.get(media_type)
        result = await _call_search_global(
            client, query, limit=page_size, min_date=from_date, max_date=to_date,
            offset_rate=offset_rate, offset_peer=offset_peer, offset_id=offset_id,
            media_filter=media_filter() if media_filter else None,
        )
        method_used = "messages.searchGlobal"

    chats_by_id, users_by_id = _build_entity_indexes(result.chats, result.users)
    visibility = "public" if method_used == "channels.searchPosts" else "account_scoped"
    if visibility == "account_scoped":
        warnings.append(
            "messages.searchGlobal results are scoped to what Telegram's index associates with this account "
            "(typically channels it has already joined) - not an unrestricted search of all public Telegram content."
        )

    items = [
        _normalize_result_message(m, chats_by_id, users_by_id, visibility=visibility, include_raw=include_raw)
        for m in result.messages
    ]

    items = _apply_local_filters(
        items, query=query, channel_username=channel_username, channel_id=channel_id, media_type=media_type,
        from_date=from_date, to_date=to_date, verify_keyword=(method_used == "messages.searchGlobal"),
    )

    if sort == "date_desc":
        items.sort(key=lambda i: i.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    next_cursor = None
    has_more = len(result.messages) >= page_size
    if has_more and result.messages:
        last_message = result.messages[-1]
        next_rate = getattr(result, "next_rate", None) or 0
        input_peer = _input_peer_from_message(last_message, chats_by_id, users_by_id)
        peer_state: dict | None = None
        if isinstance(input_peer, InputPeerChannel):
            peer_state = {"kind": "channel", "id": input_peer.channel_id, "access_hash": input_peer.access_hash}
        elif isinstance(input_peer, InputPeerChat):
            peer_state = {"kind": "chat", "id": input_peer.chat_id}
        elif isinstance(input_peer, InputPeerUser):
            peer_state = {"kind": "user", "id": input_peer.user_id, "access_hash": input_peer.access_hash}
        next_cursor = encode_cursor({"offset_rate": next_rate, "offset_id": last_message.id, "offset_peer": peer_state})

    return GlobalSearchOutcome(
        items=items, method_used=method_used, next_cursor=next_cursor, has_more=has_more, quota=quota_info, warnings=warnings,
    )
