"""Bounded, opaque-cursor pagination shared by every discovery sub-module.

Telegram's own pagination semantics differ per RPC (messages.searchGlobal
and channels.searchPosts page via offset_rate/offset_peer/offset_id;
per-channel messages.search pages via a message-id offset; contacts.search
has no pagination at all - a single call returns everything Telegram is
willing to give). Rather than pretending these are the same thing, each
search module packs whatever state IT needs into an opaque cursor string via
`encode_cursor`/`decode_cursor` - callers of the API never need to know the
shape, and it isn't guaranteed stable across app versions.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def clamp_limit(limit: int | None, *, default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE) -> int:
    """Protects against abusive/unbounded requests - never returns more than
    `maximum` regardless of what the caller asks for, and never less than 1."""
    if limit is None:
        return default
    return max(1, min(limit, maximum))


def encode_cursor(state: dict[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str | None) -> dict[str, Any]:
    if not cursor:
        return {}
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii"))
        state = json.loads(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid pagination cursor") from exc
    if not isinstance(state, dict):
        raise ValueError("invalid pagination cursor")
    return state


T = TypeVar("T")


@dataclass
class PageResult(Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool
    returned: int
