"""Message-media (photo/video/document/...) retrieval - the on-demand,
per-message counterpart to avatars.py's per-entity photo retrieval.

`download_message_media` issues its own Telegram RPC beyond whatever already
resolved the message - callers must treat it as an explicit, on-demand
action per message, never something run automatically over a list of
search/monitoring results (same account-safety rule avatars.py documents).
"""
from __future__ import annotations

from telethon.errors import FloodWaitError, RPCError

from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE

# Telegram doesn't always hand back a mime_type (e.g. photos never have one -
# they're a fixed set of JPEG sizes, not a document with its own metadata),
# so this is the fallback used when NormalizedMedia.mime_type is None.
DEFAULT_CONTENT_TYPES = {
    "photo": "image/jpeg",
    "video": "video/mp4",
    "voice": "audio/ogg",
    "audio": "audio/mpeg",
    "sticker": "image/webp",
    "other": "video/mp4",  # gif
    "document": "application/octet-stream",
}


class MediaDownloadError(Exception):
    """Carries a caller-safe reason string - never raw credential/session data."""

    def __init__(self, reason: str, *, session_invalid: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.session_invalid = session_invalid


async def download_message_media(client, message) -> bytes | None:
    """Downloads the real media bytes attached to `message`. Returns None
    when the message has no media at all - not an error, just nothing to
    fetch. Raises MediaDownloadError on FloodWait, a session that turns out
    to be invalid, or any other RPC failure."""
    try:
        return await client.download_media(message, file=bytes)
    except SESSION_INVALID_ERRORS as exc:
        raise MediaDownloadError(
            f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True
        ) from exc
    except FloodWaitError as exc:
        raise MediaDownloadError(f"FloodWaitError - wait {exc.seconds}s before retrying") from exc
    except RPCError as exc:
        raise MediaDownloadError(f"{exc.__class__.__name__}: {exc}") from exc
