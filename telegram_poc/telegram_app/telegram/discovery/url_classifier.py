"""Classifies whether a URL points at Telegram, before anything is resolved.

Hard rule, non-negotiable: this module NEVER performs an HTTP request or any
other network call. It only inspects the URL string itself (scheme, host,
path) via `urllib.parse`. A URL that is not recognized as Telegram is
classified `is_telegram=False` and must never be fetched to "find out" -
that classification is the final answer, not a hint to investigate further.

Only `t.me` and `telegram.me` (with an optional `www.` prefix) are recognized
as Telegram hosts. Everything else - shorteners, unknown domains, tracking
links - is NOT Telegram, by design; expanding that list is a deliberate,
reviewed change, not something this module infers on its own.

Once a URL is confirmed to be Telegram, identifier extraction reuses
`normalize_identifier` - the exact parser `POST /api/sources` already uses
for a user-typed identifier - rather than a second, divergent implementation
of the same @username / invite-hash / numeric-id rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from telegram_app.telegram.discovery.normalizer import NormalizedIdentifier, normalize_identifier

_TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass(frozen=True)
class TelegramUrlClassification:
    raw_url: str
    is_telegram: bool
    identifier: NormalizedIdentifier | None = None
    reason: str | None = None


def classify_telegram_url(url: str) -> TelegramUrlClassification:
    """Classifies one URL. Never fetches it - only parses the string."""
    text = (url or "").strip()
    if not text:
        return TelegramUrlClassification(raw_url=url, is_telegram=False, reason="empty URL")

    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return TelegramUrlClassification(raw_url=url, is_telegram=False, reason="unparseable URL")

    host = (parts.hostname or "").lower()
    if host not in _TELEGRAM_HOSTS:
        return TelegramUrlClassification(
            raw_url=url, is_telegram=False, reason=f"host '{host or text}' is not a recognized Telegram domain"
        )

    candidate = parts.path.lstrip("/")
    if parts.query:
        candidate = f"{candidate}?{parts.query}" if candidate else f"?{parts.query}"

    try:
        identifier = normalize_identifier(f"https://t.me/{candidate}" if candidate else text)
    except ValueError as exc:
        return TelegramUrlClassification(raw_url=url, is_telegram=True, reason=str(exc))

    return TelegramUrlClassification(raw_url=url, is_telegram=True, identifier=identifier)


def extract_telegram_urls(text: str) -> list[TelegramUrlClassification]:
    """Finds every http(s) URL in free text (e.g. a collected message body)
    and classifies each one. Both Telegram and non-Telegram results are
    returned so a caller can see what was found - non-Telegram entries are
    informational only and must never be fetched or otherwise followed."""
    if not text:
        return []
    return [classify_telegram_url(u) for u in _URL_RE.findall(text)]
