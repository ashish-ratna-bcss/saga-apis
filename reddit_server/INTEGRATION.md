# Integration Guide

For engineers wiring **any application** up to this Reddit provider service --
a web app, a mobile backend, a security/OSINT monitoring pipeline, a data
pipeline, an internal tool, anything that needs normalized Reddit data without
touching Reddit's own API or scraping it directly.

This service is a self-contained HTTP API. You do **not** need Reddit
credentials, Reddit libraries, or any knowledge of the Reddit API in your
application -- you call this service, it handles Reddit. It has **two
independent transports** you can pick between per request (see
["Choosing a transport"](#0-choosing-a-transport) below).

- **Base URL** -- wherever it is deployed, e.g. `http://reddit-service:8000`
  (see [DEPLOYMENT.md](DEPLOYMENT.md) for how to stand one up)
- **Auth** -- `X-API-Key` header (optional; see section 2)
- **Content type** -- `application/json`
- **Interactive docs** -- `GET /docs` (Swagger), `GET /openapi.json` (machine-readable)

---

## Table of contents

0. [Choosing a transport](#0-choosing-a-transport)
1. [Quick start](#1-quick-start)
2. [Authentication](#2-authentication)
3. [Error handling](#3-error-handling)
4. [IDs](#4-ids)
5. [Pagination](#5-pagination)
6. [Integration patterns](#6-integration-patterns)
7. [Endpoint reference](#7-endpoint-reference)
8. [Known Reddit API limitations](#8-known-reddit-api-limitations)
9. [Normalized data model](#9-normalized-data-model)
10. [Unauthenticated RSS transport](#10-unauthenticated-rss-transport)
11. [Client code examples](#11-client-code-examples)
12. [Versioning and compatibility](#12-versioning-and-compatibility)

---

## 0. Choosing a transport

| | OAuth API (`/api/reddit/*`, not `/rss/*`) | Public RSS (`/api/reddit/rss/*`) |
|---|---|---|
| Reddit credentials required | Yes (`REDDIT_CLIENT_ID`/`SECRET`/`USER_AGENT`) | No |
| Data source | Reddit's official authenticated API | Reddit's public RSS/Atom feeds |
| Coverage | Subreddits, posts, comments (full tree), users, search, URL resolution | Post discovery via search/subreddit feeds only -- no comment tree, no user profiles |
| Rate limit | Reddit's per-token OAuth limit (generous; this service throttles proactively) | Reddit's per-IP RSS limit, cut hard in June 2026 to ~1 req/min unless you set `REDDIT_RSS_USER`/`REDDIT_RSS_FEED` -- see section 10 |
| Best for | Investigation workflows (post → comments → author → their history), anything needing a specific post/comment/user by id | Keyword/event monitoring across many subreddits at once, deployments that can't or don't want to register a Reddit app |
| Multi-caller behavior | Each call is independent | Shared gateway: many callers' identical requests coalesce into one Reddit fetch (see section 10) |

Both transports return normalized JSON through the same error envelope (section
3) and the same base URL -- you can use either, or both, from the same
application. If you're not sure, start with RSS (zero setup) and add the OAuth
transport later if you need comment trees or user history.

---

## 1. Quick start

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok" }
```

```bash
curl http://localhost:8000/ready
```

```json
{ "status": "ok", "reddit_configured": true }
```

```bash
curl http://localhost:8000/api/reddit/status
```

```json
{ "connected": true, "authorized": true, "account": { "username": "a****t" } }
```

Generate an OpenAPI client if you prefer:

```bash
curl http://localhost:8000/openapi.json -o reddit-service.json
# openapi-generator generate -i reddit-service.json -g <your-language>
```

---

## 2. Authentication

Two independent authentication layers, easy to conflate -- keep them separate:

```text
Reddit authentication (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT):
    Required for /api/reddit/* (everything except /rss/*).
    NOT required for /api/reddit/rss/* -- see section 10.

This service's own authentication (X-API-Key / API_KEYS):
    Governs every /api/reddit/* route, /rss/* included, per this section.
```

Set `API_KEYS` on the service to a comma-separated list -- issue one key per consuming
application so they can be revoked independently:

```bash
API_KEYS=rsk_soceye_xxxxxxxx,rsk_other_yyyyyyyy
```

Send it on every `/api/reddit/*` request:

```
X-API-Key: rsk_soceye_xxxxxxxx
```

Generate a key:

```bash
python -c "from app.core.security import generate_api_key; print(generate_api_key())"
```

| Endpoint | Key required |
|---|---|
| `GET /health`, `GET /ready` | No -- for load balancers and uptime probes |
| `GET /docs`, `/openapi.json` | No |
| **everything under `/api/reddit/*`** | **Yes, when `API_KEYS` is set** |

> If `API_KEYS` is empty the API is **open**. That is the local-development default;
> the service logs a warning at startup. Never deploy that way -- your
> deployment should always set `API_KEYS` (see [DEPLOYMENT.md](DEPLOYMENT.md)).

Reddit's own OAuth2 access token is never returned by any endpoint of this service,
including `/api/reddit/status`, which reports only a masked account username (or
`null` for application-only auth).

---

## 3. Error handling

Every error returns the same shape:

```json
{ "error": { "code": "SUBREDDIT_NOT_FOUND", "message": "The subreddit r/doesnotexist could not be found.", "subreddit": "doesnotexist" } }
```

Handle `code`, not the human `message`. Extra structured fields (like `subreddit`
above, or `retry_after` on a rate limit) are merged directly into `error`.

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| 400 | `INVALID_REDDIT_URL` | URL doesn't match a subreddit/post/comment pattern | Fix the input. Do not retry. |
| 400 | `UNSUPPORTED_SEARCH` | Requested a search Reddit's API cannot do (e.g. `type=comments`) | Don't retry; use a different endpoint (see section 8). |
| 401 | `UNAUTHORIZED` | Missing/invalid `X-API-Key` | Fix the key. Do not retry. |
| 403 | `SUBREDDIT_PRIVATE` | Subreddit exists but is private | Do not retry. |
| 403 | `SUBREDDIT_QUARANTINED` | Subreddit is quarantined (see section 8) | Do not retry. |
| 403 | `REDDIT_FORBIDDEN` | Reddit refused for another reason (suspended/shadow-restricted account, etc.) | Do not retry. |
| 404 | `SUBREDDIT_NOT_FOUND` / `POST_NOT_FOUND` / `COMMENT_NOT_FOUND` / `USER_NOT_FOUND` | Resource does not exist | Do not retry. |
| 422 | `VALIDATION_ERROR` | Bad input (missing required field, invalid sort/time value, bad cursor) | Fix the input. |
| 429 | `REDDIT_RATE_LIMITED` | Reddit's rate limit was hit and this service's internal retries were exhausted | Retry after `error.retry_after` seconds. |
| 502 | `REDDIT_AUTH_FAILED` | Reddit rejected the configured credentials | Operator must fix. Do not retry. |
| 502 / 504 | `REDDIT_API_ERROR` | Reddit is unwell, or a network/timeout error talking to it | Retry with backoff. |
| 503 | `REDDIT_NOT_CONFIGURED` | No Reddit credentials set | Operator must fix. |

**Retry only** 429, 502, 504. This service already retries Reddit internally with
bounded exponential backoff and jitter, and proactively throttles from Reddit's own
`X-Ratelimit-*` response headers before ever hitting a 429 -- so keep your own retries
modest (a single retry after `error.retry_after` is usually enough).

---

## 4. IDs

Post and comment ids are Reddit's own **bare base36 ids** (no `t1_`/`t3_` prefix), e.g.
`"abc123"` -- matching what appears in a Reddit URL. Subreddit and user ids keep
Reddit's own **fullname** (`t5_...`, `t2_...`) since a bare subreddit/user id has no
independent meaning outside Reddit's internal API. All ids are strings; never treat
them as numbers.

To go from a name to more ids:

```bash
POST /api/reddit/subreddit   {"name": "india"}
POST /api/reddit/user        {"username": "someuser"}
POST /api/reddit/resolve     {"url": "https://reddit.com/r/india/comments/abc123/x/"}
```

---

## 5. Pagination

Every list endpoint takes `limit` and `cursor`, and returns:

```json
{ "items": [ /* ... */ ], "cursor": "eyJhZnRlciI6ICJ0M19hYmMxMjMifQ" }
```

`cursor` is an opaque, base64-encoded token -- do not decode or construct it
yourself, just pass whatever you received back as the next request's `cursor`.
`null` means there are no more results. Internally it wraps Reddit's own `after`
token (for subreddit/search/user listings) or a flat-list offset (for the
locally-flattened `POST_COMMENTS` tree, which Reddit does not paginate the way it
paginates listings) -- your application never needs to know which.

`limit` is clamped to `[1, 100]` server-side regardless of what's requested (Reddit's
own listing cap).

---

## 6. Integration patterns

This service owns Reddit connectivity only: authentication, pagination,
normalization, retries, rate-limit compliance. **Your application** owns
monitoring start/stop, the polling schedule, deduplication, storage,
detection/analysis, and alerts. This service never runs a background poll loop
and has no `/monitoring/start`-style endpoint anywhere -- every pattern below
is something your own scheduler drives by calling this service repeatedly.

**Subreddit monitoring:**

```
Your scheduler
        |
        v
POST /api/reddit/subreddit/posts   (sort=new, cursor from last run)
        |
        v
Reddit Provider  ->  New posts
        |
        v
Your dedup -> detection/analysis -> alerts
```

**Keyword monitoring:**

```
Your scheduler
        |
        v
GET /api/reddit/search/posts?q=<keyword>
        |
        v
Reddit Provider  ->  Matching public posts
        |
        v
Your analysis / alerts
```

**Event monitoring (subreddit targeting + keyword bursts):** both `SUBREDDIT_POSTS`
and `SEARCH_POSTS` accept Reddit's own "+"-joined combined-subreddit syntax in
`subreddit`, so a fixed set of "event subreddits" is one call/cursor instead of one
per subreddit:

```
POST /api/reddit/subreddit/posts
{ "subreddit": "worldnews+politics+India+breakingnews+news+geopolitics+CurrentEvents", "sort": "new" }

GET /api/reddit/search/posts?keywords=protest&keywords=strike&keywords=riot&keywords=curfew&subreddit=India+worldnews+politics&sort=new&t=day
```

`keywords` (repeat the query param, one value per term) is OR'd into `q` server-side
when `q` itself is omitted -- pass `q` directly instead for exact phrases, `AND`, or
`flair:` operators. This service still does not poll, deduplicate, or detect bursts
itself (see the model above): your application calls one of these on its own
schedule (15 min is the commonly recommended floor to stay well under Reddit's
rate limit -- this service also proactively throttles from Reddit's own
headers) and owns dedup/burst detection against the returned `id`s.

**Investigation:**

```
POST /api/reddit/post              (post details)
        |
        v
POST /api/reddit/post/comments     (comment tree, parent_id preserved)
        |
        v
POST /api/reddit/user              (comment author's profile)
        |
        v
POST /api/reddit/user/posts, POST /api/reddit/user/comments   (their public activity)
```

---

## 7. Endpoint reference

### `GET /health`

Process liveness. Never requires the API key, never touches Reddit.

```json
{ "status": "ok" }
```

### `GET /ready`

Configuration check only (no Reddit call, so it stays cheap for readiness probes).

```json
{ "status": "ok", "reddit_configured": true }
```

### `GET /api/reddit/status`

Live Reddit connection/auth check (a real probe, not cached).

```json
{ "connected": true, "authorized": true, "account": { "username": "a****t" } }
```

`account` is `null` under application-only (`client_credentials`) auth, which has no
specific Reddit account.

### `POST /api/reddit/subreddit`

```json
// request
{ "name": "india" }
```

```json
// response
{
  "id": "t5_2qh1e", "name": "india", "display_name": "r/india",
  "title": "India", "description": "...", "subscribers": 1000000,
  "url": "https://www.reddit.com/r/india/", "public": true, "over18": false
}
```

### `POST /api/reddit/subreddit/posts`

The recommended subreddit-polling endpoint.

```json
// request
{ "subreddit": "india", "sort": "new", "limit": 50, "cursor": null }
```

```json
// response
{
  "items": [
    {
      "id": "abc123", "subreddit": "india", "title": "Example title",
      "text": "Post body", "author": { "id": "t2_xxx", "username": "user1" },
      "created_at": "2026-09-08T10:00:00Z",
      "url": "https://www.reddit.com/r/india/comments/abc123/example/",
      "score": 123, "upvote_ratio": 0.95, "num_comments": 25, "media": []
    }
  ],
  "cursor": null
}
```

`sort` is one of `new`, `hot`, `top`, `rising`, `controversial`. `subreddit` may also be
Reddit's `+`-joined combined-subreddit syntax (e.g. `"worldnews+politics+India"`) to
poll a fixed set of "event subreddits" as one call/cursor.

### `POST /api/reddit/subreddit/access`

```json
// request
{ "subreddit": "india" }
```

```json
// response
{ "accessible": true, "state": "public", "detail": "readable" }
```

`state` is one of `public`, `private`, `restricted`, `quarantined`, `not_found`,
`unknown`. There is no Telegram-style `JOIN_INVITE` endpoint -- Reddit's access model
has no equivalent request-to-join flow for a service account to drive.

### `POST /api/reddit/post`

Accepts either `post_id` or `url`.

```json
{ "post_id": "abc123" }
```

or

```json
{ "url": "https://www.reddit.com/r/india/comments/abc123/example/" }
```

Response: the same Post shape as above.

### `POST /api/reddit/post/comments`

```json
// request
{ "post_id": "abc123", "limit": 100, "cursor": null }
```

```json
// response
{
  "items": [
    {
      "id": "comment123", "post_id": "abc123", "parent_id": null,
      "text": "Comment text", "author": { "id": "t2_yyy", "username": "user2" },
      "created_at": "2026-09-08T11:00:00Z", "score": 10,
      "url": "https://www.reddit.com/r/india/comments/abc123/example/comment123/"
    }
  ],
  "cursor": null
}
```

`parent_id` is `null` for a top-level comment (reply to the post itself) and the bare
id of the parent comment otherwise -- always enough for your application to
reconstruct the reply tree. See section 8 for the large-thread caveat.

### `POST /api/reddit/comment`

```json
{ "comment_id": "comment123" }
```

Response: the normalized Comment shape.

### `GET /api/reddit/search/posts`

Global public post discovery -- also the keyword/event-monitoring entry point.

```
GET /api/reddit/search/posts?q=ransomware&limit=50
GET /api/reddit/search/posts?q=drug%20trafficking&subreddit=india
GET /api/reddit/search/posts?keywords=protest&keywords=strike&keywords=curfew&subreddit=india+worldnews
```

Query params: `q` or `keywords` (one required -- `keywords`, repeated once per term,
is OR'd into a query server-side when `q` is omitted; pass `q` directly for exact
phrases/`AND`/`flair:` operators), `limit`, `cursor`, `sort`
(`relevance|hot|top|new|comments`, default `relevance`), `time`
(`hour|day|week|month|year|all`, default `all`), `subreddit` (restrict the search to
one subreddit, or several via `+`-joined combined-subreddit syntax).

```json
{
  "items": [
    {
      "id": "abc123", "subreddit": "india", "title": "...", "text": "...",
      "author": { "id": "t2_xxx", "username": "user1" }, "created_at": "...",
      "url": "...", "score": 100, "upvote_ratio": 0.9, "num_comments": 20, "media": []
    }
  ],
  "cursor": null
}
```

`sort=comments` here means "posts sorted by their comment count" (Reddit's own search
sort option) -- it does not search comment bodies. See section 8.

### `GET /api/reddit/search/subreddits`

```
GET /api/reddit/search/subreddits?q=cybersecurity&limit=20
```

```json
{
  "items": [
    {
      "id": "t5_xxx", "name": "cybersecurity", "display_name": "r/cybersecurity",
      "title": "Cybersecurity", "subscribers": 500000,
      "url": "https://www.reddit.com/r/cybersecurity/"
    }
  ],
  "cursor": null
}
```

### `GET /api/reddit/search`

Unified search abstraction. `type=posts` (default) and `type=subreddits` are
supported; `type=comments` returns `400 UNSUPPORTED_SEARCH` (see section 8).

```
GET /api/reddit/search?q=ransomware&type=posts&limit=50
```

Query params: `q` (required), `type` (`posts|subreddits`, default `posts`), `limit`,
`cursor`, `subreddit`, `time`.

### `POST /api/reddit/user`

```json
{ "username": "someuser" }
```

```json
{
  "id": "t2_8xwlg", "username": "someuser", "created_at": "2020-01-01T00:00:00Z",
  "link_karma": 1234, "comment_karma": 5678, "is_mod": false
}
```

Only what Reddit's public, authorized `/user/{username}/about` exposes -- no e-mail,
no verified status, no private account data.

### `POST /api/reddit/user/posts`

```json
{ "username": "someuser", "limit": 50, "cursor": null }
```

Response: `{"items": [Post, ...], "cursor": null}`.

### `POST /api/reddit/user/comments`

```json
{ "username": "someuser", "limit": 50, "cursor": null }
```

Response: `{"items": [Comment, ...], "cursor": null}`.

### `POST /api/reddit/resolve`

```json
// request
{ "url": "https://www.reddit.com/r/india/comments/abc123/example/" }
```

```json
// response (post)
{ "kind": "post", "subreddit": { "...": "..." }, "post": { "...": "..." } }
```

```json
// response (comment url)
{
  "kind": "comment",
  "subreddit": { "...": "..." }, "post": { "...": "..." }, "comment": { "...": "..." }
}
```

```json
// response (subreddit url)
{ "kind": "subreddit", "subreddit": { "...": "..." } }
```

Accepts `reddit.com`, `www.reddit.com`, `old.reddit.com`, `new.reddit.com`,
`np.reddit.com`, `m.reddit.com`, `amp.reddit.com` URLs (with or without a scheme) and
`redd.it` short links. Anything else returns `400 INVALID_REDDIT_URL`.

---

## 8. Known Reddit API limitations

**No free-text search across all comments.** Reddit's public search API
(`/search`, `/r/{sub}/search`) indexes posts and subreddits, not arbitrary comment
bodies. `SEARCH_POSTS` and the unified `SEARCH` endpoint represent global **public post**
discovery, matching the task's requirement not to claim unrestricted global comment
search. `type=comments` on `GET /api/reddit/search` returns a structured
`400 UNSUPPORTED_SEARCH` error rather than fabricating results. To get a specific
post's comments, use `POST /api/reddit/post/comments`.

**Very large comment threads may not be fully expanded in one call.** Reddit's
`GET /comments/{id}` truncates deep/large threads behind "more comments" ("more")
stubs. `POST /api/reddit/post/comments` follows up to 3 of those automatically per
request via Reddit's `api/morechildren`, bounded to protect the rate limit and this
service's response latency -- an exceptionally large thread's very deepest stubs may
still not all be expanded in a single call (they will simply be absent from `items`,
not fabricated or duplicated). `parent_id` is always preserved for whatever is
returned, so your application can reconstruct the tree from however many comments
come back, and can call again with successive `cursor`s to keep paging through
what this service already flattened.

**Quarantined subreddits are not readable by this service.** Reddit requires an
interactive, logged-in user to click through the quarantine interstitial
confirmation; an application-only or non-interactive service-account integration
cannot do that programmatically. `POST /api/reddit/subreddit/access` reports
`state: "quarantined"`, `accessible: false` for these rather than silently returning
empty results.

**Private subreddits are not readable without membership.** `subreddit_type: private`
subreddits return `403 SUBREDDIT_PRIVATE` from `SUBREDDIT_INFO`/`SUBREDDIT_POSTS`, and
`accessible: false, state: "private"` from `CHECK_SUBREDDIT_ACCESS`. There is no way
for a service account to be granted access without a subreddit moderator adding the
account as an approved user directly on Reddit -- this service has no API to request
that (mirroring why there is no `JOIN_INVITE`-style endpoint here at all).

**`USER_INFO` returns only Reddit's public, authorized profile fields.** No e-mail, no
verified status, no IP/session data -- Reddit's API does not expose that to any
third-party integration regardless of authentication, except to the token's own
account owner querying themselves.

**No real-time push.** Reddit's public API is pull-only request/response; there is no
webhook or streaming equivalent. "Live" monitoring means your application
polling `SUBREDDIT_POSTS`/`SEARCH_POSTS` on its own schedule -- see section 6.

---

## 9. Normalized data model

### Post

```json
{
  "id": "abc123", "subreddit": "india", "title": "...", "text": "...",
  "author": { "id": "t2_xxx", "username": "..." }, "created_at": "2026-09-08T10:00:00Z",
  "url": "https://www.reddit.com/...", "score": 0, "upvote_ratio": 0,
  "num_comments": 0, "media": []
}
```

`media` is a best-effort extraction of image/video/gallery attachments (Reddit spreads
this across several different raw fields depending on post type); external link posts
that aren't a recognized media type return an empty list.

### Comment

```json
{
  "id": "...", "post_id": "...", "parent_id": "...",
  "text": "...", "author": { "id": "...", "username": "..." },
  "created_at": "...", "score": 0, "url": "..."
}
```

### Subreddit

```json
{
  "id": "t5_xxxxx", "name": "india", "display_name": "r/india", "title": "India",
  "description": "...", "subscribers": 1000000, "url": "https://www.reddit.com/r/india/",
  "public": true, "over18": false
}
```

### User

```json
{
  "id": "t2_xxx", "username": "someuser", "created_at": "2020-01-01T00:00:00Z",
  "link_karma": 0, "comment_karma": 0, "is_mod": false
}
```

Every id above is Reddit's own id (bare for posts/comments, fullname for
subreddits/users) -- your application can deduplicate directly against it, no
translation layer needed on either side.

---

## 10. Unauthenticated RSS transport

`/api/reddit/rss/*` is a second, independent Reddit transport alongside everything
in sections 1-9: keyword/event monitoring through Reddit's **public RSS/Atom
feeds** instead of the official OAuth2 API. It is a public search interface, not
the official Reddit API -- describe it to your own stakeholders accordingly.

```text
No Reddit OAuth credentials required or read (REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET
    are never touched by this transport -- see app/reddit/rss_client.py).
Still governed by this service's own X-API-Key policy (section 2).
No persistent state, no scheduler, no cross-request deduplication -- your
    application still owns polling on its own schedule, exactly as in section 6.
No historical trend detection -- event_signal (below) reflects only the current
    response, never a claim about a confirmed real-world event.
Subject to Reddit's own RSS availability/rate limits -- not unlimited, not
    guaranteed. Reddit may 403/429 an unauthenticated request at any time
    (anti-scraping/IP reputation); this service surfaces that as
    REDDIT_RSS_FORBIDDEN/REDDIT_RSS_RATE_LIMITED rather than hiding it.
```

### Rate limits and the `user=`/`feed=` workaround (June 2026)

Reddit cut unauthenticated RSS's rate limit hard in June 2026 -- roughly
100 requests/10 minutes down to ~1 request/minute per feed, confirmed via
`X-Ratelimit-*` response headers (same header names the OAuth API already
sends). This client throttles proactively from those headers automatically
(mirrors `RedditRestClient`'s own bucket, see `app/reddit/rss_client.py`) --
no configuration required, it just waits exactly as long as Reddit says.

Reddit's own documented workaround is appending `user=<username>` and
`feed=<token>` to every RSS request; both values come from
**reddit.com -> Preferences -> RSS Feeds** (requires a free Reddit account,
unrelated to `REDDIT_CLIENT_ID`/OAuth). Set `REDDIT_RSS_USER`/`REDDIT_RSS_FEED`
and every request this service makes carries them automatically -- there is
nothing to change in your own API calls. This is optional: the service works
without it, just harder-throttled. `REDDIT_RSS_FEED` is handled as a secret
(never logged, never returned by any endpoint) even though it isn't a login
credential.

**Not independently verified, flagged rather than acted on:** reports that
`search.rss` sometimes doesn't apply `q=` filtering, and that Reddit is moving
to end RSS access entirely. What's independently confirmed is narrower -- an
r/modnews post deprecating *unauthenticated JSON endpoints* (not RSS) that
also asked moderators how they use RSS. If `search.rss` ever does silently
stop filtering, this is degraded, not silently wrong: `matched_keywords` is
computed client-side against whatever Reddit returns regardless of whether
Reddit's own filtering worked, so a caller can still distinguish genuine hits
from noise. If Reddit's own filtering fails entirely, expect `count`/`posts` to
include irrelevant posts with `matched_keywords: []`.

### Shared-gateway architecture

`/api/reddit/rss/*` is designed for **many independent callers hitting one
process**, which in turn hits one Reddit-facing IP under the ~1 req/min budget
above. Every caller supplies its own keywords/scopes/filters -- this service
holds no hardcoded intelligence -- but Reddit *acquisition* is shared:

```text
Callers (N)
    |
    v
ClientRateLimiter        <- Layer 3: per-caller budget (REDDIT_RSS_CLIENT_LIMIT
    |                        req / REDDIT_RSS_CLIENT_WINDOW_SECONDS), protects
    |                        the shared layers below from one caller
    |                        monopolizing them. Scoped to this router only.
    v
FeedCache + coalescing   <- Layer 2: raw feed bytes cached by the exact Reddit
    |                        URL (path + params) for REDDIT_RSS_CACHE_TTL_SECONDS.
    |                        N callers requesting the identical feed within the
    |                        TTL cause exactly one Reddit fetch; the rest either
    |                        get the fresh cache hit or join the one already in
    |                        flight. Keyword/exclude/match_field filtering is
    |                        computed fresh per caller *after* this point --
    |                        the cache never sees caller-specific options.
    v
Global TokenBucket       <- Layer 1: the hard gate. Proactively paces every
    |                        outbound attempt -- retries included -- at
    |                        REDDIT_RSS_GLOBAL_RATE, before Reddit's own
    |                        response headers ever get a say. A request that
    |                        would wait longer than REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS
    |                        fails fast with REDDIT_RSS_QUEUE_TIMEOUT instead of
    |                        hanging indefinitely.
    v
RedditRssClient          <- existing reactive header-driven throttle, retries,
    |                        timeout, size cap (unchanged, see above).
    v
Reddit RSS
```

Example: 10 callers polling the same 5 feeds every 60 seconds is 50 potential
client requests but only 5 unique feed acquisitions -- 5 Reddit requests, not
50. Different feeds never share a cache key or a token-bucket-per-subreddit
(there is one global bucket, not one per feed) -- see `app/reddit/feed_cache.py`
and `app/reddit/rate_limiter.py`.

**Process-local, not distributed.** All three layers live in one process's
memory. Behind N worker processes or N instances fronting the same public IP,
each gets its own budget/cache/limiter, so the *aggregate* could still exceed
Reddit's tolerance even though each process individually behaves. A shared
backend (e.g. Redis) would be needed to enforce one true budget across
processes -- deliberately not added here: this repo has no such dependency
today, and a single-process deployment needs none.

### Generic caller-driven filtering

Every field below is caller-supplied; nothing is hardcoded. `POST /monitor`,
`GET /search`, and `POST /event` all take:

- `keywords` -- OR'd into the Reddit query when `query` is omitted, and always
  used for `matched_keywords`/`signal` regardless of which built the query.
- `strong_keywords` -- a match here sets `signal: true` unconditionally,
  regardless of `min_matches`.
- `exclude` -- a post matching any of these is **dropped from the results
  entirely**, not just flagged.
- `match_field` (`"title"` | `"full"`, default `"full"`) -- `"title"` checks
  only the post title; `"full"` (unchanged default behavior) also checks
  content/flair.
- `min_matches` (default `1`) -- minimum `matched_keywords` count (excluding
  `strong_keywords`) for `signal: true`.

Every returned post carries `matched_keywords` (deduplicated, includes
`strong_keywords` hits) and `signal` (bool) -- a caller-computed hint from the
caller's own thresholds, never a hard filter beyond `exclude`. Filtering runs
fresh per request against whatever feed came back from the cache, so two
callers sharing one cached feed with different `keywords`/`exclude` get fully
independent, correctly-isolated results (no caller's options are stored or
reused for another caller -- see `RedditRssService.monitor`).

### `POST /api/reddit/rss/monitor`

```bash
curl -X POST http://localhost:8000/api/reddit/rss/monitor \
  -H 'Content-Type: application/json' \
  -d '{"query": "protest", "sort": "new", "time_range": "day", "limit": 25}'
```

OR-keyword monitoring, scoped to several subreddits at once:

```bash
curl -X POST http://localhost:8000/api/reddit/rss/monitor \
  -H 'Content-Type: application/json' \
  -d '{
        "keywords": ["protest", "strike", "curfew"],
        "subreddits": ["India", "worldnews", "politics"],
        "sort": "new",
        "time_range": "day",
        "limit": 100
      }'
```

Request fields: `query` or `keywords` (at least one required -- `keywords` is OR'd
into a query when `query` is omitted), `strong_keywords`/`exclude`/`match_field`/
`min_matches` (generic filtering, see above), `subreddits` (list; empty means
global search; Reddit's own `+`-joined combined-subreddit syntax is used
internally to scope one request to several subreddits), `sort`
(`relevance|hot|top|new|comments`, default `new`), `time_range`
(`hour|day|week|month|year|all`, default `day`), `from_date`/`to_date` (ISO 8601
date or datetime, e.g. `"2026-08-01"`; see below), `limit` (max 100).

**`from_date`/`to_date`** filter by each post's own `published_at`, applied
**client-side after fetching** -- Reddit's RSS/search API has no server-side
absolute date-range parameter, only the relative `time_range` buckets above,
so use both together: `time_range` narrows what Reddit itself returns,
`from_date`/`to_date` narrow it further to an exact window. A bare `to_date`
(no time component) means through the end of that day. A post whose date can't
be parsed is **excluded** when a date filter is active, never assumed in-range.

```json
{
  "source": "reddit", "transport": "rss", "authenticated": false,
  "query": "protest OR strike OR curfew", "subreddits": ["India", "worldnews"],
  "sort": "new", "time_range": "day", "count": 1,
  "posts": [
    {
      "id": "abc123", "guid": "t3_abc123", "title": "Example title",
      "author": "someuser",
      "url": "https://www.reddit.com/r/India/comments/abc123/example/",
      "subreddit": "India", "flair": null,
      "published_at": "2026-09-10T08:20:00Z", "updated_at": "2026-09-10T08:20:00Z",
      "content": "post description", "source": "reddit", "source_type": "rss",
      "matched_keywords": ["protest"], "signal": true
    }
  ]
}
```

`flair` is always `null` -- Reddit's Atom feed does not expose post flair as a
distinct field, and this service never invents one. `matched_keywords` is `[]`
whenever `query` was used directly (there is no discrete term list to check a
free-text query against); it is populated whenever `keywords`/`strong_keywords`
was supplied. `signal` is `true` when any `strong_keywords` matched or
`matched_keywords` reached `min_matches` -- a hint, not a hard filter.

### `GET /api/reddit/rss/search`

The GET convenience form. Calls the exact same use-case as `/monitor` --
there is one code path, not two (see `app/services/reddit_rss_service.py`).

```bash
curl 'http://localhost:8000/api/reddit/rss/search?keywords=protest&keywords=strike&keywords=curfew&subreddit=India+worldnews&sort=new&time=day'
```

```bash
# exact phrase / full Reddit search-operator control via q
curl 'http://localhost:8000/api/reddit/rss/search?q=%22climate+strike%22&sort=new&time=day'
```

Query params: `q` or `keywords` (repeatable; at least one required),
`strong_keywords`/`exclude` (repeatable), `match_field`, `min_matches`,
`subreddit` (one name, or several via `+`), `sort`, `time` (same values as
`/monitor`), `from_date`/`to_date` (client-side filter, see above), `limit`.

### `POST /api/reddit/rss/event`

Event monitoring: the pasted "event subreddit + keyword trigger" strategy, built
in as the default `subreddits`/`keywords` (`worldnews`, `politics`, `India`,
`breakingnews`, `news`, `geopolitics`, `CurrentEvents` / `protest`, `strike`,
`riot`, `demonstration`, `march`, `sit-in`, `curfew`, `shutdown`, `arrest`,
`detained`, `water cannon`, `tear gas`) -- override either list to monitor a
different set.

```bash
# use the built-in default strategy
curl -X POST http://localhost:8000/api/reddit/rss/event -H 'Content-Type: application/json' -d '{}'

# override with your own subreddits/keywords
curl -X POST http://localhost:8000/api/reddit/rss/event \
  -H 'Content-Type: application/json' \
  -d '{"subreddits": ["cybersecurity"], "keywords": ["breach", "ransomware"], "time_range": "day", "limit": 100}'
```

Response is the same shape as `/monitor` plus `event_signal`:

```json
{
  "event_signal": {
    "detected": true,
    "post_count": 24,
    "unique_subreddits": 5,
    "matched_keywords": ["protest", "strike"],
    "threshold": 10
  }
}
```

`detected = post_count >= REDDIT_RSS_EVENT_THRESHOLD` (default 10, configurable).
This is a **stateless, single-response activity signal** -- call it
`event_signal`/`activity_burst`/`event_candidate`, never a confirmed real-world
event: no history is kept across calls, so there is no trend detection, and a
burst of posts about the same story is not independently verified as a real
event. Treat it as a hint for your own detection logic, not a verdict.

### Deduplication (RSS-specific)

Within one response, posts are deduplicated by id/guid/canonical URL (see
`_dedupe` in `reddit_rss_service.py`) -- Reddit occasionally repeats an entry
within a single feed. **Cross-request deduplication is intentionally not
performed**: this service is stateless (section 6 applies here too), so your
application must deduplicate against post `id`s across its own polling calls,
exactly as it already does for `SUBREDDIT_POSTS`/`SEARCH_POSTS`.

### Error codes (RSS-specific)

Same `{"error": {"code": ..., "message": ...}}` envelope as section 3, with its
own codes:

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| 422 | `REDDIT_RSS_INVALID_QUERY` | Neither `query` nor `keywords` resolved to a non-empty search term, `sort`/`time_range`/`limit`/keyword count-or-length is invalid, or `from_date`/`to_date` isn't parseable ISO 8601 / `from_date` is after `to_date` | Fix the input. |
| 422 | `REDDIT_RSS_INVALID_SUBREDDIT` | A subreddit name failed validation, or too many were given | Fix the input. |
| 403 | `REDDIT_RSS_FORBIDDEN` | Reddit blocked this request (anti-scraping/IP reputation) -- not a credentials problem, there are none to fix | Back off; do not retry immediately. |
| 429 | `REDDIT_RSS_RATE_LIMITED` | Reddit's RSS rate limit was hit and retries were exhausted | Retry after `error.retry_after` seconds. |
| 502 | `REDDIT_RSS_UNAVAILABLE` | 5xx from Reddit, a connection failure, an oversized response, or an unexpected status | Retry with backoff. |
| 502 | `REDDIT_RSS_PARSE_ERROR` | Reddit returned a 200 whose body is not well-formed XML | Retry; if persistent, Reddit's feed format may have changed. |
| 504 | `REDDIT_RSS_TIMEOUT` | Timed out talking to Reddit RSS | Retry with backoff. |
| 504 | `REDDIT_RSS_QUEUE_TIMEOUT` | Waiting for the shared process-wide Reddit RSS budget (Layer 1) would have taken longer than `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS` -- the request never reached Reddit at all | Retry later; if frequent, the shared budget is oversubscribed for this deployment's call volume. |
| 429 | `REDDIT_RSS_CLIENT_RATE_LIMITED` | *This caller* exceeded its own request budget against this service (Layer 3) -- distinct from `REDDIT_RSS_RATE_LIMITED`, which means Reddit itself rate limited this service | Retry after `error.retry_after` seconds; back off your own polling interval. |

A successful, empty feed (no matching posts) is `200` with `"count": 0,
"posts": []` -- never conflated with a failure (section 13's "empty vs. failed"
distinction).

### Security

Callers supply `query`/`keywords`/`strong_keywords`/`exclude`/`match_field`/
`min_matches`/`subreddits`/`sort`/`time_range`/`limit` only. There is no way to
pass a URL, host, scheme, path, header, or proxy target -- every request this
service makes to Reddit is built internally from validated, bounded inputs
against a single hardcoded host (`www.reddit.com`). See `app/reddit/rss_urls.py`.
`keywords`/`strong_keywords`/`exclude` are each capped at 25 entries of 100
characters; `subreddits` at 25 names. The per-caller (`REDDIT_RSS_CLIENT_LIMIT`)
and process-wide (`REDDIT_RSS_GLOBAL_RATE`) limiters bound request volume and
outbound Reddit traffic respectively -- see "Shared-gateway architecture" above.

### Configuration

All optional, all have working defaults -- see `.env.example`:

`REDDIT_RSS_USER_AGENT`, `REDDIT_RSS_USER`/`REDDIT_RSS_FEED` (the rate-limit
workaround above), `REDDIT_RSS_TIMEOUT_SECONDS`, `REDDIT_RSS_MAX_RETRIES`,
`REDDIT_RSS_RETRY_BASE_DELAY_SECONDS`, `REDDIT_RSS_MAX_RESPONSE_BYTES`,
`REDDIT_RSS_EVENT_THRESHOLD`. None of the `REDDIT_*` OAuth settings from section 2
are read by this transport.

Shared-gateway infra (all process-local, see above):
`REDDIT_RSS_GLOBAL_RATE`, `REDDIT_RSS_GLOBAL_BURST`,
`REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS` (Layer 1); `REDDIT_RSS_CACHE_TTL_SECONDS`,
`REDDIT_RSS_CACHE_MAX_ENTRIES` (Layer 2); `REDDIT_RSS_CLIENT_LIMIT`,
`REDDIT_RSS_CLIENT_WINDOW_SECONDS`, `REDDIT_RSS_CLIENT_LIMIT_MAX_TRACKED_CLIENTS`
(Layer 3).

---

## 11. Client code examples

Every example below calls the RSS transport (no credentials needed to run
them as-is) against a service with `API_KEYS` unset; add the header shown in
section 2 if your deployment sets `API_KEYS`.

### Python (`httpx`, async)

```python
import httpx

BASE_URL = "http://localhost:8000"


async def monitor_keywords(keywords: list[str], subreddits: list[str] | None = None) -> dict:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        response = await client.post(
            "/api/reddit/rss/monitor",
            json={"keywords": keywords, "subreddits": subreddits or [], "sort": "new", "time_range": "day"},
            # headers={"X-API-Key": "rsk_xxx"},  # if API_KEYS is set
        )
        response.raise_for_status()
        return response.json()


# asyncio.run(monitor_keywords(["protest", "strike"], ["worldnews"]))
```

### Python (`requests`, sync)

```python
import requests

response = requests.post(
    "http://localhost:8000/api/reddit/rss/monitor",
    json={"keywords": ["protest", "strike"], "subreddits": ["worldnews"], "sort": "new"},
    timeout=30,
)
response.raise_for_status()
data = response.json()
for post in data["posts"]:
    if post["signal"]:
        print(post["title"], post["url"])
```

### JavaScript / TypeScript (`fetch`)

```javascript
async function monitorKeywords(keywords, subreddits = []) {
  const response = await fetch("http://localhost:8000/api/reddit/rss/monitor", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // "X-API-Key": "rsk_xxx",  // if API_KEYS is set
    },
    body: JSON.stringify({ keywords, subreddits, sort: "new", time_range: "day" }),
  });
  if (!response.ok) {
    const { error } = await response.json();
    throw new Error(`${error.code}: ${error.message}`);
  }
  return response.json();
}

monitorKeywords(["protest", "strike"], ["worldnews"]).then((data) => {
  for (const post of data.posts) {
    if (post.signal) console.log(post.title, post.url);
  }
});
```

### Handling pagination generically (OAuth transport)

Any language, same loop shape -- keep calling with the last response's
`cursor` until it comes back `null`:

```python
async def all_posts(client: httpx.AsyncClient, subreddit: str) -> list[dict]:
    posts, cursor = [], None
    while True:
        response = await client.post(
            "/api/reddit/subreddit/posts",
            json={"subreddit": subreddit, "sort": "new", "limit": 100, "cursor": cursor},
        )
        response.raise_for_status()
        body = response.json()
        posts.extend(body["items"])
        cursor = body["cursor"]
        if cursor is None:
            break
    return posts
```

### Generating a typed client instead

If your language has an OpenAPI codegen tool, skip hand-written HTTP calls
entirely:

```bash
curl http://localhost:8000/openapi.json -o reddit-service.json
openapi-generator generate -i reddit-service.json -g typescript-fetch -o ./generated-client
# or: -g python, -g go, -g java, -g csharp, etc.
```

---

## 12. Versioning and compatibility

`GET /openapi.json`'s top-level `info.version` field reports the running
service's version (`SERVICE_VERSION` in `app/api/routes_health.py`; `GET
/health` itself is a bare `{"status": "ok"}` and does not carry a version --
use `/openapi.json` if you need to confirm the build). There is no `/v1/`/`/v2/`
prefix in the URL space today -- the API surface is additive-by-convention:
new optional request fields and new response fields are added without
breaking existing callers (see, for example, `signal`/`strong_keywords` added
to the RSS transport in section 10 -- older callers that don't send them keep
working exactly as before, and just don't see the new response field used).

Practical guidance for your integration:

- **Deserialize leniently.** Don't fail on an unrecognized field in a
  response -- new fields are added over time (most client codegen tools
  default to this already).
- **Treat `error.code` as the stable contract, never `error.message`.** Codes
  are enumerated in sections 3 and 10; messages are free text for humans and
  may be reworded without notice.
- **Pin the OpenAPI spec you generated a client from** (`GET /openapi.json`)
  if you want to detect upstream additions deliberately rather than picking
  them up silently -- diff it against a new deployment's spec before
  regenerating your client.
- Check `GET /openapi.json`'s `info.version` if you need to confirm which
  build a given deployment is running (useful when debugging a behavior
  difference between environments).
