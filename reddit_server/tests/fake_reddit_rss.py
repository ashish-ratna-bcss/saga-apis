"""An in-memory stand-in for Reddit's public RSS/Atom host.

Every test in this suite runs against this, never real network. Reddit's
``.rss`` endpoints emit Atom XML (see ``app/reddit/rss_parser.py``), so the
fixtures here build real (if minimal) Atom documents rather than RSS 2.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

ATOM_NS = "http://www.w3.org/2005/Atom"


def atom_entry(
    *,
    post_id: str = "abc123",
    subreddit: str = "india",
    title: str = "Example title",
    author: str | None = "someuser",
    content: str = "submitted by /u/someuser &lt;br/&gt; [link]",
    published: str | None = "2026-09-10T08:20:00+00:00",
    updated: str | None = None,
    guid: str | None = None,
    link: str | None = None,
    include_category: bool = True,
) -> str:
    """One ``<entry>``. Every field is overridable so tests can build "missing
    author", "malformed timestamp", "duplicate guid" etc fixtures directly."""

    updated = updated if updated is not None else published
    guid = guid if guid is not None else f"t3_{post_id}"
    link = (
        link
        if link is not None
        else f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/example/"
    )

    author_xml = f"<author><name>/u/{author}</name></author>" if author is not None else ""
    category_xml = (
        f'<category term="{subreddit}" label="r/{subreddit}"/>' if include_category else ""
    )
    published_xml = f"<published>{published}</published>" if published is not None else ""
    updated_xml = f"<updated>{updated}</updated>" if updated is not None else ""
    link_xml = f'<link href="{link}" />' if link else ""
    guid_xml = f"<id>{guid}</id>" if guid else ""

    return (
        "<entry>"
        f"{author_xml}{category_xml}"
        f'<content type="html">{content}</content>'
        f"{guid_xml}{link_xml}{updated_xml}{published_xml}"
        f"<title>{title}</title>"
        "</entry>"
    )


def atom_feed(entries: list[str]) -> bytes:
    body = "".join(entries)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="{ATOM_NS}">'
        f"<title>search results</title>{body}</feed>"
    ).encode()


@dataclass
class FakeRedditRss:
    """Routable fake RSS host. ``default_feed`` is served for any request that
    doesn't match a scripted response; tests queue scripted responses (by path
    suffix) to simulate specific HTTP failures."""

    default_feed: bytes = field(default_factory=lambda: atom_feed([atom_entry()]))
    scripted: dict[str, list[httpx.Response]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = dict(request.url.params)
        self.calls.append((path, query))

        for suffix, queue in self.scripted.items():
            if path.endswith(suffix) and queue:
                return queue.pop(0)

        return httpx.Response(
            200, content=self.default_feed, headers={"Content-Type": "application/atom+xml"}
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls_to(self, suffix: str) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if call[0].endswith(suffix)]
