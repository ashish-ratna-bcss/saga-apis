"""Translate raw Reddit "Thing" JSON into SOC Eye's stable, platform-agnostic shapes.

This is the only module that knows Reddit's on-the-wire field names
(``selftext``, ``created_utc``, ``author_fullname``, ``subreddit_type``, ...). Every
other module -- routes, schemas, tests -- works only with the normalized dicts these
functions return, so a future Reddit API change is a one-file fix.

Reddit "fullnames" are ``<kind prefix>_<base36 id>`` (``t1_`` comment, ``t2_`` user,
``t3_`` post, ``t5_`` subreddit). Post/comment ids in this service's JSON contract are
bare (no prefix, matching the task's examples); subreddit ids keep Reddit's own
fullname since that's what the spec's example shows (``"id": "t5_xxxxx"``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def to_iso8601(created_utc: float | int | None) -> str | None:
    if created_utc is None:
        return None
    return datetime.fromtimestamp(float(created_utc), tz=UTC).isoformat().replace("+00:00", "Z")


def strip_prefix(fullname: str | None) -> str | None:
    """``"t3_abc123"`` -> ``"abc123"``. Passes through an already-bare id unchanged."""

    if not fullname:
        return None
    if "_" in fullname[:3]:
        return fullname.split("_", 1)[1]
    return fullname


def normalize_author(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the ``{id, username}`` author sub-object from a post/comment Thing."""

    username = raw.get("author")
    if not username:
        return None
    if username in ("[deleted]", "[removed]"):
        return {"id": None, "username": username}
    author_id = raw.get("author_fullname")
    return {"id": author_id, "username": username}


def extract_media(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort extraction of image/video/gallery media attached to a post.

    Reddit does not expose a single unified "media" field -- it's spread across
    ``is_video``/``media.reddit_video``, ``post_hint``/``url_overridden_by_dest``, and
    ``is_gallery``/``media_metadata`` depending on post type. Link posts to an external
    (non-media) site are intentionally left out of this list.
    """

    media: list[dict[str, Any]] = []

    if raw.get("is_video"):
        reddit_video = (raw.get("media") or {}).get("reddit_video") or {}
        fallback_url = reddit_video.get("fallback_url")
        if fallback_url:
            media.append(
                {
                    "type": "video",
                    "url": fallback_url.replace("&amp;", "&"),
                    "width": reddit_video.get("width"),
                    "height": reddit_video.get("height"),
                    "duration": reddit_video.get("duration"),
                }
            )
        return media

    if raw.get("is_gallery") and isinstance(raw.get("media_metadata"), dict):
        gallery_order = [
            item.get("media_id")
            for item in (raw.get("gallery_data") or {}).get("items", [])
            if item.get("media_id")
        ]
        metadata: dict[str, Any] = raw["media_metadata"]
        ids = gallery_order or list(metadata.keys())
        for media_id in ids:
            item = metadata.get(media_id) or {}
            source = item.get("s") or {}
            url = source.get("u") or source.get("gif") or source.get("mp4")
            if url:
                media.append(
                    {
                        "type": "image" if item.get("e") == "Image" else "gallery_item",
                        "url": url.replace("&amp;", "&"),
                        "width": source.get("x"),
                        "height": source.get("y"),
                    }
                )
        return media

    post_hint = raw.get("post_hint")
    if post_hint == "image":
        url = raw.get("url_overridden_by_dest") or raw.get("url")
        if url:
            media.append({"type": "image", "url": url, "width": None, "height": None})
    elif post_hint == "link":
        preview = (raw.get("preview") or {}).get("images") or []
        if preview:
            source = preview[0].get("source") or {}
            url = source.get("url")
            if url:
                media.append(
                    {
                        "type": "link_preview",
                        "url": url.replace("&amp;", "&"),
                        "width": source.get("width"),
                        "height": source.get("height"),
                    }
                )

    return media


def normalize_post(raw: dict[str, Any]) -> dict[str, Any]:
    """A ``t3`` Thing's ``data`` -> the SOC Eye Post shape."""

    permalink = raw.get("permalink")
    url = f"https://www.reddit.com{permalink}" if permalink else raw.get("url")
    return {
        "id": raw.get("id"),
        "subreddit": raw.get("subreddit"),
        "title": raw.get("title") or "",
        "text": raw.get("selftext") or "",
        "author": normalize_author(raw),
        "created_at": to_iso8601(raw.get("created_utc")),
        "url": url,
        "score": raw.get("score", 0),
        "upvote_ratio": raw.get("upvote_ratio"),
        "num_comments": raw.get("num_comments", 0),
        "media": extract_media(raw),
    }


def normalize_comment(raw: dict[str, Any], *, post_id: str | None = None) -> dict[str, Any]:
    """A ``t1`` Thing's ``data`` -> the SOC Eye Comment shape.

    ``parent_id`` is ``None`` when the comment replies directly to the post (Reddit's
    own ``parent_id`` equals its ``link_id`` in that case); otherwise it is the bare id
    of the parent comment, so SOC Eye can reconstruct the reply tree.
    """

    parent_fullname = raw.get("parent_id") or ""
    link_fullname = raw.get("link_id") or ""
    resolved_post_id = post_id or strip_prefix(link_fullname)
    parent_id = None
    if parent_fullname and parent_fullname != link_fullname:
        parent_id = strip_prefix(parent_fullname)

    permalink = raw.get("permalink")
    if permalink:
        url = f"https://www.reddit.com{permalink}"
    elif resolved_post_id and raw.get("id"):
        url = f"https://www.reddit.com/comments/{resolved_post_id}/_/{raw['id']}/"
    else:
        url = None

    return {
        "id": raw.get("id"),
        "post_id": resolved_post_id,
        "parent_id": parent_id,
        "text": raw.get("body") or "",
        "author": normalize_author(raw),
        "created_at": to_iso8601(raw.get("created_utc")),
        "score": raw.get("score", 0),
        "url": url,
    }


def normalize_subreddit(raw: dict[str, Any]) -> dict[str, Any]:
    """A ``t5`` Thing's ``data`` -> the SOC Eye Subreddit shape."""

    display_name = raw.get("display_name") or ""
    subreddit_type = raw.get("subreddit_type")
    url_path = raw.get("url") or (f"/r/{display_name}/" if display_name else None)
    return {
        "id": raw.get("name"),  # Reddit's own fullname, e.g. "t5_2qh1e"
        "name": display_name,
        "display_name": f"r/{display_name}" if display_name else display_name,
        "title": raw.get("title") or "",
        "description": raw.get("public_description") or raw.get("description") or "",
        "subscribers": raw.get("subscribers") or 0,
        "url": f"https://www.reddit.com{url_path}" if url_path else None,
        "public": subreddit_type == "public",
        "over18": bool(raw.get("over18")),
    }


def normalize_user(raw: dict[str, Any]) -> dict[str, Any]:
    """A ``t2`` Thing's ``data`` -> the SOC Eye User shape."""

    user_id = raw.get("id")
    return {
        "id": f"t2_{user_id}" if user_id else None,
        "username": raw.get("name"),
        "created_at": to_iso8601(raw.get("created_utc")),
        "link_karma": raw.get("link_karma", 0),
        "comment_karma": raw.get("comment_karma", 0),
        "is_mod": bool(raw.get("is_mod", False)),
    }


def parse_listing(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """A Reddit ``Listing`` object -> (child "Thing" dicts still wrapped in
    {kind, data}, next ``after`` token or None)."""

    data = (payload or {}).get("data") or {}
    children = data.get("children") or []
    after = data.get("after")
    return children, after
