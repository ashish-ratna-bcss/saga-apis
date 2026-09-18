# Reddit Provider Service

A standalone FastAPI service with **two independent Reddit transports**:

- `/api/reddit/*` -- fetches public Reddit data (subreddits, posts, comments,
  users, search) through the **official Reddit OAuth2 API** and returns stable,
  normalized JSON to SOC Eye. Requires Reddit OAuth credentials.
- `/api/reddit/rss/*` -- keyword/event monitoring through Reddit's **public,
  unauthenticated RSS/Atom feeds**. Requires no Reddit credentials at all; see
  [INTEGRATION.md, section 10](INTEGRATION.md#10-unauthenticated-rss-transport).

It is a self-contained module: no Telegram/Discord code, models, credentials or
dependencies are involved anywhere, and it follows the same provider conventions
(config, HTTP structure, auth, logging, error handling, pagination, tests) already
established by [../telegram/telegram_service](../../telegram/telegram_service) and
[../Discord/discord_service](../../Discord/discord_service).

---

## Table of contents

1. [Purpose and non-purpose](#1-purpose-and-non-purpose)
2. [Architecture](#2-architecture)
3. [Creating a Reddit app](#3-creating-a-reddit-app)
4. [Environment configuration](#4-environment-configuration)
5. [Running locally](#5-running-locally)
6. [Running the tests](#6-running-the-tests)
7. [Endpoint reference](#7-endpoint-reference)
8. [Known Reddit API limitations](#8-known-reddit-api-limitations)
9. [Security notes](#9-security-notes)

See [INTEGRATION.md](INTEGRATION.md) for the consumer-facing guide (auth, errors,
pagination, request/response examples for every endpoint) -- that's the document to
hand to whoever wires SOC Eye's scheduler up to this service.

---

## 1. Purpose and non-purpose

This service owns:

- Reddit OAuth2 authentication/session (application-only or a service account)
- Fetching Reddit data (subreddits, posts, comments, users)
- Searching public Reddit content (posts and subreddits)
- Resolving subreddits/posts/comments/users, including from raw URLs
- Returning stable, normalized JSON
- Pagination over Reddit's own listings
- Reddit API errors and rate limits (retries, backoff, proactive throttling)

It additionally owns (via the RSS transport, `/api/reddit/rss/*`):

- Keyword/event monitoring through Reddit's public RSS/Atom feeds, no Reddit
  OAuth credentials required
- A stateless, single-response "activity signal" over a fixed event-monitoring
  strategy (subreddit + keyword defaults, overridable)

This service explicitly does **not** own, and has no endpoints for (either
transport):

- Monitoring start/stop lifecycle
- Polling schedule
- Deduplication
- Storage
- Alerts
- UI
- Detection/analysis

All of that is SOC Eye's job. SOC Eye calls `POST /api/reddit/subreddit/posts` or
`GET /api/reddit/search/posts` on its own schedule; this service never runs a
background poll loop and has nothing resembling `/monitoring/start`.

---

## 2. Architecture

Strict layering; each layer only talks to the one below it.

```
HTTP  ─►  app/api/routes_*.py         validation and orchestration only
          app/api/deps.py             per-request service construction
            │
            ├── OAuth transport ──────────────────────────────────────────┐
            │     app/services/provider_service.py   business logic       │
            │       (normalization, pagination, error translation)        │
            │     app/reddit/rest_client.py   the ONLY component that     │
            │       calls oauth.reddit.com over HTTP                      │
            │     app/reddit/normalize.py     raw Reddit "Thing" JSON ->  │
            │       stable SOC Eye shapes                                 │
            │                                                             │
            └── RSS transport (no OAuth) ─────────────────────────────────┤
                  app/services/reddit_rss_service.py   business logic     │
                    (query building, keyword matching, event signal)      │
                  app/reddit/rss_client.py   the ONLY component that      │
                    calls www.reddit.com's public .rss endpoints          │
                  app/reddit/rss_parser.py   raw Atom XML -> the same     │
                    kind of stable, normalized shape                      │
                  app/reddit/rss_urls.py     RSS URL/query construction   │
                                                                           │
          app/reddit/client.py        composition root / lifecycle owner for both
          app/reddit/urls.py          subreddit-name/URL parsing (pure functions,
                                       shared by both transports)
          app/reddit/pagination.py    opaque cursor encode/decode (OAuth transport)
```

No database. Every call reads live from Reddit and returns a plain dict -- nothing is
persisted, matching the storage-decoupled provider contract this task specifies. That
applies to both transports.

### Responsibilities

| Component | Responsibility |
|---|---|
| `RedditRestClient` | Every OAuth Reddit HTTP call: token management, rate limits, retries, error translation. |
| `RedditRssClient` | Every RSS Reddit HTTP call: no token, bounded retries, size cap, error translation. |
| `RedditClientManager` | Composition root; owns both clients' start/close lifecycle (`.rest` and `.rss`). |
| `ProviderService` | One method per SOC Eye OAuth endpoint; calls `RedditRestClient`, normalizes, maps errors. |
| `RedditRssService` | Query building, keyword matching, dedup, event signal; calls `RedditRssClient`. |
| `normalize.py` | Pure functions: Reddit's on-the-wire fields -> the stable Post/Comment/Subreddit/User shapes (OAuth). |
| `rss_parser.py` | Pure functions: Reddit's Atom XML -> the stable RSS Post shape. |
| `urls.py` | Pure functions: subreddit-name normalization, Reddit URL classification -- shared by both transports. |
| `rss_urls.py` | Pure functions: RSS-specific URL/query construction and validation. |
| `pagination.py` | Opaque cursor encode/decode shared by every OAuth list endpoint. |

---

## 3. Creating a Reddit app

Only needed for the OAuth transport (`/api/reddit/*`). Skip this section entirely
if you only need `/api/reddit/rss/*` -- it needs no Reddit app, no credentials.

1. Go to <https://www.reddit.com/prefs/apps> (sign in first).
2. Click **create app** / **create another app**.
3. Choose a type:
   - **script** -- if you will also set `REDDIT_USERNAME`/`REDDIT_PASSWORD` (the
     service authenticates as that Reddit account; higher, account-scoped rate limit).
   - **web app** -- if you will leave `REDDIT_USERNAME`/`REDDIT_PASSWORD` unset (the
     service authenticates application-only via `client_credentials`; no specific
     account, still fully able to read public subreddits/posts/comments/users).
4. Note the client id (under the app name) and client secret.
5. Set a real, descriptive `REDDIT_USER_AGENT` -- Reddit throttles or blocks generic
   user agents. Recommended format: `<platform>:<app id>:<version> (by /u/<you>)`.

---

## 4. Environment configuration

Copy [.env.example](.env.example) to `.env` and fill in `REDDIT_CLIENT_ID`,
`REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, and optionally `REDDIT_USERNAME`/
`REDDIT_PASSWORD` -- these configure the OAuth transport only. Every setting is
documented inline there and in [app/core/config.py](app/core/config.py).

The RSS transport (`/api/reddit/rss/*`) needs none of the above and works with an
empty `.env`; its own settings (`REDDIT_RSS_*`, all optional with working defaults)
are documented in the same two places.

---

## 5. Running locally

```bash
python -m venv .venv
.venv/Scripts/activate   # or: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in your Reddit credentials
python run.py            # serves on http://0.0.0.0:8000
```

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/api/reddit/status
```

Interactive docs: `GET /docs` (Swagger UI), `GET /openapi.json`.

---

## 6. Running the tests

No real Reddit credentials, server or network access are required -- every test runs
against [tests/fake_reddit.py](tests/fake_reddit.py) (OAuth transport: `www.reddit.com`
tokens + `oauth.reddit.com` API) and [tests/fake_reddit_rss.py](tests/fake_reddit_rss.py)
(RSS transport: `www.reddit.com`'s public `.rss` endpoints), both in-memory stand-ins.

An optional live smoke test against the real Reddit RSS host is skipped by default
(see [tests/test_reddit_rss_live_smoke.py](tests/test_reddit_rss_live_smoke.py));
enable with `RUN_REDDIT_RSS_INTEGRATION=1 pytest`.

```bash
python -m pytest
```

---

## 7. Endpoint reference

See [INTEGRATION.md](INTEGRATION.md) for the full reference with example
requests/responses for every endpoint. Summary:

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process liveness |
| GET | `/ready` | Configuration check |
| GET | `/api/reddit/status` | Live connection/auth check |
| POST | `/api/reddit/subreddit` | Subreddit metadata |
| POST | `/api/reddit/subreddit/posts` | Subreddit posts (SOC Eye polling target) |
| POST | `/api/reddit/subreddit/access` | Subreddit accessibility check |
| POST | `/api/reddit/post` | Post details |
| POST | `/api/reddit/post/comments` | Post's comment tree |
| POST | `/api/reddit/comment` | Single comment |
| GET | `/api/reddit/search/posts` | Global public post discovery |
| GET | `/api/reddit/search/subreddits` | Subreddit discovery |
| GET | `/api/reddit/search` | Unified search (`type=posts\|subreddits`) |
| POST | `/api/reddit/user` | User profile |
| POST | `/api/reddit/user/posts` | Posts submitted by a user |
| POST | `/api/reddit/user/comments` | Comments made by a user |
| POST | `/api/reddit/resolve` | Classify/resolve a Reddit URL |
| POST | `/api/reddit/rss/monitor` | Keyword/event monitoring via public RSS (no Reddit credentials) |
| GET | `/api/reddit/rss/search` | GET convenience form of `/rss/monitor` |
| POST | `/api/reddit/rss/event` | Event monitoring: predefined subreddit/keyword strategy + activity signal |

---

## 8. Known Reddit API limitations

Documented in full, with the reasoning, in
[INTEGRATION.md, section 8](INTEGRATION.md#8-known-reddit-api-limitations). Short version:

- **No free-text search across all comments.** Reddit's public search API indexes
  posts and subreddits, not arbitrary comments. `type=comments` on `GET
  /api/reddit/search` returns a structured `UNSUPPORTED_SEARCH` error rather than
  fabricating results. Use `POST /api/reddit/post/comments` for a specific post.
- **Very large comment threads may not be fully expanded.** Reddit truncates huge
  threads behind "more comments" stubs; this service follows up to 3 of those
  automatically per request (bounded to protect the rate limit and response latency).
  `parent_id` is always preserved for whatever is returned, so SOC Eye can reconstruct
  the tree from however many comments come back.
- **Quarantined subreddits are not readable by this service.** Reddit requires an
  interactive, logged-in user to click through the quarantine interstitial; an
  application-only or non-interactive service account cannot do that. `CHECK_SUBREDDIT_ACCESS`
  reports `state: "quarantined"`, `accessible: false` rather than silently failing.
- **`USER_INFO` only returns what Reddit's public `/user/{username}/about` exposes.**
  No e-mail, no verified status, no private data -- even in an authenticated session,
  unless the token's own owner is being queried.

**RSS transport (`/api/reddit/rss/*`) limitations** -- full detail in
[INTEGRATION.md, section 10](INTEGRATION.md#10-unauthenticated-rss-transport):
no historical dedup or trend detection (stateless, single-response only); subject
to Reddit's own RSS availability/rate limits (Reddit cut unauthenticated RSS to
~1 req/min per feed in June 2026 -- this client throttles from Reddit's own
response headers automatically, and `REDDIT_RSS_USER`/`REDDIT_RSS_FEED` restore
the old ceiling if set); and `event_signal` is a stateless activity hint, never a
confirmed real-world event.

**This transport is a shared gateway, not a per-caller proxy.** Many callers
hitting one process share one Reddit-facing IP's budget, so three protection
layers sit in front of Reddit: a per-caller request limiter, a shared raw-feed
cache with request coalescing (N callers requesting the same feed cause one
Reddit fetch), and a proactive process-wide rate limiter that paces every
outbound attempt (retries included) ahead of Reddit's own throttling. All
three are process-local -- a multi-worker/multi-instance deployment needs a
distributed backend (not included) to share one true budget. See
[INTEGRATION.md's shared-gateway architecture](INTEGRATION.md#10-unauthenticated-rss-transport).

---

## 9. Security notes

- Reddit's client secret, account password, and access token are held in
  `pydantic.SecretStr` / kept in memory only, registered with the log redaction filter
  (`app/core/logging.py`), and never appear in any API response, including
  `/api/reddit/status` (which reports only a masked username).
- `API_KEYS` (a shared `X-API-Key` header) gates every `/api/reddit/*` route; it is
  empty (open) by default for local development, and the service logs a loud warning
  at startup when that's the case. Set it before exposing this service beyond
  localhost.
- `/health` and `/ready` never require the API key, so uptime probes work without a
  secret, and neither reveals anything beyond liveness/configuration status.
