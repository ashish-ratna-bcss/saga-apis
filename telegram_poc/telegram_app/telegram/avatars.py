"""Channel/group/user profile-photo retrieval.

Two distinct costs, kept deliberately separate - the same account-safety
split every other module in this package makes (see rate_policy.py):

- `stripped_thumb_data_uri` decodes the tiny, blurred preview Telegram embeds
  directly in an entity's own `photo.stripped_thumb` field. That field is
  already present in any response that returned the entity at all
  (contacts.search, get_entity, ...), so this costs NO additional Telegram
  RPC - it is pure local decoding of bytes already in hand.

- `download_avatar` fetches the real, full-resolution photo via Telethon's
  `download_profile_photo`, which issues its own RPC (upload.getFile) beyond
  whatever already resolved the entity. Callers must treat this as an
  explicit, on-demand action per entity - never something run automatically
  over a list of search/discovery results.
"""
from __future__ import annotations

import base64

from telethon import utils
from telethon.errors import FloodWaitError, RPCError

from telegram_app.telegram.client import SESSION_INVALID_ERRORS, SESSION_INVALID_MESSAGE


def stripped_thumb_data_uri(entity) -> str | None:
    """Free - issues no Telegram call. Returns a `data:image/jpeg;base64,...`
    URI for the low-resolution preview embedded in `entity.photo`, or None
    when the entity has no photo, or Telegram didn't include a stripped
    thumbnail for it (e.g. an animated/video profile photo has none)."""
    photo = getattr(entity, "photo", None)
    stripped = getattr(photo, "stripped_thumb", None)
    if not stripped:
        return None
    try:
        jpg = utils.stripped_photo_to_jpg(stripped)
    except Exception:  # noqa: BLE001 - a malformed thumbnail must never break the caller
        return None
    if not jpg:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(jpg).decode("ascii")


class AvatarDownloadError(Exception):
    """Carries a caller-safe reason string - never raw credential/session data."""

    def __init__(self, reason: str, *, session_invalid: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.session_invalid = session_invalid


async def download_avatar(client, entity) -> bytes | None:
    """Downloads the real, full-resolution profile photo for `entity`.

    Returns None when the entity has no photo at all - not an error, just
    nothing to fetch. Raises AvatarDownloadError, translated the same way
    every other Telegram-facing call in this package is, on FloodWait, a
    session that turns out to be invalid, or any other RPC failure.
    """
    try:
        return await client.download_profile_photo(entity, file=bytes, download_big=True)
    except SESSION_INVALID_ERRORS as exc:
        raise AvatarDownloadError(
            f"{SESSION_INVALID_MESSAGE} ({exc.__class__.__name__})", session_invalid=True
        ) from exc
    except FloodWaitError as exc:
        raise AvatarDownloadError(f"FloodWaitError - wait {exc.seconds}s before retrying") from exc
    except RPCError as exc:
        raise AvatarDownloadError(f"{exc.__class__.__name__}: {exc}") from exc
