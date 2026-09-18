"""Telegram provider API - the storage-decoupled contract external systems
(e.g. Sockeye/Blugate) integrate against.

Deliberately independent of app/database/*: every method here reads live
from Telegram and returns plain JSON-shaped dicts - nothing is persisted,
and nothing requires a registered `TelegramSource` row or the
/api/sources/.../monitoring/start|stop lifecycle. That split is the whole
point of this module (see the request this was built from): the consuming
system owns scheduling/storage/alerts, this service only owns "fetch
Telegram data, return JSON".

Internal endpoints (routes_search.py, routes_sources.py, ...) stay exactly
as they are - this is an additional, parallel surface, not a replacement.
"""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from telethon.errors import RPCError
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import Channel, Chat, User

from telegram_app.config import Settings
from telegram_app.telegram import access_manager, avatars, collector, join_request_manager, media_files
from telegram_app.telegram.client import TelegramClientManager
from telegram_app.telegram.discovery import (
    IdentifierKind,
    NormalizedIdentifier,
    classify_telegram_url,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    normalize_identifier,
)
from telegram_app.telegram.errors import TelegramSearchError, translate_telegram_error

_TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
_MESSAGE_PATH_RE = re.compile(r"^([A-Za-z0-9_]{3,32})/(\d+)$")

_ACCESS_STATE_MAP = {
    "PUBLIC_ACCESSIBLE": "public",
    "ACCESSIBLE": "member",
    "JOIN_REQUEST_REQUIRED": "invite_required",
    "ACCESS_DENIED": "denied",
    "NOT_FOUND": "unknown",
}


