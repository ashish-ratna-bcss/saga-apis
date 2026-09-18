"""Incremental message collection from a single accessible source.

Pure Telegram <-> normalized-dataclass translation - no DB access here (that
is message_service's job), so this stays unit-testable against a fake client
and reusable regardless of which persistence backend is behind the repos.

Captures whatever metadata Telegram actually exposes to this account for
each message; anything Telegram doesn't expose is left as None rather than
guessed (per the project's "no field is assumed to always be present" rule).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from telethon.errors import ChannelPrivateError, FloodWaitError, RPCError

from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE


@dataclass
class NormalizedMedia:
    media_type: str
    telegram_media_id: str | None
    filename: str | None
    mime_type: str | None
    file_size: int | None
    local_path: str | None = None
    sha256: str | None = None
    downloaded: bool = False
    downloaded_at: datetime | None = None


@dataclass
class NormalizedMessage:
    telegram_message_id: int
    sender_id: str | None
    sender_username: str | None
    sender_display_name: str | None
    message_date: datetime | None
    edit_date: datetime | None
    text: str | None
    views: int | None
    forwards: int | None
    reply_count: int | None
    grouped_id: str | None
    media_type: str | None
    source_url: str | None
    raw_data_hash: str
    raw_json: dict = field(default_factory=dict)
    media: list[NormalizedMedia] = field(default_factory=list)
    # Transient handle to the live Telethon message - never persisted, only
    # used (optionally, same collection cycle) for on-demand media download.
    telethon_message: object = field(default=None, repr=False, compare=False)


def classify_media(message) -> str | None:
    if message.photo:
        return "photo"
    if message.voice:
        return "voice"
    if message.video:
        return "video"
    if message.audio:
        return "audio"
    if message.sticker:
        return "sticker"
    if message.gif:
        return "other"
    if message.document:
        return "document"
    return None


def extract_media_metadata(message) -> list[NormalizedMedia]:
    media_type = classify_media(message)
    if media_type is None:
        return []
    telegram_media_id = None
    for attr in ("photo", "document"):
        obj = getattr(message, attr, None)
        if obj is not None:
            telegram_media_id = str(getattr(obj, "id", None))
            break
    file_ = getattr(message, "file", None)
    filename = getattr(file_, "name", None) if file_ else None
    mime_type = getattr(file_, "mime_type", None) if file_ else None
    file_size = getattr(file_, "size", None) if file_ else None
    return [
        NormalizedMedia(
            media_type=media_type,
            telegram_media_id=telegram_media_id,
            filename=filename,
            mime_type=mime_type,
            file_size=file_size,
        )
    ]


def hash_raw_message(raw: dict) -> str:
    payload = json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_message_url(username: str | None, message_id: int) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return None


async def normalize_message(message, *, source_username: str | None, resolve_sender: bool = True) -> NormalizedMessage:
    sender_username = None
    sender_display_name = None
    if resolve_sender:
        try:
            sender = await message.get_sender()
            if sender is not None:
                sender_username = getattr(sender, "username", None)
                first = getattr(sender, "first_name", None)
                last = getattr(sender, "last_name", None)
                sender_display_name = " ".join(p for p in (first, last) if p) or getattr(sender, "title", None)
        except RPCError:
            pass

    reply_count = None
    if getattr(message, "replies", None) is not None:
        reply_count = getattr(message.replies, "replies", None)

    raw = message.to_dict()
    return NormalizedMessage(
        telegram_message_id=message.id,
        sender_id=str(message.sender_id) if message.sender_id is not None else None,
        sender_username=sender_username,
        sender_display_name=sender_display_name,
        message_date=message.date.astimezone(timezone.utc) if message.date else None,
        edit_date=message.edit_date.astimezone(timezone.utc) if message.edit_date else None,
        text=message.message or None,
        views=getattr(message, "views", None),
        forwards=getattr(message, "forwards", None),
        reply_count=reply_count,
        grouped_id=str(message.grouped_id) if message.grouped_id else None,
        media_type=classify_media(message),
        source_url=build_message_url(source_username, message.id),
        raw_data_hash=hash_raw_message(raw),
        raw_json=raw,
        media=extract_media_metadata(message),
        telethon_message=message,
    )


@dataclass
class CollectionOutcome:
    messages: list[NormalizedMessage] = field(default_factory=list)
    error: str | None = None
    flood_wait_seconds: int | None = None
    session_invalid: bool = False


async def collect_new_messages(client, entity, *, min_id: int, limit: int, source_username: str | None) -> CollectionOutcome:
    """Fetches messages newer than `min_id`, oldest-first, up to `limit`.

    Stops and reports whatever it already fetched on error rather than
    raising, so a partial batch that made it this far can still be persisted
    by the caller (checkpoint only ever advances past what was persisted).
    """
    outcome = CollectionOutcome()
    try:
        async for message in client.iter_messages(entity, min_id=min_id, limit=limit, reverse=True):
            normalized = await normalize_message(message, source_username=source_username)
            outcome.messages.append(normalized)
    except FloodWaitError as exc:
        outcome.error = f"FloodWaitError - wait {exc.seconds}s before retrying"
        outcome.flood_wait_seconds = exc.seconds
    except ChannelPrivateError:
        outcome.error = "access was revoked (ChannelPrivateError) during collection"
    except SESSION_INVALID_ERRORS as exc:
        outcome.error = f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})"
        outcome.session_invalid = True
    except RPCError as exc:
        outcome.error = f"{exc.__class__.__name__}: {exc}"
    return outcome


async def collect_historical_messages(
    client, entity, *, from_date, to_date=None, limit: int, source_username: str | None, before_message_id: int | None = None
) -> CollectionOutcome:
    """Walks *backward* into a source's history, newest-to-oldest, stopping
    at `from_date` or `limit` - the opposite direction from
    `collect_new_messages`. `before_message_id`, when given (resuming an
    interrupted backfill job), continues strictly older than that id so a
    restarted job never re-walks - let alone re-persists, that is also
    guarded by the DB unique constraint - ground it has already covered.
    """
    outcome = CollectionOutcome()
    kwargs: dict = {"limit": limit, "reverse": False}
    if before_message_id:
        kwargs["offset_id"] = before_message_id
    elif to_date:
        kwargs["offset_date"] = to_date
    try:
        async for message in client.iter_messages(entity, **kwargs):
            if from_date and message.date and message.date.astimezone(timezone.utc) < from_date:
                break
            normalized = await normalize_message(message, source_username=source_username)
            outcome.messages.append(normalized)
    except FloodWaitError as exc:
        outcome.error = f"FloodWaitError - wait {exc.seconds}s before retrying"
        outcome.flood_wait_seconds = exc.seconds
    except ChannelPrivateError:
        outcome.error = "access was revoked (ChannelPrivateError) during backfill"
    except SESSION_INVALID_ERRORS as exc:
        outcome.error = f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})"
        outcome.session_invalid = True
    except RPCError as exc:
        outcome.error = f"{exc.__class__.__name__}: {exc}"
    return outcome


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def download_media(
    client,
    message,
    media_meta: NormalizedMedia,
    *,
    storage_dir: str,
    max_size_bytes: int,
    allowed_types: list[str],
) -> NormalizedMedia:
    """Downloads one media item if policy allows; otherwise returns it unchanged.

    Never raises - a failed/skipped download just leaves the metadata-only
    record in place, which is always stored regardless (per spec section 13).
    """
    if media_meta.media_type not in allowed_types:
        return media_meta
    if media_meta.file_size is not None and media_meta.file_size > max_size_bytes:
        return media_meta

    dest_dir = Path(storage_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{message.chat_id}_{message.id}"

    try:
        saved_path = await client.download_media(message, file=str(dest_path))
    except (FloodWaitError, RPCError):
        return media_meta

    if not saved_path:
        return media_meta

    media_meta.local_path = str(saved_path)
    media_meta.sha256 = _sha256_file(saved_path)
    media_meta.downloaded = True
    media_meta.downloaded_at = datetime.now(timezone.utc)
    return media_meta
