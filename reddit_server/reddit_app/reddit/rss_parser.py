"""Parse a Reddit RSS/Atom feed into normalized post dicts. Pure functions, no
network -- kept separate from ``rss_client.py`` so parsing can be unit tested
with fixture XML and no HTTP fake.

Reddit's public ``.rss`` endpoints emit **Atom** (``xmlns="http://www.w3.org/2005/Atom"``),
not RSS 2.0, despite the file extension -- a long-standing, stable quirk. This
parser is defensive throughout: a field Reddit's markup doesn't carry (or a shape
that drifts) becomes ``None`` rather than a crash or an invented value. A single
malformed ``<entry>`` is skipped rather than failing the whole feed; only a
response that isn't parseable XML at all raises.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

from reddit_app.core.exceptions import RedditRssParseError
from reddit_app.core.logging import get_logger
from reddit_app.reddit.urls import classify_reddit_url

logger = get_logger(__name__)

_ATOM_NS = "http://www.w3.org/2005/Atom"


def _tag(name: str) -> str:
    return f"{{{_ATOM_NS}}}{name}"


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    stripped = el.text.strip()
    return stripped or None


def _strip_html(raw: str | None) -> str | None:
    """Reddit's ``<content type="html">`` is an HTML snippet (thumbnail table,
    "submitted by", selftext preview). Best-effort plain text, not a full HTML
    parser -- this only needs to feed the keyword matcher and give a human a
    readable preview, not round-trip markup."""

    if not raw:
        return None
    unescaped = html.unescape(raw)
    stripped = re.sub(r"<[^>]+>", " ", unescaped)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    return collapsed or None


def _parse_timestamp(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _entry_link(entry: ET.Element) -> str | None:
    for link in entry.findall(_tag("link")):
        href = link.get("href")
        if href:
            return href
    return None


def _entry_author(entry: ET.Element) -> str | None:
    author_el = entry.find(_tag("author"))
    if author_el is None:
        return None
    name = _text(author_el.find(_tag("name")))
    if not name:
        return None
    return name[3:] if name.startswith("/u/") else name


def _entry_category(entry: ET.Element) -> str | None:
    category = entry.find(_tag("category"))
    if category is None:
        return None
    return category.get("term") or None


def parse_entry(entry: ET.Element) -> dict[str, Any]:
    """One Atom ``<entry>`` -> the RSS Post shape (see ``app/schemas/reddit_rss.py``)."""

    link = _entry_link(entry)
    post_id: str | None = None
    subreddit = _entry_category(entry)
    if link:
        try:
            ref = classify_reddit_url(link)
        except Exception:  # noqa: BLE001 - a link this parser can't classify just means null fields
            ref = None
        if ref is not None and ref.kind == "post":
            post_id = ref.post_id
            subreddit = subreddit or ref.subreddit

    guid = _text(entry.find(_tag("id")))
    if post_id is None and guid and "_" in guid[:3]:
        # Reddit fullname, e.g. "t3_abc123" — strip the kind prefix for a bare id
        # when the link itself didn't parse into a recognizable post URL.
        post_id = guid.split("_", 1)[1]

    content_el = entry.find(_tag("content"))
    raw_content = content_el.text if content_el is not None else None
    updated_at = _parse_timestamp(_text(entry.find(_tag("updated"))))
    published_at = _parse_timestamp(_text(entry.find(_tag("published")))) or updated_at

    return {
        "id": post_id,
        "guid": guid,
        "title": _text(entry.find(_tag("title"))),
        "author": _entry_author(entry),
        "url": link,
        "subreddit": subreddit,
        # Reddit's Atom feed does not expose post flair as a distinct field.
        "flair": None,
        "published_at": published_at,
        "updated_at": updated_at,
        "content": _strip_html(raw_content),
        "source": "reddit",
        "source_type": "rss",
        "matched_keywords": [],
    }


def parse_feed(raw_xml: bytes) -> list[dict[str, Any]]:
    """A full Atom document -> a list of normalized post dicts. Raises
    ``RedditRssParseError`` only when the document itself isn't well-formed XML; a
    successful, empty feed returns ``[]`` (a valid result, not an error -- see the
    RSS integration contract's distinction between "empty" and "failed")."""

    if not raw_xml or not raw_xml.strip():
        return []
    try:
        root = ET.fromstring(raw_xml)  # noqa: S314 - Reddit's own feed, not user-supplied XML
    except ET.ParseError as exc:
        raise RedditRssParseError("Reddit RSS returned malformed XML") from exc

    posts: list[dict[str, Any]] = []
    for entry in root.findall(_tag("entry")):
        try:
            posts.append(parse_entry(entry))
        except Exception:  # noqa: BLE001 - one bad entry must never fail the whole feed
            logger.warning("Skipping an unparseable RSS entry", exc_info=True)
            continue
    return posts
