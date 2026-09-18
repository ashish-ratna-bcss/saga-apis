"""Identifier normalization.

Recognizes the identifier shapes the spec requires: @username, t.me URLs
(including invite-hash links), bare usernames, and numeric Telegram IDs.
Normalization is purely string-level and never contacts Telegram - actual
resolution/access checking happens in access_manager.py.

Moved here unchanged from the original `app/telegram/discovery.py` module
when discovery grew from a single file into a package (global_search,
channel_search, channel_discovery, pagination) - behavior is identical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class IdentifierKind(str, Enum):
    USERNAME = "username"
    INVITE_HASH = "invite_hash"
    NUMERIC_ID = "numeric_id"


@dataclass(frozen=True)
class NormalizedIdentifier:
    kind: IdentifierKind
    value: str
    normalized: str
    original: str


_TME_PREFIX_RE = re.compile(r"^(https?://)?(www\.)?t\.me/", re.IGNORECASE)


def normalize_identifier(raw: str) -> NormalizedIdentifier:
    """Normalizes a user-supplied source identifier into a canonical form.

    Examples:
        "@ExampleChannel"            -> username "exampleChannel" (case kept, dedup key lowercased)
        "https://t.me/examplechan"   -> username "examplechan"
        "t.me/+AbCdEfGhIjK"          -> invite_hash "AbCdEfGhIjK"
        "https://t.me/joinchat/xyz"  -> invite_hash "xyz"
        "-1001234567890"             -> numeric_id "-1001234567890"
    """
    if raw is None:
        raise ValueError("identifier is required")
    text = raw.strip()
    if not text:
        raise ValueError("identifier is required")

    text = _TME_PREFIX_RE.sub("", text)
    text = text.rstrip("/")

    if text.startswith("joinchat/"):
        invite_hash = text[len("joinchat/") :]
        return NormalizedIdentifier(IdentifierKind.INVITE_HASH, invite_hash, f"https://t.me/+{invite_hash}", raw)
    if text.startswith("+"):
        invite_hash = text[1:]
        return NormalizedIdentifier(IdentifierKind.INVITE_HASH, invite_hash, f"https://t.me/+{invite_hash}", raw)

    if text.startswith("@"):
        text = text[1:]

    if re.fullmatch(r"-?\d+", text):
        return NormalizedIdentifier(IdentifierKind.NUMERIC_ID, text, text, raw)

    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", text):
        raise ValueError(f"'{raw}' is not a recognizable Telegram identifier (@username, t.me link, or numeric ID)")

    return NormalizedIdentifier(IdentifierKind.USERNAME, text, f"@{text.lower()}", raw)