def _parse_message_url(url: str) -> tuple[str, int] | None:
    """`https://t.me/<username>/<message_id>` -> (username, message_id), or
    None for anything else (invite links, bare channel links, non-Telegram
    URLs, or `t.me/c/<internal_id>/<id>` private-channel links - the latter
    are a known gap, not supported by this parser)."""
    from urllib.parse import urlsplit

    text = (url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _TELEGRAM_HOSTS:
        return None
    match = _MESSAGE_PATH_RE.match(parts.path.strip("/"))
    if not match:
        return None
    return match.group(1), int(match.group(2))


@dataclass
class MessagePage:
    items: list[dict]
    cursor: str | None


class ProviderService:
    def __init__(self, client_manager: TelegramClientManager, settings: Settings) -> None:
        self.client_manager = client_manager
        self.settings = settings

    async def _require_session(self) -> None:
        ok, err = await self.client_manager.verify_authorized()
        if not ok:
            raise TelegramSearchError(code="NOT_AUTHENTICATED", message=err or "not authenticated", http_status=401, retryable=False)

    @property
    def client(self):
        return self.client_manager.client

    async def _resolve(self, identifier: str):
        normalized = normalize_identifier(identifier)
        try:
            return await access_manager.resolve_entity(self.client, normalized)
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc

    @staticmethod
    def _pick_identifier(*, username: str | None, url: str | None, channel_id: str | None) -> str:
        for value in (username, url, channel_id):
            if value:
                return value
        raise TelegramSearchError(
            code="INVALID_REQUEST", message="one of username/url/channel_id is required", http_status=422, retryable=False,
        )

    # -- CHANNEL_INFO ------------------------------------------------------ #

    async def channel_info(self, *, username: str | None = None, url: str | None = None, channel_id: str | None = None) -> dict:
        await self._require_session()
        identifier = self._pick_identifier(username=username, url=url, channel_id=channel_id)
        entity = await self._resolve(identifier)
        return await self._entity_to_channel_info(entity)

    async def _entity_to_channel_info(self, entity) -> dict:
        source_type = access_manager.classify_entity_type(entity)
        entity_username = getattr(entity, "username", None)
        title = getattr(entity, "title", None) or getattr(entity, "first_name", None)
        description = None
        members_count = getattr(entity, "participants_count", None)

        try:
            if isinstance(entity, Channel):
                full = await self.client(GetFullChannelRequest(entity))
                description = full.full_chat.about or None
                members_count = full.full_chat.participants_count
            elif isinstance(entity, Chat):
                full = await self.client(GetFullChatRequest(entity.id))
                description = full.full_chat.about or None
                participants = getattr(full.full_chat.participants, "participants", None)
                if participants is not None:
                    members_count = len(participants)
            elif isinstance(entity, User):
                full = await self.client(GetFullUserRequest(entity))
                description = full.full_user.about or None
        except RPCError:
            pass  # best-effort enrichment only - identity fields above still stand

        has_photo = avatars.stripped_thumb_data_uri(entity) is not None

        return {
            "id": str(entity.id),
            "username": entity_username,
            "title": title,
            "type": source_type.value,
            "description": description,
            "members_count": members_count,
            "photo_path": f"/api/telegram/channels/{entity.id}/photo" if has_photo else None,
            "is_public": bool(entity_username),
            "url": f"https://t.me/{entity_username}" if entity_username else None,
        }

    # -- CHANNEL_MESSAGES ---------------------------------------------------- #

    async def channel_messages(
        self, *, username: str | None = None, channel_id: str | None = None, limit: int | None = None, cursor: str | None = None,
    ) -> MessagePage:
        await self._require_session()
        identifier = self._pick_identifier(username=username, url=None, channel_id=channel_id)
        entity = await self._resolve(identifier)

        page_size = clamp_limit(limit)
        offset_id = decode_cursor(cursor).get("offset_id", 0)

        messages = []
        try:
            async for message in self.client.iter_messages(entity, limit=page_size, offset_id=offset_id, reverse=False):
                messages.append(message)
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc

        items = [await self._message_to_item(m, entity=entity) for m in messages]
        next_cursor = encode_cursor({"offset_id": messages[-1].id}) if len(messages) >= page_size and messages else None
        return MessagePage(items=items, cursor=next_cursor)

    async def _message_to_item(self, message, *, entity) -> dict:
        normalized = await collector.normalize_message(message, source_username=getattr(entity, "username", None))
        channel_id = str(getattr(entity, "id", None))
        author = None
        if normalized.sender_id is not None:
            author = {"id": normalized.sender_id, "username": normalized.sender_username, "name": normalized.sender_display_name}
        media = [
            {
                "type": m.media_type, "filename": m.filename, "mime_type": m.mime_type, "file_size": m.file_size,
                # Relative - routes_provider.py turns this into an absolute
                # `url` via request.base_url. Fetching it is a separate,
                # explicit on-demand call (see get_message_media) - never
                # done automatically for a whole page of messages.
                "url_path": f"/api/telegram/channels/{channel_id}/messages/{normalized.telegram_message_id}/media",
            }
            for m in normalized.media
        ]
        return {
            "id": str(normalized.telegram_message_id),
            "channel_id": channel_id,
            "text": normalized.text,
            "date": normalized.message_date,
            "url": normalized.source_url,
            "views": normalized.views,
            "forwards": normalized.forwards,
            "replies_count": normalized.reply_count,
            "media": media,
            "author": author,
        }

    # -- MESSAGE -------------------------------------------------------------- #

    async def message(self, *, url: str | None = None, channel_id: str | None = None, message_id: str | int | None = None) -> dict | None:
        await self._require_session()
        if url:
            parsed = _parse_message_url(url)
            if parsed is None:
                raise TelegramSearchError(
                    code="INVALID_REQUEST",
                    message="url must be a https://t.me/<username>/<message_id> link",
                    http_status=422, retryable=False,
                )
            channel_id, message_id = parsed
        if not channel_id or message_id is None:
            raise TelegramSearchError(
                code="INVALID_REQUEST", message="either url, or both channel_id and message_id, are required",
                http_status=422, retryable=False,
            )

        entity = await self._resolve(str(channel_id))
        try:
            found = await self.client.get_messages(entity, ids=int(message_id))
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc
        if found is None:
            return None
        return await self._message_to_item(found, entity=entity)

    # -- MESSAGE_REPLIES ------------------------------------------------------- #

    async def message_replies(self, *, channel_id: str, message_id: str | int, limit: int | None = None, cursor: str | None = None) -> MessagePage:
        await self._require_session()
        entity = await self._resolve(str(channel_id))
        page_size = clamp_limit(limit)
        offset_id = decode_cursor(cursor).get("offset_id", 0)

        try:
            replies = []
            async for message in self.client.iter_messages(
                entity, reply_to=int(message_id), limit=page_size, offset_id=offset_id, reverse=False
            ):
                replies.append(message)
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc

        items = []
        for message in replies:
            item = await self._message_to_item(message, entity=entity)
            items.append({"id": item["id"], "text": item["text"], "date": item["date"], "url": item["url"], "author": item["author"]})
        next_cursor = encode_cursor({"offset_id": replies[-1].id}) if len(replies) >= page_size and replies else None
        return MessagePage(items=items, cursor=next_cursor)

    # -- message media (photo/video/document attached to a message) --------- #

    async def get_message_media(self, *, channel_id: str, message_id: str | int) -> tuple[bytes, str] | None:
        """The real bytes of whatever's attached to one message - the
        per-message counterpart to get_channel_photo. Returns None when the
        message doesn't exist or carries no media at all (not an error).
        Caches to disk by (channel, message) so a repeat request costs no
        further Telegram RPC. Never called automatically for a whole page of
        CHANNEL_MESSAGES/SEARCH_MESSAGES results - each is one explicit,
        on-demand fetch, same account-safety rule as avatars.py/photos.
        """
        await self._require_session()
        entity = await self._resolve(str(channel_id))

        cached = self._find_cached_media(str(entity.id), str(message_id))
        if cached is not None:
            return cached

        try:
            message = await self.client.get_messages(entity, ids=int(message_id))
        except Exception as exc:  # noqa: BLE001
            raise translate_telegram_error(exc) from exc
        if message is None:
            return None

        media_type = collector.classify_media(message)
        if media_type is None:
            return None

        file_meta = getattr(message, "file", None)
        file_size = getattr(file_meta, "size", None)
        if file_size is not None and file_size > self.settings.message_media_max_size_bytes:
            raise TelegramSearchError(
                code="MEDIA_TOO_LARGE",
                message=f"media is {file_size} bytes, over this service's {self.settings.message_media_max_size_bytes}-byte limit",
                http_status=413, retryable=False,
            )

        try:
            content = await media_files.download_message_media(self.client, message)
        except media_files.MediaDownloadError as exc:
            if exc.session_invalid:
                self.client_manager.mark_session_invalid(exc.reason)
                raise TelegramSearchError(
                    code="TELEGRAM_SESSION_INVALID", message=exc.reason, http_status=401, retryable=False,
                ) from exc
            raise TelegramSearchError(code="MEDIA_DOWNLOAD_FAILED", message=exc.reason, http_status=502, retryable=True) from exc

        if content is None:
            return None

        content_type = getattr(file_meta, "mime_type", None) or media_files.DEFAULT_CONTENT_TYPES.get(media_type, "application/octet-stream")
        self._write_media_cache(str(entity.id), str(message_id), content, content_type)
        return content, content_type

    def _media_cache_stem(self, channel_id: str, message_id: str) -> str:
        return f"{channel_id}_{message_id}"

    def _find_cached_media(self, channel_id: str, message_id: str) -> tuple[bytes, str] | None:
        stem = self._media_cache_stem(channel_id, message_id)
        cache_dir = Path(self.settings.message_media_cache_path)
        if not cache_dir.is_dir():
            return None
        for path in cache_dir.glob(f"{stem}.*"):
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return path.read_bytes(), content_type
        return None

    def _write_media_cache(self, channel_id: str, message_id: str, content: bytes, content_type: str) -> None:
        cache_dir = Path(self.settings.message_media_cache_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        extension = mimetypes.guess_extension(content_type) or ".bin"
        stem = self._media_cache_stem(channel_id, message_id)
        (cache_dir / f"{stem}{extension}").write_bytes(content)

    # -- RESOLVE_LINK ----------------------------------------------------------- #

    async def resolve_link(self, url: str) -> dict:
        await self._require_session()
        parsed = _parse_message_url(url)
        if parsed is not None:
            channel_username, message_id = parsed
            channel = await self.channel_info(username=channel_username)
            msg = await self.message(channel_id=channel_username, message_id=message_id)
            return {"kind": "message", "channel": channel, "message": msg}

        classification = classify_telegram_url(url)
        if not classification.is_telegram:
            raise TelegramSearchError(code="NOT_TELEGRAM_URL", message=classification.reason or "not a Telegram URL", http_status=422, retryable=False)
        identifier: NormalizedIdentifier | None = classification.identifier
        if identifier is None:
            raise TelegramSearchError(code="INVALID_REQUEST", message=classification.reason or "unparseable Telegram URL", http_status=422, retryable=False)

        if identifier.kind == IdentifierKind.INVITE_HASH:
            return {"kind": "invite", "invite": identifier.value}

        channel = await self.channel_info(username=identifier.value)
        return {"kind": "channel", "channel": channel}

    # -- CHECK_ACCESS ------------------------------------------------------------ #

    async def check_access(self, *, username: str | None = None, url: str | None = None, channel_id: str | None = None) -> dict:
        await self._require_session()
        identifier = self._pick_identifier(username=username, url=url, channel_id=channel_id)
        normalized = normalize_identifier(identifier)
        result = await access_manager.check_access(self.client, normalized)

        if result.session_invalid:
            self.client_manager.mark_session_invalid(result.reason or "session invalidated during access check")
            raise TelegramSearchError(code="TELEGRAM_SESSION_INVALID", message=result.reason, http_status=401, retryable=False)
        if result.status.value == "ERROR":
            retryable = "FloodWaitError" in (result.reason or "")
            raise TelegramSearchError(
                code="ACCESS_CHECK_FAILED", message=result.reason or "could not check access", http_status=502, retryable=retryable,
            )

        state = _ACCESS_STATE_MAP.get(result.status.value, "unknown")
        return {
            "accessible": result.status.value in ("PUBLIC_ACCESSIBLE", "ACCESSIBLE"),
            "state": state,
            "detail": result.reason or state,
        }

    # -- JOIN_INVITE --------------------------------------------------------------- #

    async def join_invite(self, invite: str) -> dict:
        await self._require_session()
        normalized = normalize_identifier(invite)
        if normalized.kind != IdentifierKind.INVITE_HASH:
            raise TelegramSearchError(code="INVALID_REQUEST", message="not a Telegram invite link", http_status=422, retryable=False)

        result = await join_request_manager.submit_join_request(self.client, username=None, invite_hash=normalized.value)

        if result.session_invalid:
            self.client_manager.mark_session_invalid(result.reason or "session invalidated during invite join")
            raise TelegramSearchError(code="TELEGRAM_SESSION_INVALID", message=result.reason, http_status=401, retryable=False)
        if result.status.value == "ERROR":
            reason = result.reason or ""
            http_status = 410 if "Expired" in reason else 422 if "Invalid" in reason else 429 if "FloodWait" in reason else 502
            raise TelegramSearchError(code="JOIN_FAILED", message=reason or "could not join via invite", http_status=http_status, retryable=http_status == 429)

        joined = result.status.value == "JOINED"
        channel = None
        try:
            probe = await access_manager.check_access(self.client, normalized)
            if probe.resolved_entity is not None:
                channel = await self._entity_to_channel_info(probe.resolved_entity)
        except Exception:  # noqa: BLE001 - the join itself already succeeded/pended; a preview failure must not mask that
            channel = None

        return {"joined": joined, "pending": result.status.value == "JOIN_REQUEST_PENDING", "channel": channel}
