"""An in-memory stand-in for the Reddit OAuth2 + REST API.

Every unit and integration test runs against this, so the suite never needs real
Reddit credentials or network access. Two transports are exposed: one for the token
endpoint (``www.reddit.com``) and one for the authenticated API (``oauth.reddit.com``)
-- mirroring how :class:`app.reddit.rest_client.RedditRestClient` talks to two hosts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

ACCESS_TOKEN = "fake-access-token-000000000000000000"
CREATED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC).timestamp()


def _json(status_code: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    merged = {
        "Content-Type": "application/json",
        "X-Ratelimit-Remaining": "599.0",
        "X-Ratelimit-Used": "1",
        "X-Ratelimit-Reset": "600",
        **(headers or {}),
    }
    return httpx.Response(status_code, content=json.dumps(payload).encode(), headers=merged)


def rate_limited(retry_after: float = 0.01) -> httpx.Response:
    return _json(429, {"message": "Too Many Requests"}, headers={"Retry-After": str(retry_after)})


def server_error() -> httpx.Response:
    return _json(500, {"message": "internal server error"})


def listing(children: list[dict[str, Any]], *, after: str | None = None) -> dict[str, Any]:
    return {"kind": "Listing", "data": {"children": children, "after": after, "before": None}}


def post_thing(
    post_id: str,
    *,
    subreddit: str = "india",
    title: str = "Example title",
    selftext: str = "Post body",
    author: str = "user1",
    author_fullname: str = "t2_user1",
    score: int = 123,
    upvote_ratio: float = 0.95,
    num_comments: int = 2,
    created_utc: float = CREATED,
) -> dict[str, Any]:
    permalink = f"/r/{subreddit}/comments/{post_id}/example/"
    return {
        "kind": "t3",
        "data": {
            "id": post_id,
            "subreddit": subreddit,
            "title": title,
            "selftext": selftext,
            "author": author,
            "author_fullname": author_fullname,
            "created_utc": created_utc,
            "permalink": permalink,
            "url": f"https://www.reddit.com{permalink}",
            "score": score,
            "upvote_ratio": upvote_ratio,
            "num_comments": num_comments,
            "is_video": False,
            "is_gallery": False,
            "post_hint": None,
        },
    }


def comment_thing(
    comment_id: str,
    *,
    link_id: str,
    parent_id: str | None = None,
    subreddit: str = "india",
    body: str = "Comment text",
    author: str = "user2",
    author_fullname: str = "t2_user2",
    score: int = 10,
    created_utc: float = CREATED,
    replies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_parent = parent_id or link_id
    permalink = f"/r/{subreddit}/comments/{link_id[3:]}/example/{comment_id}/"
    return {
        "kind": "t1",
        "data": {
            "id": comment_id,
            "link_id": link_id,
            "parent_id": resolved_parent,
            "subreddit": subreddit,
            "body": body,
            "author": author,
            "author_fullname": author_fullname,
            "score": score,
            "created_utc": created_utc,
            "permalink": permalink,
            "replies": replies or "",
        },
    }


def more_thing(children: list[str], *, parent_id: str) -> dict[str, Any]:
    return {"kind": "more", "data": {"id": "_", "parent_id": parent_id, "children": children}}


def subreddit_thing(
    name: str,
    *,
    title: str = "",
    subscribers: int = 1000000,
    subreddit_type: str = "public",
    over18: bool = False,
    quarantine: bool = False,
    public_description: str = "...",
) -> dict[str, Any]:
    return {
        "kind": "t5",
        "data": {
            "name": f"t5_{name}",
            "display_name": name,
            "title": title or name.title(),
            "public_description": public_description,
            "subscribers": subscribers,
            "url": f"/r/{name}/",
            "subreddit_type": subreddit_type,
            "over18": over18,
            "quarantine": quarantine,
        },
    }


def user_thing(
    username: str,
    *,
    user_id: str = "8xwlg",
    created_utc: float = CREATED,
    link_karma: int = 1234,
    comment_karma: int = 5678,
    is_mod: bool = False,
) -> dict[str, Any]:
    return {
        "kind": "t2",
        "data": {
            "id": user_id,
            "name": username,
            "created_utc": created_utc,
            "link_karma": link_karma,
            "comment_karma": comment_karma,
            "is_mod": is_mod,
        },
    }


@dataclass
class FakeReddit:
    """Routable fake Reddit API with configurable failures."""

    reject_auth: bool = False
    access_token: str = ACCESS_TOKEN

    subreddits: dict[str, dict[str, Any]] = field(default_factory=dict)
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)
    comment_trees: dict[str, dict[str, Any]] = field(default_factory=dict)  # post_id -> comments listing
    more_children: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # ",".join(ids) -> things
    users: dict[str, dict[str, Any]] = field(default_factory=dict)

    token_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    api_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    scripted: dict[str, list[httpx.Response]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seed()

    # ------------------------------------------------------------------ seeding --
    def seed(self) -> None:
        self.subreddits["india"] = subreddit_thing("india", title="India", subscribers=1_000_000)
        self.subreddits["cybersecurity"] = subreddit_thing(
            "cybersecurity", title="Cybersecurity", subscribers=500_000
        )
        self.subreddits["privatesub"] = subreddit_thing("privatesub", subreddit_type="private")
        self.subreddits["restrictedsub"] = subreddit_thing("restrictedsub", subreddit_type="restricted")
        self.subreddits["quarantinedsub"] = subreddit_thing("quarantinedsub", quarantine=True)

        self.posts["abc123"] = post_thing("abc123", subreddit="india")
        self.posts["def456"] = post_thing(
            "def456",
            subreddit="cybersecurity",
            title="Breaking ransomware attack",
            author="user4",
            author_fullname="t2_user4",
        )

        reply = comment_thing(
            "comment456", link_id="t3_abc123", parent_id="t1_comment123", body="A reply", author="user3"
        )
        top_level = comment_thing(
            "comment123",
            link_id="t3_abc123",
            body="Comment text",
            replies=listing([reply]),
        )
        self.comment_trees["abc123"] = listing([top_level])

        self.users["someuser"] = user_thing("someuser")
        self.users["user1"] = user_thing("user1", user_id="user1id")
        self.users["user2"] = user_thing("user2", user_id="user2id")
        self.users["user3"] = user_thing("user3", user_id="user3id")

    # -------------------------------------------------------------------- token --
    def token_handler(self, request: httpx.Request) -> httpx.Response:
        body = dict(x.split("=", 1) for x in request.content.decode().split("&") if "=" in x)
        self.token_calls.append((request.method, body))
        if self.reject_auth:
            return _json(401, {"message": "invalid_grant", "error": 401})
        return _json(
            200,
            {"access_token": self.access_token, "token_type": "bearer", "expires_in": 3600, "scope": "*"},
        )

    def token_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.token_handler)

    # ---------------------------------------------------------------------- api --
    def api_handler(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 - a routing table
        path = request.url.path
        query = dict(request.url.params)
        self.api_calls.append((request.method, path, query))

        for suffix, queue in self.scripted.items():
            if path.endswith(suffix) and queue:
                return queue.pop(0)

        if request.headers.get("Authorization") != f"Bearer {self.access_token}":
            return _json(401, {"message": "Unauthorized", "error": 401})

        if path == "/api/v1/me":
            return _json(200, {"name": "service_account", "id": "svcacct"})

        match = re.fullmatch(r"/r/(?P<name>[A-Za-z0-9_]+)/about", path)
        if match:
            sub = self.subreddits.get(match.group("name"))
            if sub is None:
                return _json(404, {"message": "Not Found", "error": 404})
            return _json(200, sub)

        match = re.fullmatch(
            r"/r/(?P<name>[A-Za-z0-9_+]+)/(?P<sort>new|hot|top|rising|controversial)", path
        )
        if match:
            names = match.group("name").split("+")
            known = [n for n in names if n in self.subreddits]
            if not known:
                return _json(404, {"message": "Not Found", "error": 404})
            if any(self.subreddits[n]["data"]["subreddit_type"] == "private" for n in known):
                return _json(403, {"message": "Forbidden", "error": 403})
            posts = [p for p in self.posts.values() if p["data"]["subreddit"] in names]
            return _json(200, listing(posts))

        match = re.fullmatch(r"(?:/r/(?P<sub>[A-Za-z0-9_]+))?/comments/(?P<post_id>[A-Za-z0-9]+)", path)
        if match:
            post = self.posts.get(match.group("post_id"))
            if post is None:
                return _json(404, {"message": "Not Found", "error": 404})
            comments = self.comment_trees.get(match.group("post_id"), listing([]))
            return _json(200, [listing([post]), comments])

        if path == "/api/morechildren" and request.method == "POST":
            body = dict(x.split("=", 1) for x in request.content.decode().split("&") if "=" in x)
            children_key = body.get("children", "")
            things = self.more_children.get(children_key, [])
            return _json(200, {"json": {"errors": [], "data": {"things": things}}})

        if path == "/api/info":
            ids = (query.get("id") or "").split(",")
            found: list[dict[str, Any]] = []
            for fullname in ids:
                if fullname.startswith("t1_"):
                    for tree in self.comment_trees.values():
                        found.extend(self._find_comment(tree, fullname[3:]))
                elif fullname.startswith("t3_") and fullname[3:] in self.posts:
                    found.append(self.posts[fullname[3:]])
            return _json(200, listing(found))

        if path == "/search" or re.fullmatch(r"/r/(?P<name>[A-Za-z0-9_+]+)/search", path):
            sub_match = re.fullmatch(r"/r/(?P<name>[A-Za-z0-9_+]+)/search", path)
            names = sub_match.group("name").split("+") if sub_match else None
            if names is not None and not any(n in self.subreddits for n in names):
                return _json(404, {"message": "Not Found", "error": 404})
            q = (query.get("q") or "").lower()
            # Mimics enough of Reddit's " OR " search operator to test keyword-OR
            # queries; a real substring/relevance search engine this is not.
            terms = [t.strip() for t in q.split(" or ")] if " or " in q else [q]

            def _matches(post: dict[str, Any]) -> bool:
                haystack = f"{post['data']['title']} {post['data']['selftext']}".lower()
                return any(term and term in haystack for term in terms)

            results = [
                p
                for p in self.posts.values()
                if (names is None or p["data"]["subreddit"] in names) and _matches(p)
            ]
            return _json(200, listing(results))

        if path == "/subreddits/search":
            q = (query.get("q") or "").lower()
            results = [s for s in self.subreddits.values() if q in s["data"]["display_name"].lower()]
            return _json(200, listing(results))

        match = re.fullmatch(r"/user/(?P<name>[A-Za-z0-9_-]+)/about", path)
        if match:
            user = self.users.get(match.group("name"))
            if user is None:
                return _json(404, {"message": "Not Found", "error": 404})
            return _json(200, user)

        match = re.fullmatch(r"/user/(?P<name>[A-Za-z0-9_-]+)/(?P<kind>submitted|comments)", path)
        if match:
            name = match.group("name")
            if name not in self.users:
                return _json(404, {"message": "Not Found", "error": 404})
            if match.group("kind") == "submitted":
                results = [p for p in self.posts.values() if p["data"]["author"] == name]
            else:
                results = []
                for tree in self.comment_trees.values():
                    results.extend(self._all_comments(tree))
                results = [c for c in results if c["data"]["author"] == name]
            return _json(200, listing(results))

        return _json(404, {"message": "Not Found", "error": 404})

    def _find_comment(self, tree: dict[str, Any], comment_id: str) -> list[dict[str, Any]]:
        return [c for c in self._all_comments(tree) if c["data"]["id"] == comment_id]

    def _all_comments(self, tree: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for child in tree.get("data", {}).get("children", []):
            if child.get("kind") != "t1":
                continue
            out.append(child)
            replies = child["data"].get("replies")
            if isinstance(replies, dict):
                out.extend(self._all_comments(replies))
        return out

    def api_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.api_handler)

    # ---------------------------------------------------------------- assertions --
    def calls_to(self, suffix: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [call for call in self.api_calls if call[1].endswith(suffix)]
