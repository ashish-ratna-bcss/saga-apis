"""Subreddit-name normalization and Reddit URL parsing/classification.

Nothing here talks to the network -- these are pure functions over strings, kept
separate from ``rest_client.py`` so URL/name parsing can be unit tested without any
HTTP fake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from reddit_app.core.exceptions import InvalidRedditUrlError, ValidationError

_REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "np.reddit.com",
    "amp.reddit.com",
    "m.reddit.com",
    "sh.reddit.com",
}
_SHORT_HOSTS = {"redd.it", "www.redd.it"}

_SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_BASE36_ID_RE = r"[A-Za-z0-9]{1,10}"

_COMMENT_RE = re.compile(
    rf"^/r/(?P<sub>[A-Za-z0-9_]{{2,21}})/comments/(?P<post_id>{_BASE36_ID_RE})"
    rf"(?:/(?P<slug>[^/]*)/(?P<comment_id>{_BASE36_ID_RE}))/?$"
)
_POST_RE = re.compile(
    rf"^/r/(?P<sub>[A-Za-z0-9_]{{2,21}})/comments/(?P<post_id>{_BASE36_ID_RE})(?:/[^/]*)?/?$"
)
_SUBREDDIT_RE = re.compile(r"^/r/(?P<sub>[A-Za-z0-9_]{2,21})/?$")
_USER_COMMENT_RE = re.compile(
    rf"^/user/[A-Za-z0-9_-]+/comments/(?P<post_id>{_BASE36_ID_RE})"
    rf"(?:/(?P<slug>[^/]*)/(?P<comment_id>{_BASE36_ID_RE}))?/?$"
)
_SHORTLINK_RE = re.compile(rf"^/(?P<post_id>{_BASE36_ID_RE})/?$")


def normalize_subreddit_name(raw: str) -> str:
    """Accept "india", "r/india", "/r/india/" (any case) and return the bare, canonical
    subreddit name ("india"). Raises ValidationError for anything else."""

    text = (raw or "").strip().strip("/")
    if text.lower().startswith("r/"):
        text = text[2:]
    if not _SUBREDDIT_NAME_RE.match(text):
        raise ValidationError(
            "Invalid subreddit name; expected e.g. 'india' or 'r/india'",
            details={"name": raw},
        )
    return text


def normalize_subreddit_names(raw: str) -> str:
    """Like :func:`normalize_subreddit_name`, but also accepts Reddit's "+"-joined
    combined-subreddit ("multireddit") syntax -- e.g. "india+worldnews+politics" --
    so listing/search endpoints can watch several subreddits in one call. Each
    segment is validated and normalized individually; the result is rejoined with
    "+". A single name normalizes exactly like ``normalize_subreddit_name``."""

    segments = (raw or "").split("+")
    if not segments or any(not segment.strip() for segment in segments):
        raise ValidationError(
            "Invalid subreddit name; expected e.g. 'india' or 'india+worldnews'",
            details={"name": raw},
        )
    return "+".join(normalize_subreddit_name(segment) for segment in segments)


def normalize_username(raw: str) -> str:
    """Accept "someuser", "u/someuser", "/u/someuser/" and return the bare username."""

    text = (raw or "").strip().strip("/")
    if text.lower().startswith("u/"):
        text = text[2:]
    if not text or "/" in text or " " in text:
        raise ValidationError("Invalid username", details={"username": raw})
    return text


Kind = Literal["subreddit", "post", "comment"]


@dataclass
class ResolvedRef:
    kind: Kind
    subreddit: str | None = None
    post_id: str | None = None
    comment_id: str | None = None


def _split_url(url: str) -> tuple[str, str]:
    text = (url or "").strip()
    if not text:
        raise InvalidRedditUrlError("url must not be empty")
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError as exc:
        raise InvalidRedditUrlError("Could not parse url", details={"url": url}) from exc
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    return host, path


def classify_reddit_url(url: str) -> ResolvedRef:
    """Determine whether a Reddit URL points at a subreddit, post, or comment."""

    host, path = _split_url(url)

    if host in _SHORT_HOSTS:
        match = _SHORTLINK_RE.match(path)
        if match:
            return ResolvedRef(kind="post", post_id=match.group("post_id"))
        raise InvalidRedditUrlError("Not a recognizable redd.it short link", details={"url": url})

    if host not in _REDDIT_HOSTS:
        raise InvalidRedditUrlError(
            "Not a reddit.com URL", details={"url": url, "host": host}
        )

    match = _COMMENT_RE.match(path)
    if match:
        return ResolvedRef(
            kind="comment",
            subreddit=match.group("sub"),
            post_id=match.group("post_id"),
            comment_id=match.group("comment_id"),
        )

    match = _USER_COMMENT_RE.match(path)
    if match and match.group("comment_id"):
        return ResolvedRef(kind="comment", post_id=match.group("post_id"), comment_id=match.group("comment_id"))

    match = _POST_RE.match(path)
    if match:
        return ResolvedRef(kind="post", subreddit=match.group("sub"), post_id=match.group("post_id"))

    match = _USER_COMMENT_RE.match(path)
    if match:
        return ResolvedRef(kind="post", post_id=match.group("post_id"))

    match = _SUBREDDIT_RE.match(path)
    if match:
        return ResolvedRef(kind="subreddit", subreddit=match.group("sub"))

    raise InvalidRedditUrlError(
        "URL does not match a subreddit, post, or comment link", details={"url": url}
    )
