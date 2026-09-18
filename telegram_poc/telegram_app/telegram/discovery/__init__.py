"""Telegram discovery layer: search, and channel discovery - independent
from the monitoring scheduler.

`normalize_identifier` / `IdentifierKind` / `NormalizedIdentifier` are
re-exported here unchanged so every existing `from telegram_app.telegram.discovery
import normalize_identifier` import elsewhere in the codebase keeps working
now that this module grew from a single file into a package.
"""
from __future__ import annotations

from telegram_app.telegram.discovery.channel_discovery import (
    ChannelDiscoveryOutcome,
    TelegramChannelDiscoveryResult,
    discover_channels,
)
from telegram_app.telegram.discovery.channel_search import ChannelSearchOutcome, search_channel
from telegram_app.telegram.discovery.global_search import (
    GlobalSearchOutcome,
    SearchChannelRef,
    SearchMediaRef,
    SearchSenderRef,
    TelegramSearchResultItem,
    search_global,
)
from telegram_app.telegram.discovery.normalizer import IdentifierKind, NormalizedIdentifier, normalize_identifier
from telegram_app.telegram.discovery.pagination import PageResult, clamp_limit, decode_cursor, encode_cursor
from telegram_app.telegram.discovery.url_classifier import (
    TelegramUrlClassification,
    classify_telegram_url,
    extract_telegram_urls,
)

__all__ = [
    "IdentifierKind",
    "NormalizedIdentifier",
    "normalize_identifier",
    "PageResult",
    "clamp_limit",
    "decode_cursor",
    "encode_cursor",
    "search_global",
    "GlobalSearchOutcome",
    "TelegramSearchResultItem",
    "SearchChannelRef",
    "SearchSenderRef",
    "SearchMediaRef",
    "search_channel",
    "ChannelSearchOutcome",
    "discover_channels",
    "ChannelDiscoveryOutcome",
    "TelegramChannelDiscoveryResult",
    "TelegramUrlClassification",
    "classify_telegram_url",
    "extract_telegram_urls",
]
