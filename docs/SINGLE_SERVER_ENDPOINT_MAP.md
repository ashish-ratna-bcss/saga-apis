# Single-Server Endpoint Map

Companion to [`docs/SINGLE_SERVER_DEPLOYMENT_PLAN.md`](./SINGLE_SERVER_DEPLOYMENT_PLAN.md). Every endpoint discovered across all four services, with the recommended public/internal URL under the subdomain-based reverse-proxy scheme from that plan's §14. Replace `<domain>` with the operator's real domain.

Internal URL for every row within a service is that service's container DNS name + port, unchanged from what's shown once per service (not repeated per row): `bluweb:8000`, `osint:8000`, `reddit:8000`, `telegram:8000`.

No secrets are shown anywhere below.

---

## Bluweb — `bluweb.<domain>` → internal `bluweb:8000`

Auth on every route: **none**. Recommended nginx timeout for this upstream: 35s default (covers `search/instant`'s internal 30s wait); everything else responds fast or is async (202 + poll).

| Method | Endpoint | Auth | Request | Response | Timeout | Dependencies | Notes |
|---|---|---|---|---|---|---|---|
| GET | `/health` | none | – | `{"status":"ok"}` | default | none | liveness |
| GET | `/health/live` | none | – | `{"status":"alive"}` | default | none | liveness alias |
| GET | `/health/ready` | none | – | `{"status":"ready\|not_ready", db}` | default | Postgres | readiness — use for orchestrator probe |
| GET | `/metrics` | none | – | Prometheus text | default | none | scrape target |
| POST | `/api/v1/preflight` | none | target URL + options | preflight report | 35s | Postgres, DNS, target site, optional Playwright | synchronous, always 201 |
| GET | `/api/v1/preflight/{preflight_id}` | none | – | stored report | default | Postgres | 404 if missing |
| POST | `/api/v1/crawls` | none | crawl spec | `202` job accepted | default (fires background task, returns immediately) | Postgres, Crawlee, httpx/Playwright, MinIO | poll via GET below |
| GET | `/api/v1/crawls` | none | filters | job list | default | Postgres | |
| GET | `/api/v1/crawls/{crawl_id}` | none | – | job status | default | Postgres | poll target |
| GET | `/api/v1/crawls/{crawl_id}/pages` | none | – | per-URL attempts | default | Postgres | |
| POST | `/api/v1/crawls/{crawl_id}/cancel` | none | – | – | default | Postgres, in-process task registry | |
| GET | `/api/v1/documents` | none | filters (crawl_id/source_id/domain/page_type) | doc list, hard-capped 50 | default | Postgres | no pagination param today |
| GET | `/api/v1/documents/{document_id}` | none | – | detail + entity/story ids | default | Postgres | |
| GET | `/api/v1/documents/{document_id}/changes` | none | filters | change events | default | Postgres | |
| GET | `/api/v1/documents/{document_id}/versions` | none | – | version summaries | default | Postgres | no body content |
| GET | `/api/v1/documents/{document_id}/versions/{n}` | none | – | full version incl. `content` | default | Postgres | only place body text returns |
| GET | `/api/v1/documents/{document_id}/diff` | none | two version refs | unified diff lines | default | Postgres | |
| POST | `/api/v1/sources` | none | source spec (needs valid unexpired preflight, same domain) | 201 source | default | Postgres | 400/404/409 on invalid |
| GET | `/api/v1/sources` | none | – | source list | default | Postgres | |
| GET | `/api/v1/sources/{source_id}` | none | – | detail | default | Postgres | |
| PATCH | `/api/v1/sources/{source_id}` | none | name/type/policy | updated source | default | Postgres | |
| DELETE | `/api/v1/sources/{source_id}` | none | – | 204 | default | Postgres | |
| POST | `/api/v1/sources/{source_id}/start` (alias `/resume`) | none | – | – | default | Postgres | |
| POST | `/api/v1/sources/{source_id}/pause` | none | – | – | default | Postgres | |
| GET | `/api/v1/sources/{source_id}/events` | none | – | monitoring events | default | Postgres | |
| GET | `/api/v1/sources/{source_id}/statistics` | none | – | counters | default | Postgres | |
| POST | `/api/v1/search` | none | query | Postgres FTS results | default | Postgres GIN index | |
| POST | `/api/v1/search/instant` | none | query + `wait_seconds` | partial results | **35s** | Postgres, crawl engine | blocks server-side up to 30s |
| GET | `/api/v1/domains/{domain}/profile` | none | – | learning/health/politeness | default | Postgres | 404 if never crawled |
| GET | `/api/v1/domains/{domain}/capabilities` | none | – | capability summary | default | Postgres | |
| GET | `/api/v1/entities` | none | filter | entity list | default | Postgres | |
| GET | `/api/v1/entities/{entity_id}` | none | – | detail | default | Postgres | |
| GET | `/api/v1/entities/{entity_id}/documents` | none | – | linked doc ids | default | Postgres | |
| GET | `/api/v1/entities/{entity_id}/stories` | none | – | linked stories | default | Postgres | |
| GET | `/api/v1/stories` | none | filters | story list | default | Postgres | |
| GET | `/api/v1/stories/{story_id}` | none | – | summary | default | Postgres | |
| GET | `/api/v1/stories/{story_id}/documents` | none | – | attachment scores | default | Postgres | |
| GET | `/api/v1/stories/{story_id}/entities` | none | – | entities | default | Postgres | |
| GET | `/api/v1/stories/{story_id}/sources` | none | – | contributing domains | default | Postgres | |
| GET | `/api/v1/stories/{story_id}/timeline` | none | – | ordered timeline | default | Postgres | |

---

## OSINT — `osint.<domain>` → internal `osint:8000`

Auth on every route: **none** (API-key auth was deliberately removed). Recommended nginx timeout: 300s (covers username-lookup, documented as able to take minutes).

| Method | Endpoint | Auth | Request | Response | Timeout | Dependencies | Notes |
|---|---|---|---|---|---|---|---|
| GET | `/health` | none | – | `{"status":"ok"}` | default | none | liveness |
| GET | `/ready` | none | – | `{"status":"ready"}` or 503 | default | DB `SELECT 1` | readiness |
| GET | `/metrics` | none | – | Prometheus text | default | in-memory counters | |
| GET/POST | `/api/v1/search` | none | query | `SearchResponseOut` | default | self-hosted SearxNG | degrades gracefully if SearxNG down |
| POST | `/api/v1/phone/lookup` | none | phone number | `LookupResultOut` | default | `phonenumbers` lib (offline) | synchronous |
| POST | `/api/v1/email/lookup` | none | email | `LookupResultOut` | default | `holehe` (~140 sites) | synchronous |
| POST | `/api/v1/username/lookup` | none | username | `LookupResultOut` | **300s** | `sherlock`+`maigret` | can take minutes |
| POST | `/api/v1/person/lookup` | none | name | `LookupResultOut` | default | `wikidata` + `public_web` | |
| POST | `/api/v1/domain/lookup` | none | domain | `LookupResultOut` | default | `nslookup`/`whois` subprocess + `public_web` | needs `dnsutils`, optional `whois` |
| POST | `/api/v1/investigations` | none | targets + mode | 201 `InvestigationSummary` | default | worker/job_queue, `Idempotency-Key` header | 409 idempotency conflict, 429 over cap |
| GET | `/api/v1/investigations` | none | – | list, capped 200 | default | DB | |
| GET | `/api/v1/investigations/{id}` | none | – | full detail | default | DB | |
| GET | `/api/v1/investigations/{id}/status` | none | – | lightweight poll | default | DB counts | poll target |
| POST | `/api/v1/investigations/{id}/cancel` | none | – | updated summary | default | DB | 409 if already terminal |
| GET | `/api/v1/investigations/{id}/report` | none | – | case report | default | DB | |
| GET | `/api/v1/investigations/{id}/graph` | none | – | entity graph nodes/edges | default | DB | |
| GET | `/api/v1/investigations/{id}/timeline` | none | – | chronological events | default | DB | |
| PUT | `/api/v1/investigations/{id}/notes` | none | notes text | updated summary | default | DB | |
| GET | `/api/v1/sources/health` | none | – | per-adapter reachability | default | `SourceHealth` table + live SearxNG probe | diagnostics, not a liveness probe |
| POST | `/api/v1/utils/pwned-password` | none | password (k-anonymity prefix) | `{pwned, times_seen}` | default | HIBP public API | only genuine 3rd-party API call |

---

## reddit_server — `reddit.<domain>` → internal `reddit:8000`

Auth: **`X-API-Key` header** (enable via `API_KEYS` — currently optional/off by default, must be turned on for production). Recommended nginx timeout: ≥60s (must exceed `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS`, default 55s, or callers get a proxy 504 instead of the service's own structured timeout error).

| Method | Endpoint | Auth | Request | Response | Timeout | Dependencies | Notes |
|---|---|---|---|---|---|---|---|
| GET | `/health` | none | – | `{"status":"ok"}` | default | none | never call Reddit — safe liveness probe |
| GET | `/ready` | none | – | `{"status":"ok","reddit_configured":bool}` | default | config presence only, no network | safe readiness probe |
| GET | `/api/reddit/status` | X-API-Key | – | connect/auth status | default | live Reddit OAuth call | **never use as a health probe** |
| POST | `/api/reddit/subreddit` | X-API-Key | subreddit name | subreddit JSON | default | Reddit OAuth API | |
| POST | `/api/reddit/subreddit/posts` | X-API-Key | subreddit(s), sort, cursor | `{items,cursor}` | default | Reddit OAuth API | `+`-joined multireddit supported |
| POST | `/api/reddit/subreddit/access` | X-API-Key | subreddit name | `{accessible,state,detail}` | default | Reddit OAuth API | |
| POST | `/api/reddit/post` | X-API-Key | post_id or url | post JSON | default | Reddit OAuth API | |
| POST | `/api/reddit/post/comments` | X-API-Key | post id, cursor | `{items,cursor}` | default | Reddit OAuth API | auto-expands up to 3 "more" stubs |
| POST | `/api/reddit/comment` | X-API-Key | comment id | comment JSON | default | Reddit OAuth API | |
| POST | `/api/reddit/user` | X-API-Key | username | user JSON | default | Reddit OAuth API | |
| POST | `/api/reddit/user/posts` | X-API-Key | username, cursor | `{items,cursor}` | default | Reddit OAuth API | |
| POST | `/api/reddit/user/comments` | X-API-Key | username, cursor | `{items,cursor}` | default | Reddit OAuth API | |
| GET | `/api/reddit/search/posts` | X-API-Key | query, cursor | `{items,cursor}` | default | Reddit OAuth API | |
| GET | `/api/reddit/search/subreddits` | X-API-Key | query, cursor | `{items,cursor}` | default | Reddit OAuth API | |
| GET | `/api/reddit/search` | X-API-Key | `type=posts\|subreddits` | one of the above | default | Reddit OAuth API | `type=comments` → 400 |
| POST | `/api/reddit/resolve` | X-API-Key | Reddit URL | `{kind,subreddit,post?,comment?}` | default | Reddit OAuth API | |
| POST | `/api/reddit/rss/monitor` | X-API-Key + per-caller RSS limit | keyword/subreddit | parsed posts | **60s** | public RSS feed | no Reddit credentials needed |
| GET | `/api/reddit/rss/search` | X-API-Key + per-caller RSS limit | same, GET form | same as `/monitor` | **60s** | public RSS feed | |
| POST | `/api/reddit/rss/event` | X-API-Key + per-caller RSS limit | keyword/subreddit | `/monitor` shape + `event_signal` | **60s** | public RSS feed | fixed-strategy event monitoring |

---

## telegram_poc — `telegram.<domain>` → internal `telegram:8000`

Auth: **none at the API layer** (SEC-01, documented gap). Separate concern: real Telegram user-account auth governs whether the *service itself* can talk to Telegram (see the `/api/telegram/auth/*` handshake below). Recommend nginx-level access control on this whole subdomain regardless (§16 of the deployment plan). Recommended nginx timeout: default is fine except media endpoints (allow normal binary transfer time).

| Method | Endpoint | Auth | Request | Response | Timeout | Dependencies | Notes |
|---|---|---|---|---|---|---|---|
| GET | `/health` | none | – | `{"status":"ok"}` | default | none | liveness |
| GET | `/ready` | none | – | `{"status","telegram_configured"}` | default | config presence only | does **not** confirm session is connected |
| GET | `/console` | none | – | operator HTML console | default | none (same-origin JS) | `include_in_schema=False` |
| GET | `/api/telegram/status` | none | – | connected/authenticated/account | default | live Telegram check | masked phone only |
| POST | `/api/telegram/auth/send-code` | none | phone | – | default | real Telegram SMS/app code | rate-limit-sensitive — no caller auth today |
| POST | `/api/telegram/auth/verify-code` | none | phone, code | may request password | default | Telegram | |
| POST | `/api/telegram/auth/verify-password` | none | 2FA password | – | default | Telegram | password crosses wire as plain JSON — TLS at nginx is mandatory |
| POST | `/api/sources` | none | source ref | registered source | default | Telegram resolve+probe | |
| GET | `/api/sources` | none | – | source list | default | DB | |
| GET | `/api/sources/{id}` | none | – | detail | default | DB | |
| PATCH | `/api/sources/{id}` | none | fields | 200 (**known bug: does not commit**) | default | DB | pre-existing app bug, not introduced by this plan |
| DELETE | `/api/sources/{id}` | none | – | cascading delete | default | DB | no confirmation/soft-delete |
| POST | `/api/sources/{id}/request-access` | none | – | join request submitted | default | Telegram | idempotent |
| GET | `/api/sources/{id}/access-status` | none | – | status | default | DB | |
| POST | `/api/sources/{id}/check-access` | none | – | re-probed status | default | Telegram | |
| POST | `/api/sources/{id}/monitoring/start` | none | – | monitoring started + immediate collection | default | Telegram, scheduler | |
| POST | `/api/sources/{id}/monitoring/stop` | none | – | stopped | default | DB | |
| GET | `/api/sources/{id}/monitoring-status` | none | – | status | default | DB | |
| GET | `/api/sources/{id}/messages` | none | filters | stored messages | default | DB only | never calls Telegram |
| GET | `/api/messages/{id}` | none | – | one message | default | DB only | |
| GET | `/api/messages` | none | filters | cross-source search | default | DB only | **limit param unbounded** (documented gap) |
| GET | `/api/notifications` | none | – | notification list | default | DB | |
| POST | `/api/notifications/{id}/read` | none | – | 200 (**known bug: does not commit**) | default | DB | pre-existing app bug |
| GET | `/api/telegram/search/global` | none | query, cursor | cross-channel results | default | Telegram | |
| POST | `/api/telegram/search/sources` | none | source ids or all | per-source results | default | Telegram | sequential, not parallel |
| POST | `/api/telegram/discovery/extract-links` | none | text | classified t.me links | default | none (regex/parse only) | read-only, no fetch |
| GET | `/api/telegram/channels/search` | none | query | discovered channels | default | Telegram `contacts.search` | no native pagination |
| GET | `/api/telegram/channels/{channel_id}/photo` | none | – | binary `image/jpeg` | default | Telegram RPC, disk cache | |
| GET | `/api/telegram/channels/{channel_id}/search` | none | query, cursor | results | default | Telegram | fresh probe every call |
| GET | `/api/sources/{id}/search` | none | query, cursor | results | default | Telegram | trusts stored access status |
| POST | `/api/telegram/channels/{telegram_id}/register` | none | – | 201 registered source | default | DB | |
| POST | `/api/telegram/search/results/save` | none | result ref | 201 | default | DB | only path a search result is persisted |
| POST | `/api/sources/{id}/backfill` | none | range/limit | 202 job id | default (fires `asyncio.create_task`) | Telegram, DB | **not restart-safe** (documented gap) |
| GET | `/api/sources/{id}/backfill` | none | – | job list | default | DB | |
| GET | `/api/sources/{id}/backfill/{job_id}` | none | – | job status | default | DB | poll target |
| POST | `/api/telegram/bots/{id}/start` | none | – | harvested links from bot reply | default | Telegram (sends `/start` as the user account) | not a hosted bot — uses the user session as a client |
| POST | `/api/telegram/invites/join` | none | invite hash/link | join result | default | Telegram | |
| POST | `/channel` (provider API) | none | channel ref | metadata | default | Telegram | storage-decoupled, never touches app DB |
| POST | `/channel/messages` | none | channel ref, cursor | recent messages | default | Telegram | |
| POST | `/message` | none | url or (channel_id,message_id) | message JSON | default | Telegram | 404 on miss |
| POST | `/message/replies` | none | message ref | comment thread | default | Telegram | requires discussion group |
| GET | `/search/messages` | none | query | results | default | Telegram | |
| GET | `/search/channels` | none | query | results | default | Telegram | |
| POST | `/resolve` | none | t.me link | parsed reference | default | Telegram | |
| POST | `/channel/access` | none | channel ref | accessibility | default | Telegram | |
| POST | `/invite/join` | none | invite | join result | default | Telegram | |
| GET | `/channels/{channel_id}/messages/{message_id}/media` | none | – | binary media, disk cache | default | Telegram | 404/413 on miss/oversize |

Note: the provider-API rows (`/channel`, `/channel/messages`, `/message`, `/message/replies`, `/search/messages`, `/search/channels`, `/resolve`, `/channel/access`, `/invite/join`, the media-by-message route) are mounted under the `/api/telegram` prefix in the actual app (per `routes_provider.py`'s `APIRouter(prefix="/api/telegram")`) — shown here without the prefix only where the discovery report's own path listing did; treat the effective public path as `telegram.<domain>/api/telegram/<path>` for all provider-API rows, consistent with every other row in this table.

---

## Cross-service collision check

Bluweb's `/health`, `/metrics`, `/api/v1/*` and OSINT's `/health`, `/metrics`, `/api/v1/*` are byte-for-byte identical path shapes. Under the subdomain scheme above (`bluweb.<domain>` vs `osint.<domain>`) this is a non-issue — each lives on its own origin. Do **not** attempt to merge these two under one path-routed origin without a full nginx prefix-rewrite pass (see deployment plan §14 for why that's the fallback, not the default).
