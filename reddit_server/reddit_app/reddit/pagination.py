"""Bounded, opaque-cursor pagination shared by every Reddit listing endpoint.

Reddit's own listings page via an ``after`` fullname (e.g. ``t3_abc123``) handed back
in ``data.after``. That is already an opaque token, so most endpoints simply forward it
as-is as the SOC Eye ``cursor``. A couple of endpoints (flattened comment trees, which
Reddit does not paginate the way it paginates listings) need extra state alongside the
Reddit token, so every endpoint uses the same ``encode_cursor``/``decode_cursor``
envelope regardless -- callers of the API never need to know which shape is inside.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from reddit_app.core.exceptions import ValidationError

DEFAULT_PAGE_SIZE = 25
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
        raise ValidationError("invalid pagination cursor") from exc
    if not isinstance(state, dict):
        raise ValidationError("invalid pagination cursor")
    return state
