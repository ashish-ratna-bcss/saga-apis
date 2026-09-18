# 1. Executive Summary

**Service name:** Telegram OSINT Collection Service (`telegram_service`)
**Codebase version:** `0.1.0` (FastAPI `app.title = "Telegram OSINT Collection Service"`)
**Analysis basis:** static read of the complete source tree, the SQLite database produced by the running service, and the test suite. Where a document and the code disagreed, the **code was taken as the source of truth** and the discrepancy is recorded in section 18.

### What this service is

A **backend HTTP service that turns one authenticated Telegram user account into a controlled, auditable data-collection API.** It authenticates a single Telegram account over MTProto (Telethon), registers Telegram channels/groups/bots as "sources", determines each source's *real* access status by asking Telegram rather than assuming it, runs a legitimate join-request workflow for private sources, searches Telegram live, and collects messages incrementally into a local SQLite database once — and only once — access has been positively verified.

It is explicitly the **data-collection and access-management layer** of a larger OSINT platform. NLP, OCR, entity extraction, correlation, risk scoring and case management are **Not Implemented** here and are expected to consume this service's normalized tables downstream.

### Business / technical objective

| Objective | How the implementation meets it |
| --- | --- |
| Collect Telegram content lawfully, without bypassing Telegram's access controls | Every read is performed as a normal authenticated user account. Private content is only reached after Telegram itself approves a join request. |
| Never guess an access status | `check_access` performs a real Telegram read probe (or `CheckChatInviteRequest`) and drives an explicit 13-state machine. |
| Be restart-safe and non-duplicating | Per-message commits, a per-source checkpoint, and a DB-level `UNIQUE(source_id, telegram_message_id)` constraint. |
| Be defensible / auditable | Every significant operation writes an `audit_logs` row; every collected message's raw Telegram payload is preserved to disk and SHA-256 hashed. |
| Protect the Telegram account | Bounded page sizes, a global search-concurrency cap of 3, in-flight request de-duplication, `max_instances=1` on all background jobs, no retry-on-FloodWait loops, exactly one join request per source. |

### Telegram functionality currently supported

| Capability | Status |
| --- | --- |
| Non-interactive account login (code + 2FA) and session reuse | Implemented |
| Source registration with identifier normalization and double de-duplication | Implemented |
| Live access probing and a validated access state machine | Implemented |
| Join public channel / import invite link / submit approval-gated join request | Implemented |
| 12-hour join-request reconciliation with approval and rejection detection | Implemented |
| Incremental forward monitoring with checkpointing | Implemented |
| Historical backward backfill as a background job | Implemented |
| Live Telegram-wide message search (`channels.searchPosts` with `messages.searchGlobal` fallback) | Implemented, quota-limited by Telegram |
| Live per-channel message search (`messages.search`) | Implemented |
| Channel/group discovery by keyword (`contacts.search`) | Implemented |
| Telegram-link extraction and classification from free text | Implemented |
| Bot `/start` interaction and link harvesting from the reply | Implemented |
| Media metadata capture; optional policy-gated media download | Implemented (download **off** by default) |
| Stored-message retrieval and flat-filter search over collected data | Implemented |
| Persisted notifications with pluggable outbound dispatch | Implemented (no dispatcher registered by default) |
| Sending arbitrary messages, posting, reacting, editing, deleting | **Not Implemented** |
| Any bypass of Telegram permissions, invite guessing, or membership spoofing | **Not Implemented — explicitly out of scope** |

### High-level request flow

```text
HTTP client
    |
    v
FastAPI router  (app/api/routes_*.py)          thin: validation + status codes only
    |
    v
Service layer   (app/services/*.py)            business rules, access rules, audit
    |
    +---------------------------+
    |                           |
    v                           v
Telegram layer                Repository layer
(app/telegram/*, Telethon)    (app/database/repositories/*)
    |                           |
    v                           v
Telegram MTProto API          SQLite  (DATABASE_URL, swappable)
```

### External Telegram APIs and libraries used

| Item | Value |
| --- | --- |
| Protocol | Telegram **MTProto** (not the HTTP Bot API) |
| Client library | **Telethon** `>=1.36.0` |
| Account type | **One Telegram *user* account** (api_id / api_hash from `my.telegram.org/apps`) |
| Telegram RPCs used | `contacts.resolveUsername` / `channels.getChannels` (via `get_entity`), `messages.getHistory` / `messages.search` (via `iter_messages`), `messages.searchGlobal`, `channels.searchPosts`, `channels.checkSearchPostsFlood`, `contacts.search`, `messages.checkChatInvite`, `messages.importChatInvite`, `channels.joinChannel`, `messages.sendMessage` (bot `/start` only), `auth.sendCode` / `auth.signIn` |
| Web framework | FastAPI + Uvicorn |
| Persistence | SQLAlchemy 2.x async ORM over SQLite (`aiosqlite`) |
| Scheduling | APScheduler `AsyncIOScheduler`, embedded in the API process |

### Major limitations (detail in sections 7, 8 and 18)

1. **The service has no authentication of its own.** Every one of its 35 endpoints is unauthenticated, and the default bind address is `0.0.0.0`. See finding **SEC-01** (High).
2. **"Global search" is not a search of all of Telegram.** `channels.searchPosts` is limited by a small free daily quota (then Telegram Stars, which this service will never spend); the fallback `messages.searchGlobal` is scoped to what Telegram's index associates with this account.
3. **Access is exactly this one account's access** — nothing more. Private sources stay unreadable until Telegram approves a join request.
4. **A silently declined join request is indistinguishable from a pending one** for username-based requests; Telegram exposes no decline signal to the requester.
5. **The event-driven access-change fast path described in the README is not wired into the running application** (`register_access_change_handlers` is never called). Only the 12-hour reconciliation job is live. See **DOC-01**.
6. **Backfill jobs do not survive a process restart** — they run as in-process `asyncio` tasks with no resumption on startup. See **ISS-02**.
7. **No Alembic migrations**; schema evolution relies on `create_all` plus a hand-maintained additive-column patch list for SQLite only.

<!--PAGEBREAK-->

# 2. System Architecture

## 2.1 Layer model

The codebase enforces a strict, one-directional dependency rule that was verified by inspection:

* **Routes never import Telethon and never build SQL.** They depend only on a service, injected through `app/api/deps.py`.
* **Services** own all business rules and are the only layer that combines Telegram operations with persistence.
* **`app/telegram/*` is the only package that imports Telethon.** `app/telegram/client.py` is the only module that constructs a `TelegramClient`.
* **`app/database/repositories/*` is the only code that builds queries.** Swapping `DATABASE_URL` from SQLite to PostgreSQL therefore requires no change above the repository layer (with the caveat in section 18 about the SQLite-specific `ON CONFLICT` insert).

```text
                         HTTP client (curl / Postman / platform service)
                                          |
                                          v
+---------------------------------------------------------------------------+
|  API layer            app/main.py            FastAPI app, lifespan, /health |
|                       app/api/routes_*.py    8 routers, 35 endpoints        |
|                       app/api/deps.py        dependency wiring              |
|                       app/schemas/*.py       Pydantic request/response DTOs |
+---------------------------------------------------------------------------+
                                          |  services only
                                          v
+---------------------------------------------------------------------------+
|  Service layer        source_service      registration, access, joins       |
|                       monitoring_service  start/stop, collection cycle      |
|                       search_service      live search orchestration         |
|                       message_service     persistence, dedup, checkpoints   |
|                       backfill_service    historical background jobs        |
|                       bot_service         bot /start workflow               |
|                       notification_service, audit_service                   |
+---------------------------------------------------------------------------+
           |                                              |
           v                                              v
+------------------------------------+   +---------------------------------------+
|  Telegram integration               |   |  Database layer                       |
|  client.py        connection/session|   |  database.py     async engine/session |
|  authentication.py login steps      |   |  models.py       11 ORM tables        |
|  access_manager.py state machine    |   |  repositories/   10 repositories      |
|  join_request_manager.py joins      |   +---------------------------------------+
|  collector.py     message walking   |                    |
|  bot_interaction.py /start          |                    v
|  discovery/       search + parsing  |              SQLite (data/telegram_service.db)
|  rate_policy.py   concurrency/dedup |              data/raw_messages/<src>/<id>.json
|  errors.py        error translation |              data/media/ (optional)
|  event_handler.py NOT WIRED IN      |
+------------------------------------+
           |
           v
   Telegram MTProto API
           |
           v
+---------------------------------------------------------------------------+
|  Background jobs (APScheduler, in-process)                                 |
|  reconcile_pending_access (12h) | run_collection_cycle (5m)                 |
|  connection_health_check (5m)   | process_notifications (1m)                |
+---------------------------------------------------------------------------+
```

## 2.2 Component reference

| Component | Files | Responsibility |
| --- | --- | --- |
| **API layer** | `app/main.py`, `app/api/routes_*.py` | Publishes 35 endpoints across 8 routers. Registers one global exception handler for `TelegramSearchError`. Owns the lifespan: DB init, first Telegram connect, scheduler start/stop, client disconnect. |
| **Router/controller** | 8 modules: `routes_auth`, `routes_sources`, `routes_monitoring`, `routes_messages`, `routes_notifications`, `routes_search`, `routes_bots`, `routes_backfill` | Translate service exceptions into HTTP status codes (`404/409/422`), shape responses, declare OpenAPI metadata. No business logic. |
| **Dependency wiring** | `app/api/deps.py` | Builds each service with a request-scoped `AsyncSession`, the process-wide client manager, and `Settings`. Routes receive services, never sessions or Telethon objects. |
| **Service / business logic** | `app/services/*.py` | Access rules (`SEARCHABLE_STATUSES`, `BACKFILLABLE_STATUSES`), state-machine enforcement, de-duplication, audit writes, error translation, structured logging. |
| **Telegram integration** | `app/telegram/*` | `TelegramClientManager` (singleton connection + invalid-session latch), non-interactive login, access probing, join submission/polling, message normalization, search RPCs, URL classification, request concurrency policy, error mapping. |
| **Database layer** | `app/database/database.py`, `models.py`, `repositories/` | Async engine/sessionmaker, `create_all` bootstrap plus a SQLite-only additive-column patcher, 11 ORM models, 10 repositories. |
| **Models / entities** | `app/database/models.py` | 11 tables and 7 enums (`SourceType`, `SourceAccessStatus`, `AccessRequestStatus`, `MediaType`, `ProcessingStatus`, `CollectionMethod`, `BackfillJobStatus`). |
| **Schemas / DTOs** | `app/schemas/*.py` | Pydantic v2 models. Search DTOs are hand-mapped from Telegram-layer dataclasses in `routes_search.py` so ORM models are never serialized directly. |
| **Configuration** | `app/config.py`, `.env` | `pydantic-settings` `Settings`, cached with `lru_cache`. No secret has a hard-coded default. |
| **Authentication / session** | `app/telegram/client.py`, `authentication.py` | One Telethon session file per service instance. A latched `session_invalid` flag short-circuits every Telegram-requiring operation until re-authentication succeeds. **There is no application-level (caller) authentication.** |
| **Background jobs** | `app/scheduler/jobs.py` | Four APScheduler interval jobs, all `max_instances=1`, `coalesce=True`, each opening its own DB session. |
| **Logging / auditing** | `app/observability.py`, `app/services/audit_service.py` | `log_structured` emits single-line JSON events. `AuditService.log` writes an `audit_logs` row for ~30 declared event types. |
| **Error handling** | `app/telegram/errors.py` | `translate_telegram_error` maps Telethon exceptions to a stable `{code, message, retryable, retry_after_seconds}` payload with an appropriate HTTP status. |
| **Notifications** | `app/services/notification_service.py`, `app/notifications/manager.py` | Notifications are always persisted first; outbound dispatch is a pluggable registry with **no provider registered by default**. |

## 2.3 Runtime shape

* **One process.** Uvicorn serves the API, the APScheduler jobs run on the same event loop, and a single Telethon MTProto connection is shared process-wide (`get_client_manager()` singleton).
* **One Telegram account.** There is no multi-account pool, rotation, or proxy support anywhere in the codebase.
* **Backfill runs as `asyncio.create_task`** inside the same process — there is no external worker or queue.

<div class="callout warn">
<strong>Architectural consequence:</strong> because the Telethon client, the API and the scheduler share one process and one account, API load and background collection compete for the same Telegram rate budget. The only in-code guard is a semaphore of 3 concurrent <em>search</em> RPCs (<code>rate_policy.SEARCH_CONCURRENCY_LIMIT</code>); collection, access checks and joins are not covered by it.
</div>

<!--PAGEBREAK-->

# 3. Telegram API Integration

## 3.1 Two APIs, clearly separated

Throughout this document:

* **Application API** — the 35 REST endpoints this service publishes (`/api/...`). Documented in sections 4 and 5.
* **Telegram API** — Telegram's MTProto methods that the service calls on the caller's behalf, through Telethon.

The two are connected in exactly one place: a **service** method calls a function in `app/telegram/*`, which issues the MTProto request using the shared authenticated client. No route ever reaches Telegram directly.

```text
Application API                Service                 Telegram layer            Telegram API (MTProto)
-------------------------------------------------------------------------------------------------------
POST /api/sources            SourceService         access_manager.check_access   contacts.resolveUsername
                             .register_source                                    + messages.getHistory(1)
POST /api/sources/{id}/      SourceService         join_request_manager          channels.joinChannel  OR
     request-access          .request_access       .submit_join_request          messages.importChatInvite
POST /api/telegram/          SourceService         join_request_manager          messages.checkChatInvite
     invites/join            .join_by_invite                                     + messages.importChatInvite
GET  /api/telegram/          SearchService         discovery.global_search       channels.checkSearchPostsFlood
     search/global           .global_search                                      -> channels.searchPosts
                                                                                 -> messages.searchGlobal
GET  /api/sources/{id}/      SearchService         discovery.channel_search      messages.search
     search                  .channel_search_by_source
GET  /api/telegram/          SearchService         discovery.channel_discovery   contacts.search
     channels/search         .discover_channels
POST /api/telegram/          BotService            bot_interaction.start_bot     messages.sendMessage
     bots/{id}/start         .start_bot                                          + messages.getHistory
POST /api/sources/{id}/      BackfillService       collector                     messages.getHistory
     backfill                .start_backfill       .collect_historical_messages   (backward walk)
(scheduled)                  MonitoringService     collector                     messages.getHistory
                             .run_collection_for_source .collect_new_messages     (forward, min_id)
GET  /api/messages           MessageService        -- none --                    NONE (local SQLite only)
```

## 3.2 Telegram API type and client

| Aspect | Implementation |
| --- | --- |
| API type | **MTProto user API.** The HTTP Bot API is not used anywhere. |
| Library | Telethon (`telethon>=1.36.0`) |
| Client construction | Only in `TelegramClientManager._build_client()`; raises `TelegramNotConfiguredError` if `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` are absent. |
| Instance scope | Process-wide singleton via `get_client_manager()`; guarded by an `asyncio.Lock` so concurrent first-time connects cannot race. |
| Session storage | A Telethon SQLite session file at `TELEGRAM_SESSION_PATH` (default `data/telegram_service.session`). Parent directory is created automatically. |

## 3.3 Authentication mechanism

The service authenticates **as a human Telegram user account**, not as a bot. `api_id`/`api_hash` identify the *application*; the phone-number login identifies the *account* whose permissions all collection then inherits.

Login is split into explicit HTTP steps because a long-running server cannot block on `input()` the way a CLI can:

```text
POST /api/telegram/auth/send-code       -> auth.sendCode        (stores phone_code_hash in memory only)
POST /api/telegram/auth/verify-code     -> auth.signIn          (may report requires_password=true)
POST /api/telegram/auth/verify-password -> auth.checkPassword   (only when 2FA is enabled)
GET  /api/telegram/status               -> confirms + returns masked identity
```

`pending_phone` and `pending_phone_code_hash` live **only in memory** on the client manager and are cleared on success. They are never persisted or logged. After a successful sign-in the Telethon session file is authorized and every restart reconnects with no further login.

## 3.4 Session management and the invalid-session latch

This is one of the more deliberate parts of the design and matters for account safety.

* `verify_authorized()` is a mandatory pre-flight before **every** Telegram-requiring operation (entity resolution, access checks, search, monitoring, backfill, bot start). It connects if needed and confirms `is_user_authorized()`.
* When any code path observes an error in `SESSION_INVALID_ERRORS` (`AuthKeyUnregisteredError`, `AuthKeyInvalidError`, `AuthKeyPermEmptyError`, `SessionExpiredError`, `SessionRevokedError`, `UserDeactivatedError`, `UserDeactivatedBanError`, `ActiveUserRequiredError`), the manager **latches** `session_invalid = True`.
* While latched, every Telegram-requiring operation short-circuits **without issuing another RPC**, returning `TELEGRAM_SESSION_INVALID` / HTTP 401. This is what stops the service from hammering Telegram with a key Telegram has already rejected.
* Only a successful `verify-code` or `verify-password` clears the latch.
* `SessionPasswordNeededError` is deliberately **excluded** from that set — it is a normal step of sign-in, not an invalidated session.

## 3.5 Connection lifecycle

| Phase | Behaviour |
| --- | --- |
| Startup (`lifespan`) | `init_db()` -> connect if configured (a failure is logged as a warning, never fatal) -> `setup_scheduler().start()`. |
| Steady state | `connection_health_check` runs every `CONNECTION_HEALTH_INTERVAL_MINUTES` (default 5) and calls `ensure_connected()` — **one** attempt per run, never an internal retry loop. It also upserts the single `telegram_accounts` row. |
| Shutdown | `scheduler.shutdown(wait=False)` then `client_manager.disconnect()`. |

## 3.6 How responses are processed

1. **Messages** are converted by `collector.normalize_message()` into a `NormalizedMessage` dataclass: id, sender id/username/display name, dates, text, views/forwards/reply count, `grouped_id`, media classification, permalink (`https://t.me/<username>/<id>`, `None` without a username), plus the full `message.to_dict()` payload and its SHA-256 hash. **Fields Telegram does not expose are left `None`, never inferred.**
2. **Search results** are converted into `TelegramSearchResultItem` dataclasses, then hand-mapped to Pydantic DTOs in `routes_search.py`. Each result carries `visibility` (`public` / `account_scoped` / `member`) and `collection_method`, so a consumer can tell *how* the service saw it.
3. **Entities** resolved during an access check are cached in `telegram_entities` — explicitly a resolution cache, **never treated as proof of access**.
4. **Raw evidence** for every persisted message is written to `data/raw_messages/<source_id>/<telegram_message_id>.json` before the row is inserted.

## 3.7 How Telegram errors are handled

Two distinct strategies, chosen by whether the caller has a source record to fall back on:

**(a) Domain-state mapping** — `access_manager`, `join_request_manager` and `collector` catch Telethon errors and convert them into `SourceAccessStatus` values or an `error` string on an outcome dataclass. Nothing raises through to the route; the source's `status_reason` and the checkpoint's `error_count` record what happened.

**(b) Stable API error translation** — the search/discovery/backfill paths have no source state machine, so `errors.translate_telegram_error()` maps the exception to a `TelegramSearchError` carrying `code`, `message`, `http_status`, `retryable`, `retry_after_seconds`. `app/main.py` registers a global handler that renders it as JSON. The full mapping is in section 16.

<div class="callout info">
<strong>Credential hygiene in errors:</strong> every error message is built from the exception <em>class name</em> plus Telegram's own public error text. <code>api_hash</code>, session contents, verification codes and 2FA passwords never appear in an error, a log line, or an API response — this was verified across <code>client.py</code>, <code>authentication.py</code>, <code>errors.py</code> and <code>observability.py</code>.
</div>
<!--PAGEBREAK-->

# 4. Endpoint Inventory

**35 endpoints** across 10 functional groups. This table was derived from the router source files and cross-checked against `tests/test_endpoint_coverage.py`, which fails the build if the set of endpoints the app publishes differs from the set the test exercises.

<div class="callout crit">
<strong>Authentication column:</strong> "None" is literal. There is no API key, bearer token, session cookie, CORS policy or IP allow-list anywhere in the codebase — a grep for <code>api_key</code>, <code>HTTPBearer</code>, <code>OAuth2</code>, <code>jwt</code>, <code>middleware</code> and <code>CORS</code> across <code>app/</code> returns nothing. The "Telegram session" entries below mean the endpoint <em>requires the service's own Telegram account to be authorized</em>, not that the caller is authenticated. See finding SEC-01.
</div>

### 4.1 Health / Status

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/health` | Liveness probe | None | — | `{"status":"ok"}` | Implemented |
| GET | `/ready` | Readiness; reports whether Telegram credentials are configured | None | — | `{"status","telegram_configured"}` | Implemented |

### 4.2 Telegram Session / Authentication

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/telegram/status` | Connection + authorization state, masked account identity | None (caller) | — | `TelegramStatusResponse` | Implemented |
| POST | `/api/telegram/auth/send-code` | Step 1 of login: request an SMS/app code | None (caller) | `SendCodeRequest` | `AuthStepResponse` | Implemented |
| POST | `/api/telegram/auth/verify-code` | Step 2: sign in with the code | None (caller) | `VerifyCodeRequest` | `AuthStepResponse` | Implemented |
| POST | `/api/telegram/auth/verify-password` | Step 3: 2FA password (only if required) | None (caller) | `VerifyPasswordRequest` | `AuthStepResponse` | Implemented |

### 4.3 Source Management

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/sources` | Register a source; resolves and probes access immediately | None + Telegram session | `SourceCreate` | `SourceOut` (201) | Implemented |
| GET | `/api/sources` | List registered sources | None | — | `SourceOut[]` | Implemented |
| GET | `/api/sources/{source_id}` | Fetch one source | None | — | `SourceOut` | Implemented |
| PATCH | `/api/sources/{source_id}` | Update `monitoring_enabled` / `title` | None | `SourceUpdate` | `SourceOut` | Implemented |
| DELETE | `/api/sources/{source_id}` | Delete a source and cascade its data | None | — | 204 no content | Implemented |
| POST | `/api/sources/{source_id}/request-access` | Submit exactly one join request | None + Telegram session | — | access-request object | Implemented |
| GET | `/api/sources/{source_id}/access-status` | Operational status + last live probe result | None | — | `AccessStatusOut` | Implemented |
| POST | `/api/sources/{source_id}/check-access` | Re-probe Telegram for current access | None + Telegram session | — | `SourceOut` | Implemented |

### 4.4 Monitoring

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/sources/{source_id}/monitoring/start` | Enter MONITORING and run an immediate collection | None + Telegram session | — | `MonitoringStatusOut` | Implemented |
| POST | `/api/sources/{source_id}/monitoring/stop` | Disable monitoring | None | — | `MonitoringStatusOut` | Implemented |
| GET | `/api/sources/{source_id}/monitoring-status` | Monitoring state and last collection markers | None | — | `MonitoringStatusOut` | Implemented |

### 4.5 Message Retrieval (stored / local only — never calls Telegram)

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/sources/{source_id}/messages` | Stored messages for one source, filterable | None | — | `MessageOut[]` | Implemented |
| GET | `/api/messages/{message_id}` | One stored message by internal id | None | — | `MessageOut` | Implemented |
| GET | `/api/messages` | Flat-filter search across all stored messages | None | — | `MessageOut[]` | Implemented |

### 4.6 Live Telegram Search (every call hits Telegram)

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/telegram/search/global` | Telegram-wide public post search | None + Telegram session | — | `TelegramGlobalSearchResponse` | Implemented (quota-limited) |
| POST | `/api/telegram/search/sources` | Live search across a chosen set of registered sources | None + Telegram session | `MultiSourceSearchRequest` | `MultiSourceSearchResponse` | Implemented (no cursor returned) |
| GET | `/api/sources/{source_id}/search` | Live search inside one registered source | None + Telegram session | — | `TelegramChannelSearchResponse` | Implemented |
| GET | `/api/telegram/channels/{channel_id}/search` | Live search inside an arbitrary channel (fresh probe each call) | None + Telegram session | — | `TelegramChannelSearchResponse` | Implemented |
| POST | `/api/telegram/search/results/save` | Promote one search result into a stored message | None + Telegram session | `SaveSearchResultRequest` | `SaveSearchResultResponse` (201) | Implemented |

### 4.7 Telegram Source Discovery

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/telegram/channels/search` | Find candidate channels/groups by keyword | None + Telegram session | — | `TelegramChannelDiscoveryResponse` | Implemented (no pagination) |
| POST | `/api/telegram/channels/{telegram_id}/register` | Register a discovered channel as a source | None + Telegram session | — (query param only) | `SourceOut` (201) | Implemented |
| POST | `/api/telegram/discovery/extract-links` | Classify Telegram links found in free text (read-only) | None + Telegram session | `ExtractLinksRequest` | `ExtractLinksResponse` | Implemented |

### 4.8 Invite / Join and Bot Operations

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/telegram/bots/{source_id}/start` | Send one `/start` to a registered bot, read its reply, harvest Telegram links | None + Telegram session | — | `StartBotResponse` | Implemented |
| POST | `/api/telegram/invites/join` | Join or request to join via `t.me/+hash` | None + Telegram session | `JoinByInviteRequest` | `JoinByInviteResponse` | Implemented |

### 4.9 Historical Backfill

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| POST | `/api/sources/{source_id}/backfill` | Queue a backward historical collection job | None + Telegram session | `BackfillRequest` (optional) | `BackfillJobResponse` (202) | Implemented |
| GET | `/api/sources/{source_id}/backfill` | List backfill jobs for a source (latest 20) | None | — | `BackfillJobResponse[]` | Implemented |
| GET | `/api/sources/{source_id}/backfill/{job_id}` | Poll one job's progress | None | — | `BackfillJobResponse` | Implemented |

### 4.10 Notifications

| Method | Endpoint | Purpose | Authentication | Request Body | Response | Status |
| --- | --- | --- | --- | --- | --- | --- |
| GET | `/api/notifications` | List persisted notifications | None | — | `NotificationOut[]` | Implemented |
| POST | `/api/notifications/{notification_id}/read` | Mark one notification read | None | — | `NotificationOut` | Implemented |

<div class="callout info">
<strong>Categories deliberately absent.</strong> There is no endpoint group for sending messages, posting content, managing multiple Telegram accounts, exporting data, administering users, or configuring the service at runtime. Those are <strong>Not Implemented</strong>.
</div>

<!--PAGEBREAK-->

# 5. Detailed Endpoint Documentation

**Conventions used in this section**

* Base URL in all examples: `http://localhost:8000` (from `API_HOST=0.0.0.0`, `API_PORT=8000`).
* "Authentication" describes what the *implementation* requires. **No endpoint authenticates the caller.** Where an endpoint needs the service's Telegram account to be authorized, that is stated explicitly.
* Two error envelope shapes exist and are used consistently — `{"detail": ...}` for `HTTPException`/validation, and `{"error": {...}}` for `TelegramSearchError`. See section 16.
* Every request/response field below was read from the Pydantic schema or the route signature. No field has been invented.

## 5.1 GET /health

**Purpose.** Liveness probe. Returns a constant; touches nothing.

**When to use.** Container/orchestrator liveness checks and smoke tests.

**Authentication.** None. No Telegram session required.

**Request**

```http
GET /health HTTP/1.1
Host: localhost:8000
```

**Request Parameters.** None.

**Processing Flow**

```text
Client Request -> FastAPI route -> constant response
```

**Response** (200)

```json
{ "status": "ok" }
```

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | Always the literal `"ok"`. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Always, while the process is serving. |

**Example**

```bash
curl http://localhost:8000/health
```

## 5.2 GET /ready

**Purpose.** Readiness probe. Reports whether Telegram API credentials are configured — it does **not** report whether the session is authorized or connected.

**When to use.** Deployment gate; a quick way to confirm `.env` was loaded.

**Authentication.** None.

**Request**

```http
GET /ready HTTP/1.1
Host: localhost:8000
```

**Processing Flow**

```text
Client Request -> route -> get_client_manager().is_configured (reads Settings only, no RPC)
```

**Response** (200)

```json
{ "status": "ready", "telegram_configured": true }
```

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | `"ready"` when both `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are set, otherwise `"not_configured"`. |
| `telegram_configured` | boolean | The same condition as a boolean. |

<div class="callout warn">
<code>/ready</code> returning <code>"ready"</code> does <strong>not</strong> mean Telegram is reachable or the account is signed in. Use <code>GET /api/telegram/status</code> for that.
</div>

**Example**

```bash
curl http://localhost:8000/ready
```

## 5.3 GET /api/telegram/status

**Purpose.** The authoritative view of the service's Telegram connection: connected, authenticated, which account, and whether the session has been latched invalid.

**When to use.** Before driving any Telegram-dependent workflow; after a suspected session revocation; as an operations dashboard tile.

**Authentication.** None for the caller. Internally attempts a connect, and calls `get_me()` when authenticated.

**Request**

```http
GET /api/telegram/status HTTP/1.1
Host: localhost:8000
```

**Processing Flow**

```text
Client Request
      |
      v
route (routes_auth) -> authentication.get_status(client_manager)
      |
      +-- not configured -> {"error": "not configured"}
      |
      v
client_manager.connect()  ------- failure -> {connected:false, error, session_invalid}
      |
      v
is_authenticated() -> client.get_me() -> mask phone -> response
```

**Response** (200)

```json
{
  "connected": true,
  "authenticated": true,
  "account": { "id": "123456789", "username": "collector_account", "phone": "+1******89" },
  "error": null,
  "session_invalid": false
}
```

| Field | Type | Description |
| --- | --- | --- |
| `connected` | boolean | The Telethon client currently holds a connection. |
| `authenticated` | boolean | `is_user_authorized()` returned true and the session is not latched invalid. |
| `account` | object or null | Present only when authenticated. |
| `account.id` | string or null | Telegram user id of the service's account. |
| `account.username` | string or null | Account username, if it has one. |
| `account.phone` | string or null | **Masked** — first two and last two digits only (`_mask_phone`). |
| `error` | string or null | Connection error, or the latched invalid-session reason. |
| `session_invalid` | boolean | True once a session-invalidating Telegram error has been observed. Only re-authentication clears it. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Always — failure states are reported in the body, not as an HTTP error. |

<div class="callout ok">
This endpoint never returns <code>api_hash</code>, session data, or an unmasked phone number.
</div>

**Example**

```bash
curl http://localhost:8000/api/telegram/status
```

```python
import httpx
s = httpx.get("http://localhost:8000/api/telegram/status").json()
if not s["authenticated"]:
    raise SystemExit(f"Telegram not ready: {s['error']}")
```

## 5.4 POST /api/telegram/auth/send-code

**Purpose.** Step 1 of the non-interactive login: asks Telegram to send a verification code to the account's phone.

**When to use.** First-time setup, or after `session_invalid` becomes true.

**Authentication.** None for the caller. Requires `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` to be configured.

**Request**

```http
POST /api/telegram/auth/send-code HTTP/1.1
Host: localhost:8000
Content-Type: application/json
```

**Request Body**

```json
{ "phone": "+15551234567" }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `phone` | string | Yes | Phone number of the Telegram account, in international format. | `"+15551234567"` |

**Processing Flow**

```text
Client Request -> route -> authentication.send_code()
      |
      v
client_manager.connect()
      |
      v
Telegram: auth.sendCode
      |
      +-- ApiIdInvalidError / PhoneNumberInvalidError / FloodWaitError / RPCError
      |        -> {"ok": false, "error": "..."}   (still HTTP 200)
      v
store pending_phone + pending_phone_code_hash IN MEMORY -> {"ok": true}
```

**Response** (200)

```json
{ "ok": true, "requires_password": false, "error": null }
```

| Field | Type | Description |
| --- | --- | --- |
| `ok` | boolean | The code request succeeded. |
| `requires_password` | boolean | Always `false` at this step. |
| `error` | string or null | Human-readable failure reason, e.g. `"invalid phone number"`, `"rate-limited by Telegram, wait 300s"`. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Both success and handled failure — check `ok`. |
| 422 | Validation error | `phone` missing or not a string. |

**Error Responses**

```json
{ "ok": false, "requires_password": false, "error": "rate-limited by Telegram, wait 86400s" }
```

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/auth/send-code \
  -H "Content-Type: application/json" \
  -d '{"phone": "+15551234567"}'
```

<div class="callout crit">
This endpoint triggers a real Telegram login SMS and is <strong>unauthenticated</strong>. Anyone who can reach the port can initiate login attempts and cause Telegram to rate-limit the account. Do not expose this service to an untrusted network (SEC-01).
</div>

## 5.5 POST /api/telegram/auth/verify-code

**Purpose.** Step 2: completes sign-in with the code Telegram sent, or reports that a 2FA password is still needed.

**When to use.** Immediately after a successful `send-code`, in the same process lifetime (the `phone_code_hash` is in-memory only).

**Authentication.** None for the caller.

**Request Body**

```json
{ "phone": "+15551234567", "code": "12345" }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `phone` | string | Yes | Must exactly match the phone used in `send-code`. | `"+15551234567"` |
| `code` | string | Yes | The verification code, as a string. | `"12345"` |

**Processing Flow**

```text
Client Request -> route -> authentication.verify_code()
      |
      v
guard: pending_phone_code_hash exists AND pending_phone == phone
      |  (else -> ok:false "no pending code request for this phone")
      v
Telegram: auth.signIn(phone, code, phone_code_hash)
      |
      +-- SessionPasswordNeededError -> {ok:false, requires_password:true}
      +-- PhoneCodeInvalidError / PhoneCodeExpiredError -> {ok:false, error:"<ClassName>"}
      v
clear pending hash -> clear_session_invalid() -> {ok:true}
```

**Response** (200)

```json
{ "ok": true, "requires_password": false, "error": null }
```

| Field | Type | Description |
| --- | --- | --- |
| `ok` | boolean | Sign-in completed; the session file is now authorized. |
| `requires_password` | boolean | `true` means 2FA is enabled — call `verify-password` next. |
| `error` | string or null | `"2FA password required"`, `"PhoneCodeInvalidError"`, `"PhoneCodeExpiredError"`, or a raw RPC description. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Success and all handled failures. |
| 422 | Validation error | Missing `phone` or `code`. |

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/auth/verify-code \
  -H "Content-Type: application/json" \
  -d '{"phone": "+15551234567", "code": "12345"}'
```

## 5.6 POST /api/telegram/auth/verify-password

**Purpose.** Step 3: supplies the two-factor (cloud password) to finish sign-in.

**When to use.** Only when `verify-code` returned `requires_password: true`.

**Authentication.** None for the caller.

**Request Body**

```json
{ "password": "<SECRET>" }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `password` | string | Yes | The account's 2FA cloud password. Never logged or stored. | `"<SECRET>"` |

**Processing Flow**

```text
Client Request -> route -> authentication.verify_password()
      |
      v
Telegram: auth.checkPassword (via client.sign_in(password=...))
      |
      +-- RPCError -> {ok:false, error:"<ClassName>: <text>"}
      v
clear pending phone + hash -> clear_session_invalid() -> {ok:true}
```

**Response** (200)

```json
{ "ok": true, "requires_password": false, "error": null }
```

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Success and handled failure. |
| 422 | Validation error | `password` missing. |

<div class="callout crit">
The 2FA password crosses the wire in a plain JSON body. The service terminates <strong>HTTP, not HTTPS</strong> — deploy it behind a TLS-terminating reverse proxy on a trusted network (SEC-03).
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/auth/verify-password \
  -H "Content-Type: application/json" \
  -d '{"password": "<SECRET>"}'
```

<!--PAGEBREAK-->

## 5.7 POST /api/sources

**Purpose.** Registers a Telegram channel, group, bot or user as a tracked **source**, then immediately resolves it against Telegram and records its real access status.

**When to use.** The entry point for everything else. Any workflow that needs a `source_id` starts here.

**Authentication.** None for the caller. Telegram must be configured; if it is unreachable the source is still created and left at `DISCOVERED`.

**Request**

```http
POST /api/sources HTTP/1.1
Host: localhost:8000
Content-Type: application/json
```

**Request Body**

```json
{ "identifier": "@examplechannel", "monitoring_enabled": true }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `identifier` | string | Yes | `@username`, bare username, `https://t.me/...` URL, invite link (`t.me/+hash`, `t.me/joinchat/hash`), or a numeric Telegram id. | `"@examplechannel"` |
| `source_type` | string | No | **Accepted and ignored.** The route never reads it; the real type comes from resolution. | `"channel"` |
| `monitoring_enabled` | boolean | No (default `true`) | Sets the flag on the row. **It does not start monitoring.** | `true` |

<div class="callout warn">
<strong>Common misunderstanding:</strong> <code>monitoring_enabled: true</code> only sets a column. Collection requires <code>access_status == "MONITORING"</code>, which is only reachable through <code>POST /api/sources/{id}/monitoring/start</code>. A source registered with <code>monitoring_enabled: true</code> that is left at <code>PUBLIC_ACCESSIBLE</code> is <strong>never collected</strong> — the scheduler's <code>list_monitoring_enabled()</code> requires both conditions. This state is present in the live database today (source 7).
</div>

**Processing Flow**

```text
Client Request
      |
      v
Route validation (Pydantic SourceCreate)
      |
      v
SourceService.register_source()
      |
      v
normalize_identifier()            string-level only, no network
      |
      v
Dedup pass 1: sources.get_by_identifier(normalized)  -> 409 if hit
      |
      v
INSERT telegram_sources (access_status = DISCOVERED) + audit SOURCE_REGISTERED + COMMIT
      |
      v
check_access(source.id)
      |   verify_authorized() -> Telegram: contacts.resolveUsername / getChannels
      |                       -> Telegram: messages.getHistory(limit=1)   [not for User entities]
      |                       -> or messages.checkChatInvite for invite hashes
      v
record last_probe_* unconditionally; apply state machine to access_status
      |
      v
Dedup pass 2: same telegram_entity_id already tracked?
      |   yes -> delete this row, detach audit, COMMIT, raise 409
      v
API Response 201 (SourceOut)
```

**Response** (201)

```json
{
  "id": 1,
  "identifier": "@examplechannel",
  "telegram_entity_id": "1234567890",
  "username": "examplechannel",
  "title": "Example Channel",
  "source_type": "channel",
  "access_status": "PUBLIC_ACCESSIBLE",
  "status_reason": null,
  "monitoring_enabled": true,
  "monitoring_started_at": null,
  "last_status_check_at": "2026-09-06T09:15:02.113Z",
  "next_status_check_at": null,
  "last_message_id": null,
  "last_collected_at": null,
  "last_probe_status": "PUBLIC_ACCESSIBLE",
  "last_probe_reason": null,
  "last_probe_at": "2026-09-06T09:15:02.113Z",
  "created_at": "2026-09-06T09:15:01.880Z",
  "updated_at": "2026-09-06T09:15:02.115Z"
}
```

**Response Fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | integer | Internal source id used by every other endpoint. |
| `identifier` | string | The **normalized** identifier (`@name` lower-cased, `https://t.me/+hash`, or the numeric id). |
| `telegram_entity_id` | string or null | Telegram's own id, filled in once resolution succeeds. |
| `username` / `title` | string or null | From the resolved entity; `null` if not exposed. |
| `source_type` | string | `channel`, `supergroup`, `group`, `user`, `bot`, `unknown`. |
| `access_status` | string | One of the 13 state-machine states (section 6.1). |
| `status_reason` | string or null | Why the status is what it is, e.g. `"membership required to read this source"`. |
| `monitoring_enabled` | boolean | Flag only — see the callout above. |
| `monitoring_started_at` | datetime or null | Set when the source first enters MONITORING. |
| `last_status_check_at` / `next_status_check_at` | datetime or null | Operational status-check markers. `next_status_check_at` is never written by any code path. |
| `last_message_id` | integer or null | Highest Telegram message id collected so far. |
| `last_collected_at` | datetime or null | End of the last collection cycle. |
| `last_probe_status` / `last_probe_reason` / `last_probe_at` | string / string / datetime, nullable | The **most recent live probe result**, recorded unconditionally even when the state machine refuses to let it overwrite the operational `access_status`. |
| `created_at` / `updated_at` | datetime | Row timestamps. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 201 | Created | Source registered (whatever access status resulted). |
| 409 | Conflict | Identifier already registered, or the resolved entity is already tracked under another identifier. |
| 422 | Unprocessable | `identifier` is not a recognizable Telegram identifier, or body validation failed. |

**Error Responses**

```json
{ "detail": "source already registered as id=1" }
```

```json
{ "detail": "source already registered as id=1 (identifier '@examplechannel', same Telegram entity 1234567890)" }
```

```json
{ "detail": "'not a channel!' is not a recognizable Telegram identifier (@username, t.me link, or numeric ID)" }
```

**Example Usage**

```bash
curl -X POST http://localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"identifier": "@examplechannel", "monitoring_enabled": true}'
```

```python
import httpx

r = httpx.post("http://localhost:8000/api/sources",
               json={"identifier": "@examplechannel", "monitoring_enabled": True})
if r.status_code == 409:
    print("already registered:", r.json()["detail"])
r.raise_for_status()
source = r.json()
print(source["id"], source["access_status"])
```

## 5.8 GET /api/sources

**Purpose.** Lists registered sources, ordered by id ascending.

**When to use.** Inventory views; finding a `source_id`.

**Authentication.** None. No Telegram call.

**Request**

```http
GET /api/sources?limit=100&offset=0 HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `limit` | integer (query) | No (default 100) | Page size. **Not bounded by the implementation.** | `50` |
| `offset` | integer (query) | No (default 0) | Rows to skip. | `100` |

**Processing Flow**

```text
Client Request -> route -> SourceService.list_sources -> SourceRepository.list_all -> SELECT ... ORDER BY id
```

**Response** (200) — a JSON array of `SourceOut` objects (fields as in 5.7).

```json
[ { "id": 1, "identifier": "@examplechannel", "access_status": "MONITORING", "...": "..." } ]
```

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Always, including an empty array. |
| 422 | Validation error | `limit`/`offset` not integers. |

**Example**

```bash
curl "http://localhost:8000/api/sources?limit=50&offset=0"
```

## 5.9 GET /api/sources/{source_id}

**Purpose.** Fetches one source by internal id.

**When to use.** Polling a source's `access_status` after registration or a join request.

**Authentication.** None. No Telegram call — this reads the stored row only.

**Request**

```http
GET /api/sources/1 HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Internal source id. | `1` |

**Processing Flow**

```text
Client Request -> route -> SourceService.get_source -> session.get(TelegramSource, id) -> 404 if None
```

**Response** (200) — `SourceOut`, identical shape to 5.7.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Source exists. |
| 404 | Not Found | No such source id. |

**Error Responses**

```json
{ "detail": "source not found" }
```

**Example**

```bash
curl http://localhost:8000/api/sources/1
```

## 5.10 PATCH /api/sources/{source_id}

**Purpose.** Updates a source's `monitoring_enabled` flag and/or `title`.

**When to use.** Renaming a source for readability. **Not** the correct way to start or stop monitoring.

**Authentication.** None. No Telegram call.

**Request Body**

```json
{ "title": "Example Channel (priority)", "monitoring_enabled": false }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Internal source id. | `1` |
| `monitoring_enabled` | boolean | No | Written straight to the column. | `false` |
| `title` | string | No | Free-text label; overwritten again on the next successful access check. | `"Example Channel"` |

<div class="callout crit">
<strong>Confirmed defect ISS-01 — this endpoint does not persist anything.</strong> <code>SourceService.update_source</code> calls <code>SourceRepository.update</code>, which <code>flush()</code>es but never <code>commit()</code>s, and no caller commits afterwards. When FastAPI closes the request-scoped session from <code>get_db</code>, the open transaction is rolled back. The endpoint returns <strong>200 with the modified object</strong>, but re-reading the source afterwards shows the old values. Verified by reproducing the exact dependency/repository pattern against SQLAlchemy 2.0.52 + aiosqlite. Do not rely on this endpoint until it is fixed (one-line fix: commit in <code>update_source</code>).
</div>

<div class="callout warn">
<strong>Second issue in the same method (ISS-04):</strong> <code>update_source</code> writes fields directly through the repository, bypassing <code>_apply_status</code> — the choke point that normally enforces "monitoring may not be enabled from a blocked state". Once the missing commit is fixed, setting <code>monitoring_enabled: true</code> here on, say, an <code>ACCESS_DENIED</code> source would persist an inconsistent row. Collection would still not run (the job requires <code>monitoring_enabled = true <em>AND</em> access_status = 'MONITORING'</code>), but the invariant would be violated. Use <code>monitoring/start</code> and <code>monitoring/stop</code> instead.
</div>

**Processing Flow**

```text
Client Request -> Pydantic SourceUpdate (exclude_unset) -> SourceService.update_source
      -> drops None values -> SourceRepository.update (flush only, NO COMMIT, no state machine)
      -> response serialized from the in-memory object
      -> get_db closes the session -> TRANSACTION ROLLED BACK -> change lost
```

**Response** (200) — `SourceOut`.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Updated. |
| 404 | Not Found | No such source. |
| 422 | Validation error | Wrong field types. |

**Example**

```bash
curl -X PATCH http://localhost:8000/api/sources/1 \
  -H "Content-Type: application/json" \
  -d '{"title": "Example Channel (priority)"}'
```

## 5.11 DELETE /api/sources/{source_id}

**Purpose.** Permanently removes a source and, by ORM cascade, its messages, media, access requests and checkpoint.

**When to use.** Retiring a source. **Destructive and irreversible.**

**Authentication.** None. No Telegram call.

**Request**

```http
DELETE /api/sources/1 HTTP/1.1
Host: localhost:8000
```

**Processing Flow**

```text
Client Request -> route -> SourceService.delete_source
      -> _require_source (404 if missing)
      -> session.delete(source)   ORM cascade: messages -> media, access_requests, checkpoint
      -> COMMIT
```

**Response** (204) — empty body.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 204 | No Content | Deleted. |
| 404 | Not Found | No such source. |

<div class="callout crit">
Unauthenticated and destructive. Deleting source <code>1</code> in the current database would remove 4,400 collected messages. Raw evidence JSON under <code>data/raw_messages/&lt;source_id&gt;/</code> is <strong>not</strong> deleted — it is orphaned on disk. Audit rows survive with <code>source_id</code> left dangling (SQLite foreign keys are not enabled; only <code>audit.detach_source</code> during duplicate-rejection clears them).
</div>

**Example**

```bash
curl -X DELETE http://localhost:8000/api/sources/1 -i
```

## 5.12 POST /api/sources/{source_id}/request-access

**Purpose.** Submits **exactly one** legitimate Telegram join request for a private source, and records it for the 12-hour reconciliation job to follow up.

**When to use.** When `access_status` is `JOIN_REQUEST_REQUIRED` (or `ERROR`, to retry after a transient failure).

**Authentication.** None for the caller. **Requires an authorized Telegram session.**

**Request**

```http
POST /api/sources/2/request-access HTTP/1.1
Host: localhost:8000
```

**Request Parameters**

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Internal source id. | `2` |

**Request Body.** None.

**Processing Flow**

```text
Client Request
      |
      v
SourceService.request_access()
      |
      v
Is there already a PENDING access request?  --yes--> return it unchanged (NO second RPC)
      |
      v
Is access_status in {JOIN_REQUEST_REQUIRED, ERROR}?  --no--> 409
      |
      v
verify_authorized()   --failure--> set ERROR + RuntimeError (surfaces as HTTP 500, see ISS-03)
      |
      v
join_request_manager.submit_join_request()
      |   invite hash -> Telegram: messages.importChatInvite
      |   username    -> Telegram: channels.joinChannel
      v
INSERT telegram_access_requests (status, requested_at, next_check_at = +12h, attempt_count = 1)
      |
      v
apply state machine: JOINED | JOIN_REQUEST_PENDING | ERROR
      |   JOINED also writes a TELEGRAM_ACCESS_GRANTED notification
      v
audit JOIN_REQUEST_SUBMITTED -> COMMIT -> API Response 200
```

**Response** (200)

```json
{
  "id": 1,
  "source_id": 2,
  "status": "PENDING",
  "requested_at": "2026-09-06T09:20:00.000Z",
  "next_check_at": "2026-09-06T21:20:00.000Z",
  "attempt_count": 1
}
```

| Field | Type | Description |
| --- | --- | --- |
| `id` | integer | Access-request row id. |
| `source_id` | integer | The source this request belongs to. |
| `status` | string | `PENDING`, `APPROVED` (joined immediately), or `ERROR`. |
| `requested_at` | datetime | When the request was submitted. |
| `next_check_at` | datetime or null | When reconciliation will re-check (submission time + 12h). |
| `attempt_count` | integer | Number of reconciliation attempts; `1` at submission. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Request submitted, or an existing PENDING request returned unchanged. |
| 404 | Not Found | No such source. |
| 409 | Conflict | Source is not in `JOIN_REQUEST_REQUIRED` or `ERROR` (e.g. a public channel that needs no join). |
| 500 | Internal Server Error | Telegram session unusable — `RuntimeError` is not mapped by the route (finding ISS-03). |

**Error Responses**

```json
{ "detail": "source 1 is in state PUBLIC_ACCESSIBLE; access can only be requested from JOIN_REQUEST_REQUIRED" }
```

<div class="callout ok">
<strong>Guarantee enforced in code:</strong> while a PENDING request exists, this endpoint is a no-op that returns the existing row — no duplicate join is ever sent to Telegram, however many times it is called. After a rejection, no alternative access mechanism is attempted.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/sources/2/request-access
```

## 5.13 GET /api/sources/{source_id}/access-status

**Purpose.** Returns the source's **operational** status and, separately, the result of the most recent **live probe**. The two are deliberately not merged.

**When to use.** Diagnosing "why is this source not collecting?"; distinguishing "we are monitoring it" from "the last probe said X".

**Authentication.** None. No Telegram call — reads stored fields only.

**Request**

```http
GET /api/sources/1/access-status HTTP/1.1
Host: localhost:8000
```

**Processing Flow**

```text
Client Request -> route -> SourceService.get_access_status (row read) -> map to AccessStatusOut
```

**Response** (200)

```json
{
  "source_id": 1,
  "access_status": "MONITORING",
  "status_reason": null,
  "last_status_check_at": "2026-09-06T09:15:02.113Z",
  "next_status_check_at": null,
  "last_probe_status": "PUBLIC_ACCESSIBLE",
  "last_probe_reason": null,
  "last_probe_at": "2026-09-06T09:15:02.113Z"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `source_id` | integer | Source id. |
| `access_status` | string | The operational state (e.g. `MONITORING`). |
| `status_reason` | string or null | Explanation attached to the current state. |
| `last_status_check_at` | datetime or null | When `check_access` last ran. |
| `next_status_check_at` | datetime or null | **Always `null`** — no code path writes this column. |
| `last_probe_status` | string or null | What the last live probe actually found. May legitimately differ from `access_status` (a probe finding `PUBLIC_ACCESSIBLE` must not downgrade an operational `MONITORING`). |
| `last_probe_reason` | string or null | Probe explanation. |
| `last_probe_at` | datetime or null | Probe timestamp. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Source exists. |
| 404 | Not Found | No such source. |

**Example**

```bash
curl http://localhost:8000/api/sources/1/access-status
```

## 5.14 POST /api/sources/{source_id}/check-access

**Purpose.** Forces a fresh live access probe against Telegram and updates the stored source record.

**When to use.** After a join request may have been approved; after an `ERROR`; when access is suspected to have been revoked.

**Authentication.** None for the caller. Requires an authorized Telegram session (a failure is recorded as `ERROR`, not raised).

**Request**

```http
POST /api/sources/1/check-access HTTP/1.1
Host: localhost:8000
```

**Processing Flow**

```text
Client Request
      |
      v
SourceService.check_access()
      |
      v
verify_authorized()  --fail--> access_status = ERROR, last_probe_* = ERROR,
      |                        audit ACCESS_CHECKED or TELEGRAM_ACCOUNT_UNAUTHORIZED, return 200
      v
access_manager.check_access()
      |   username/id  -> Telegram: get_entity  then messages.getHistory(limit=1)
      |   invite hash  -> Telegram: messages.checkChatInvite
      v
write last_probe_status / last_probe_reason / last_probe_at  (ALWAYS)
write telegram_entity_id / username / title / source_type when resolved
      |
      v
upsert telegram_entities (resolution cache, never access proof)
      |
      v
_apply_status(): validate transition; a blocked transition leaves access_status
                 untouched and only records status_reason
      |
      v
audit ACCESS_CHECKED (+ SOURCE_RESOLVED / TELEGRAM_BOT_DETECTED on first resolution)
      |
      v
COMMIT -> API Response 200 (SourceOut)
```

**Response** (200) — `SourceOut` (see 5.7).

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Probe ran; the outcome is in the body (including failures recorded as `ERROR`). |
| 404 | Not Found | No such source. |

**Access outcomes produced by the probe**

| Probe result | Meaning |
| --- | --- |
| `PUBLIC_ACCESSIBLE` | Public channel/group readable without membership. |
| `ACCESSIBLE` | Already a member (`left == False`), or a User entity, or `ChatInviteAlready`. |
| `JOIN_REQUEST_REQUIRED` | Private but has a username, or an invite link that needs joining/approval. |
| `ACCESS_DENIED` | Private with no invite, admin rights required, or an expired invite link. |
| `NOT_FOUND` | Username unoccupied/invalid, channel invalid, or invalid invite hash. |
| `ERROR` | FloodWait, other RPC failure, or an invalid Telegram session (`session_invalid` also latches). |

**Example**

```bash
curl -X POST http://localhost:8000/api/sources/1/check-access
```

<!--PAGEBREAK-->

## 5.15 POST /api/sources/{source_id}/monitoring/start

**Purpose.** Moves a verified-accessible source into `MONITORING`, enables the flag, and **runs one collection cycle immediately** rather than waiting for the scheduler.

**When to use.** Once `access_status` is `PUBLIC_ACCESSIBLE`, `ACCESSIBLE`, `JOINED` or already `MONITORING`.

**Authentication.** None for the caller. **Requires an authorized Telegram session** — checked before anything is written.

**Request**

```http
POST /api/sources/1/monitoring/start HTTP/1.1
Host: localhost:8000
```

**Processing Flow**

```text
Client Request
      |
      v
MonitoringService.start_monitoring()
      |
      v
verify_authorized()  --fail--> MonitoringStateError -> 409
      |
      v
access_status in {PUBLIC_ACCESSIBLE, ACCESSIBLE, JOINED, MONITORING}?  --no--> 409
      |
      v
transition -> MONITORING; set monitoring_enabled = true, monitoring_started_at = now
      |
      v
audit MONITORING_STARTED -> COMMIT
      |
      v
run_collection_for_source()   [immediate first cycle]
      |   verify_authorized -> get_entity
      |   checkpoint = get_or_create(source)
      |   Telegram: iter_messages(min_id = checkpoint, reverse=True, limit = COLLECTION_BATCH_LIMIT)
      |   normalize -> write raw evidence JSON -> INSERT ... ON CONFLICT DO NOTHING
      |   advance checkpoint, one COMMIT per message
      v
API Response 200 (MonitoringStatusOut)
```

**Response** (200)

```json
{
  "source_id": 1,
  "monitoring_enabled": true,
  "access_status": "MONITORING",
  "monitoring_started_at": "2026-09-06T09:30:00.000Z",
  "last_collected_at": "2026-09-06T09:30:04.512Z",
  "last_message_id": 72697
}
```

| Field | Type | Description |
| --- | --- | --- |
| `source_id` | integer | Source id. |
| `monitoring_enabled` | boolean | Now `true`. |
| `access_status` | string | `MONITORING` on success. |
| `monitoring_started_at` | datetime or null | Set on the first entry into MONITORING. |
| `last_collected_at` | datetime or null | End of the collection cycle this call just ran. |
| `last_message_id` | integer or null | Checkpoint after that cycle. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Monitoring started and the initial cycle completed (even if the cycle itself collected nothing). |
| 404 | Not Found | No such source. |
| 409 | Conflict | Source not in a verified-accessible state, or the Telegram session is unusable. |

**Error Responses**

```json
{ "detail": "source 2 is in state JOIN_REQUEST_PENDING; monitoring requires verified access first" }
```

```json
{ "detail": "cannot start monitoring: The Telegram authorization session is no longer valid. Re-authentication is required." }
```

<div class="callout warn">
This call is <strong>synchronous through the first collection cycle</strong>. For a large channel that can pull up to <code>COLLECTION_BATCH_LIMIT</code> (default 200) messages, download nothing (media off by default), and write 200 evidence files before responding. Expect a multi-second response; set a generous client timeout.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/sources/1/monitoring/start
```

## 5.16 POST /api/sources/{source_id}/monitoring/stop

**Purpose.** Disables monitoring for a source. The scheduled collection job will skip it from the next cycle.

**When to use.** Pausing collection without deleting anything.

**Authentication.** None. No Telegram call.

**Processing Flow**

```text
Client Request -> MonitoringService.stop_monitoring
      -> monitoring_enabled = false   (access_status is deliberately left as MONITORING)
      -> audit MONITORING_STOPPED -> COMMIT
```

<div class="callout info">
Stopping leaves <code>access_status = "MONITORING"</code> and only clears the flag. That is intentional and reversible: <code>monitoring/start</code> then simply re-enables the flag without a state transition. The collection job requires <em>both</em> conditions, so nothing is collected while stopped.
</div>

**Response** (200) — `MonitoringStatusOut` with `monitoring_enabled: false`.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Stopped. |
| 404 | Not Found | No such source. |

**Example**

```bash
curl -X POST http://localhost:8000/api/sources/1/monitoring/stop
```

## 5.17 GET /api/sources/{source_id}/monitoring-status

**Purpose.** Reports monitoring state and collection progress markers for one source.

**When to use.** Dashboards; confirming that collection is advancing (`last_message_id` increasing).

**Authentication.** None. No Telegram call.

**Response** (200) — `MonitoringStatusOut`, fields as in 5.15.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Source exists. |
| 404 | Not Found | No such source. |

**Example**

```bash
curl http://localhost:8000/api/sources/1/monitoring-status
```

## 5.18 GET /api/sources/{source_id}/messages

**Purpose.** Reads **locally stored** messages for one source, with flat filters. **Never calls Telegram.**

**When to use.** Reviewing what has actually been collected. For a live query against Telegram, use `GET /api/sources/{source_id}/search` (5.22).

**Authentication.** None. Database only.

**Request**

```http
GET /api/sources/1/messages?keyword=invoice&limit=50&offset=0 HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Source id. | `1` |
| `limit` | integer (query) | No (default 50) | Page size. **Unbounded in code** (finding ISS-05). | `100` |
| `offset` | integer (query) | No (default 0) | Rows to skip. | `50` |
| `keyword` | string (query) | No | Case-insensitive substring match on `text` (SQL `ILIKE %kw%`). No boolean/`AND`/`OR` syntax. | `invoice` |
| `sender_username` | string (query) | No | **Exact** match on the stored sender username. | `poster` |
| `media_type` | string (query) | No | Exact match: `photo`, `video`, `document`, `audio`, `voice`, `sticker`, `other`. | `photo` |
| `date_from` | datetime (query) | No | `message_date >=`. ISO-8601. | `2026-09-01T00:00:00Z` |
| `date_to` | datetime (query) | No | `message_date <=`. | `2026-09-06T00:00:00Z` |

**Processing Flow**

```text
Client Request -> route -> MessageService.list_for_source -> MessageRepository.list_for_source
      -> SELECT ... WHERE source_id = ? [+ filters] ORDER BY telegram_message_id DESC LIMIT/OFFSET
```

**Response** (200) — array of `MessageOut`.

```json
[
  {
    "id": 12805,
    "source_id": 1,
    "telegram_message_id": 72697,
    "sender_id": "1234567890",
    "sender_username": "poster",
    "sender_display_name": "Example Poster",
    "message_date": "2026-09-06T08:59:11Z",
    "edit_date": null,
    "text": "Example message body",
    "views": 1420,
    "forwards": 3,
    "reply_count": null,
    "media_type": "photo",
    "source_url": "https://t.me/examplechannel/72697",
    "collected_at": "2026-09-06T09:00:02Z",
    "raw_data_hash": "9f2c...e11a",
    "processing_status": "raw"
  }
]
```

**Response Fields**

| Field | Type | Description |
| --- | --- | --- |
| `id` | integer | Internal message row id (used by `GET /api/messages/{message_id}`). |
| `source_id` | integer | Owning source. |
| `telegram_message_id` | integer | Telegram's own message id within the channel. |
| `sender_id` / `sender_username` / `sender_display_name` | string, nullable | `null` when Telegram does not expose the sender (common for broadcast channels). |
| `message_date` / `edit_date` | datetime, nullable | UTC. |
| `text` | string or null | Message body; `null` for media-only messages. |
| `views` / `forwards` / `reply_count` | integer, nullable | `null` when not exposed. |
| `media_type` | string or null | Classified type; `null` when no media. |
| `source_url` | string or null | Permalink; `null` when the source has no username. |
| `collected_at` | datetime | When this service stored it. |
| `raw_data_hash` | string or null | SHA-256 of the raw Telegram payload (evidence integrity). |
| `processing_status` | string | Always `"raw"` today — `normalized`/`enriched` exist in the enum but nothing sets them. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Including an empty array for an unknown `source_id` (no 404 here). |
| 422 | Validation error | Bad datetime or integer format. |

**Example**

```bash
curl "http://localhost:8000/api/sources/1/messages?keyword=invoice&media_type=photo&limit=20"
```

## 5.19 GET /api/messages/{message_id}

**Purpose.** Fetches one stored message by its internal row id.

**Authentication.** None. Database only.

**Request Parameters**

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `message_id` | integer (path) | Yes | Internal row id — **not** the Telegram message id. | `12805` |

**Processing Flow**

```text
Client Request -> MessageService.get_message -> session.get(TelegramMessage, id) -> 404 if None
```

**Response** (200) — a single `MessageOut` (fields as in 5.18).

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Row exists. |
| 404 | Not Found | `{"detail": "message not found"}`. |

**Example**

```bash
curl http://localhost:8000/api/messages/12805
```

## 5.20 GET /api/messages

**Purpose.** Flat-filter search across **all** stored messages, independent of any Telegram search RPC.

**When to use.** Analysis over already-collected data; the cheapest and safest query in the service (no Telegram traffic, no rate-limit exposure).

**Authentication.** None. Database only.

**Request**

```http
GET /api/messages?keyword=invoice&source_id=1&limit=50 HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `keyword` | string (query) | No | Case-insensitive substring on `text`. | `invoice` |
| `source_id` | integer (query) | No | Restrict to one source. | `1` |
| `sender_username` | string (query) | No | Exact match. | `poster` |
| `media_type` | string (query) | No | Exact match. | `document` |
| `date_from` / `date_to` | datetime (query) | No | Inclusive bounds on `message_date`. | `2026-09-01T00:00:00Z` |
| `limit` | integer (query) | No (default 50) | Page size, unbounded in code. | `100` |
| `offset` | integer (query) | No (default 0) | Rows to skip. | `0` |

**Processing Flow**

```text
Client Request -> MessageService.search -> MessageRepository.search
      -> SELECT ... [filters] ORDER BY message_date DESC LIMIT/OFFSET
```

**Response** (200) — array of `MessageOut`.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Including an empty array. |
| 422 | Validation error | Bad parameter types. |

<div class="callout info">
<strong>Not implemented:</strong> boolean/nested query syntax (<code>AND</code>/<code>OR</code>/<code>NOT</code>, phrase grouping), full-text indexing, relevance ranking, and result counts/total pages. Filters are flat and combined with SQL <code>AND</code>. Ordering is always <code>message_date DESC</code>.
</div>

**Example**

```bash
curl "http://localhost:8000/api/messages?keyword=invoice&date_from=2026-09-01T00:00:00Z&limit=20"
```

```python
import httpx
rows = httpx.get("http://localhost:8000/api/messages",
                 params={"keyword": "invoice", "source_id": 1, "limit": 100}).json()
print(len(rows), "stored matches")
```
<!--PAGEBREAK-->

## 5.21 GET /api/telegram/search/global

**Purpose.** A **live, Telegram-wide** search for public posts. Prefers Telegram's genuine cross-membership post search (`channels.searchPosts`) and falls back to `messages.searchGlobal` when the free quota is exhausted. **Never reads the local database.**

**When to use.** Discovering content outside the sources you already track. For already-collected data use `GET /api/messages` — it costs nothing and carries no rate-limit risk.

**Authentication.** None for the caller. **Requires an authorized Telegram session** (`_require_session()` — 401 otherwise).

**Request**

```http
GET /api/telegram/search/global?q=example&limit=20&sort=date_desc HTTP/1.1
Host: localhost:8000
```

**Request Parameters**

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `q` | string (query) | **Yes** | Search text, 1–256 characters (enforced by both FastAPI and the discovery layer). | `example` |
| `limit` | integer (query) | No (default 20) | 1–100, enforced by FastAPI and re-clamped by `clamp_limit`. | `50` |
| `cursor` | string (query) | No | Opaque base64 cursor from a previous response's `next_cursor`. Not stable across app versions. | `eyJvZmZzZXQ...` |
| `from_date` | datetime (query) | No | Lower date bound. Native (`min_date`) on the `searchGlobal` path; applied **locally** on the `searchPosts` path. | `2026-09-01T00:00:00Z` |
| `to_date` | datetime (query) | No | Upper date bound; same caveat. | `2026-09-06T00:00:00Z` |
| `channel_username` | string (query) | No | **Local** post-filter on the returned page only. | `examplechannel` |
| `channel_id` | string (query) | No | **Local** post-filter on the returned page only. | `1234567890` |
| `media_type` | string (query) | No | `photo`, `video`, `document`, `voice`, `audio`. Native filter on `searchGlobal`; local on `searchPosts`. | `photo` |
| `sort` | string (query) | No (default `date_desc`) | Only `date_desc` or `relevance`. `relevance` performs **no** re-sorting — Telegram's own order is preserved. | `date_desc` |
| `include_raw` | boolean (query) | No (default `false`) | Include the full raw Telegram payload per result. Large. | `false` |

**Processing Flow**

```text
Client Request
      |
      v
Route validation (q length, limit range, sort pattern)
      |
      v
SearchService.global_search -> _require_session() -> 401 NOT_AUTHENTICATED if unusable
      |
      v
rate_policy.run_bounded_search(key)      max 3 concurrent search RPCs;
      |                                  identical concurrent calls share one execution
      v
Telegram: channels.checkSearchPostsFlood(q)
      |
      +-- free quota remains --> Telegram: channels.searchPosts     visibility = "public"
      |
      +-- quota exhausted -----> WARNING added, NO Stars are ever spent
      +-- PremiumAccountRequiredError / RPCError --> WARNING added
                     |
                     v
              Telegram: messages.searchGlobal(broadcasts_only=True)  visibility = "account_scoped"
      |
      v
Normalize messages -> local filters (keyword re-verification ONLY on the searchGlobal path)
      |
      v
Sort (date_desc) -> build next_cursor from the last message + next_rate
      |
      v
audit SEARCH_GLOBAL + structured log -> API Response 200
```

**Response** (200)

```json
{
  "results": [
    {
      "message_id": "72697",
      "channel": { "id": "1234567890", "title": "Example Channel", "username": "examplechannel", "type": "channel" },
      "text": "Example matching message",
      "date": "2026-09-06T08:59:11Z",
      "sender": { "id": "9876543210", "username": "poster", "display_name": "Example Poster" },
      "media": { "media_type": "photo", "filename": null, "mime_type": "image/jpeg", "file_size": 84213 },
      "url": "https://t.me/examplechannel/72697",
      "source_type": "telegram",
      "visibility": "public",
      "collection_method": "global_search",
      "raw": null
    }
  ],
  "pagination": { "limit": 20, "next_cursor": "eyJvZmZzZXRfaWQiOjcyNjk3fQ==", "has_more": true },
  "method_used": "channels.searchPosts",
  "quota": { "remaining": 4, "total_daily": 5, "stars_required": 0 },
  "warnings": []
}
```

**Response Fields**

| Field | Type | Description |
| --- | --- | --- |
| `results[].message_id` | string | Telegram message id, as a string. |
| `results[].channel` | object | `id`, `title`, `username`, `type` (`channel`/`group`/`user`/`unknown`). Any may be `null` when Telegram did not return the entity. |
| `results[].text` | string or null | Message body. |
| `results[].date` | datetime or null | UTC. |
| `results[].sender` | object or null | `id`, `username`, `display_name`. Often absent for broadcast posts. |
| `results[].media` | object or null | `media_type`, `filename`, `mime_type`, `file_size`. |
| `results[].url` | string or null | Permalink; `null` when the channel has no username. |
| `results[].source_type` | string | Always `"telegram"`. |
| `results[].visibility` | string | `"public"` (searchPosts) or `"account_scoped"` (searchGlobal). **The key honesty field.** |
| `results[].collection_method` | string | Always `"global_search"` here. |
| `results[].raw` | object or null | Full Telegram payload when `include_raw=true`. |
| `pagination.limit` | integer | Echo of the requested limit. |
| `pagination.next_cursor` | string or null | Pass back as `cursor`. |
| `pagination.has_more` | boolean | True when Telegram returned a full page. |
| `method_used` | string | `channels.searchPosts` or `messages.searchGlobal`. |
| `quota` | object or null | `remaining`, `total_daily`, `stars_required` — present only when the flood check succeeded. |
| `warnings` | string[] | Human-readable caveats, e.g. quota exhaustion or the account-scoped disclaimer. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Search executed (possibly with warnings and zero results). |
| 401 | Unauthorized | `NOT_AUTHENTICATED` / `TELEGRAM_SESSION_INVALID`. |
| 402 | Payment Required | `PREMIUM_OR_QUOTA_REQUIRED` if Telegram raises it outside the handled fallback. |
| 422 | Unprocessable | `q` empty or over 256 chars, `limit` out of range, `sort` not matching the pattern, bad cursor. |
| 429 | Too Many Requests | `FLOOD_WAIT` with `retry_after_seconds`. |
| 502 / 503 | Bad Gateway / Unavailable | `TELEGRAM_RPC_ERROR` / `NETWORK_ERROR`. |

**Error Responses**

```json
{ "error": { "code": "FLOOD_WAIT", "message": "Telegram is rate-limiting this account - wait 42s before retrying.", "retryable": true, "retry_after_seconds": 42 } }
```

```json
{ "error": { "code": "NOT_AUTHENTICATED", "message": "The Telegram authorization session is no longer valid. Re-authentication is required.", "retryable": false } }
```

<div class="callout warn">
<strong>This is not "search all of Telegram."</strong> When <code>method_used</code> is <code>messages.searchGlobal</code>, results come from Telegram's index <em>for this account</em> — in practice mostly channels it has already joined — and a warning saying exactly that is returned in <code>warnings</code>. Only <code>channels.searchPosts</code> reaches channels the account has not joined, and Telegram caps that with a small free daily quota (then Telegram Stars, which this service will never spend automatically).
</div>

<div class="callout info">
<strong>Filter caveat:</strong> <code>channel_username</code>, <code>channel_id</code>, and (on the searchPosts path) <code>media_type</code>/<code>from_date</code>/<code>to_date</code> are applied <em>after</em> Telegram returns a page. A request with <code>limit=20</code> plus a narrow filter can legitimately return far fewer than 20 results while <code>has_more</code> is still <code>true</code>. Page through with <code>cursor</code> rather than assuming an empty page means "no more matches".
</div>

**Example Usage**

```bash
curl -G "http://localhost:8000/api/telegram/search/global" \
  --data-urlencode "q=example" \
  --data-urlencode "limit=20" \
  --data-urlencode "sort=date_desc"
```

```python
import httpx

def paged_global_search(q, pages=3):
    cursor, out = None, []
    with httpx.Client(base_url="http://localhost:8000", timeout=60) as c:
        for _ in range(pages):
            params = {"q": q, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            r = c.get("/api/telegram/search/global", params=params)
            if r.status_code == 429:
                print("flood wait:", r.json()["error"]["retry_after_seconds"], "s")
                break
            r.raise_for_status()
            body = r.json()
            out += body["results"]
            for w in body["warnings"]:
                print("WARNING:", w)
            cursor = body["pagination"]["next_cursor"]
            if not body["pagination"]["has_more"] or not cursor:
                break
    return out
```

## 5.22 GET /api/sources/{source_id}/search

**Purpose.** **Live** Telegram search inside one already-registered source, trusting its stored access status instead of re-probing on every call.

**When to use.** Targeted keyword queries against a channel you already track and have verified access to.

**Authentication.** None for the caller. Requires an authorized Telegram session **and** a source whose stored `access_status` is one of `PUBLIC_ACCESSIBLE`, `ACCESSIBLE`, `JOINED`, `MONITORING`.

**Request**

```http
GET /api/sources/1/search?q=invoice&limit=20 HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Registered source id. | `1` |
| `q` | string (query) | **Yes** | 1–256 characters. | `invoice` |
| `limit` | integer (query) | No (default 20) | 1–100. | `50` |
| `cursor` | string (query) | No | Opaque cursor (`offset_id`) from a previous response. | `eyJvZmZzZXRfaWQiOjcyNjk3fQ==` |
| `sender_username` | string (query) | No | Applied **natively** by Telegram (`from_user`). | `poster` |
| `from_date` | datetime (query) | No | Walk stops when older messages are reached (local break). | `2026-09-01T00:00:00Z` |
| `to_date` | datetime (query) | No | Passed natively as `offset_date`. | `2026-09-06T00:00:00Z` |
| `media_type` | string (query) | No | Native Telegram filter (`photo`/`video`/`document`/`voice`/`audio`). | `document` |
| `include_raw` | boolean (query) | No | Include the raw payload per result. | `false` |

**Processing Flow**

```text
Client Request
      |
      v
SearchService.channel_search_by_source()
      |
      v
Load source (404 if missing) -> reject if source_type == "bot" (422 NOT_A_CHANNEL)
      |
      v
stored access_status in SEARCHABLE_STATUSES?  --no--> 403/404 WITHOUT contacting Telegram
      |
      v
_require_session() -> Telegram: get_entity(normalized identifier)
      |
      v
Telegram: messages.search via iter_messages(entity, search=q, from_user, filter, offset_id, offset_date)
      |
      v
Normalize -> build next_cursor (offset_id of the last message)
      |
      v
audit SEARCH_CHANNEL + structured log -> API Response 200
```

**Response** (200)

```json
{
  "results": [
    {
      "message_id": "72697",
      "channel": { "id": "1234567890", "title": "Example Channel", "username": "examplechannel", "type": "channel" },
      "text": "Example matching message",
      "date": "2026-09-06T08:59:11Z",
      "sender": { "id": "9876543210", "username": null, "display_name": null },
      "media": null,
      "url": "https://t.me/examplechannel/72697",
      "source_type": "telegram",
      "visibility": "public",
      "collection_method": "channel_search",
      "raw": null
    }
  ],
  "pagination": { "limit": 20, "next_cursor": null, "has_more": false },
  "warnings": []
}
```

**Response Fields.** Same result shape as 5.21, with two differences: `collection_method` is `"channel_search"`, and `visibility` is `"public"` when the channel has a username, otherwise `"member"`. `sender.username` and `sender.display_name` are always `null` on this path — the per-channel search does not resolve senders (only `sender.id` is returned).

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Search executed. |
| 401 | Unauthorized | Telegram session unusable. |
| 403 | Forbidden | Stored status blocks searching: `JOIN_REQUIRED`, `JOIN_PENDING`, `ACCESS_REJECTED`, `ACCESS_DENIED`. |
| 404 | Not Found | Source id unknown, or its status is `NOT_FOUND`. |
| 422 | Unprocessable | `q`/`limit` invalid, bad cursor, or the source is a bot (`NOT_A_CHANNEL`). |
| 429 | Too Many Requests | `FLOOD_WAIT`. |

**Error Responses**

```json
{ "error": { "code": "JOIN_PENDING", "message": "A join request for this source is still pending Telegram's approval.", "retryable": false } }
```

```json
{ "error": { "code": "NOT_A_CHANNEL", "message": "This Telegram entity is a bot, not a channel/group - bots have no message-search source. Use the bot-start workflow (POST /api/telegram/bots/{id}/start) instead.", "retryable": false } }
```

<div class="callout ok">
An inaccessible source is <strong>refused with an explicit error, never answered with an empty result list</strong>. That distinction is deliberate: "no matches" and "you cannot read this" must not look alike.
</div>

**Example**

```bash
curl -G "http://localhost:8000/api/sources/1/search" \
  --data-urlencode "q=invoice" --data-urlencode "media_type=document"
```

## 5.23 GET /api/telegram/channels/{channel_id}/search

**Purpose.** **Live** search inside an arbitrary channel that is **not** registered as a source. Performs a fresh access probe on every call, because there is no stored state to trust.

**When to use.** One-off exploration of a channel found via discovery, before deciding to register it.

**Authentication.** None for the caller. Requires an authorized Telegram session.

**Request**

```http
GET /api/telegram/channels/examplechannel/search?q=invoice HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `channel_id` | string (path) | Yes | Any identifier `normalize_identifier` accepts: `@username`, bare username, or numeric id. | `examplechannel` |
| `q`, `limit`, `cursor`, `sender_username`, `from_date`, `to_date`, `media_type`, `include_raw` | | | Identical to 5.22. | |

**Processing Flow**

```text
Client Request
      |
      v
SearchService.channel_search_by_identifier() -> _require_session()
      |
      v
normalize_identifier -> access_manager.check_access()      LIVE PROBE ON EVERY CALL
      |     (Telegram: get_entity + messages.getHistory(1), or checkChatInvite)
      |
      +-- session_invalid -> latch + 401
      +-- entity is a bot -> 422 NOT_A_CHANNEL
      +-- status not in SEARCHABLE_STATUSES -> 403/404 with the probe's own reason
      v
Telegram: messages.search on the resolved entity
      |
      v
audit SEARCH_CHANNEL (source_id = null) -> API Response 200
```

**Response.** Identical shape to 5.22.

**HTTP Status Codes.** Same as 5.22, except 404 additionally covers "no Telegram entity found for this identifier".

<div class="callout warn">
<strong>Cost:</strong> this endpoint spends <em>two</em> Telegram round trips before the search itself (resolve + read probe) and repeats them on every call. For a channel you query more than once, register it (<code>POST /api/sources</code>) and use <code>GET /api/sources/{id}/search</code> instead — that path skips the re-probe and is materially safer for the account's rate budget.
</div>

**Example**

```bash
curl -G "http://localhost:8000/api/telegram/channels/examplechannel/search" \
  --data-urlencode "q=invoice" --data-urlencode "limit=10"
```

## 5.24 POST /api/telegram/search/sources

**Purpose.** Runs the same per-source live search across a chosen set of registered sources, reporting which ones were skipped and why instead of failing the whole call.

**When to use.** A keyword sweep across your tracked estate.

**Authentication.** None for the caller. Requires an authorized Telegram session.

**Request Body**

```json
{
  "source_ids": [1, 3, 4],
  "q": "invoice",
  "limit": 20,
  "cursor": null,
  "sender_username": null,
  "from_date": null,
  "to_date": null,
  "media_type": null,
  "include_raw": false
}
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_ids` | integer[] or null | No | Sources to search. **Omit or `null` to search every source that is currently `MONITORING` with the flag enabled.** | `[1, 3]` |
| `q` | string | **Yes** | Search text. Length is validated in the discovery layer (1–256), not by the schema. | `invoice` |
| `limit` | integer | No (default 20) | Per-source page size; clamped to 100 downstream. **No `ge`/`le` on the schema itself.** | `50` |
| `cursor` | string or null | No | Passed to every per-source search. See the caveat below. | `null` |
| `sender_username` | string or null | No | Native Telegram filter. | `poster` |
| `from_date` / `to_date` | datetime or null | No | Date bounds. | `null` |
| `media_type` | string or null | No | Native Telegram filter. | `photo` |
| `include_raw` | boolean | No (default `false`) | Include raw payloads. | `false` |

**Processing Flow**

```text
Client Request
      |
      v
SearchService.search_selected_sources() -> _require_session()
      |
      v
Resolve requested ids (missing -> skipped "source not found")
   or, if omitted, load every MONITORING + monitoring_enabled source
      |
      v
Partition: SEARCHABLE_STATUSES -> to_search ; everything else -> skipped (never queried)
      |
      v
FOR EACH source SEQUENTIALLY (one shared DB session; not asyncio.gather)
      |     rate_policy.run_bounded_search(per-source key)
      |     -> channel_search_by_source()  [same access rules, same audit log]
      |     -> TelegramSearchError becomes a `skipped` entry, not a failure
      v
Merge results -> API Response 200
```

**Response** (200)

```json
{
  "results": [ { "message_id": "72697", "channel": { "...": "..." }, "collection_method": "channel_search", "...": "..." } ],
  "searched_source_ids": [1, 3],
  "skipped": [ { "source_id": 4, "status": "JOIN_REQUEST_PENDING", "reason": "source is JOIN_REQUEST_PENDING, not currently searchable" } ],
  "warnings": []
}
```

| Field | Type | Description |
| --- | --- | --- |
| `results` | array | Merged results from every searched source (same item shape as 5.21). **Not re-sorted across sources** — they appear grouped in source order. |
| `searched_source_ids` | integer[] | Sources actually queried. |
| `skipped[].source_id` | integer | Source not queried. |
| `skipped[].status` | string or null | Its stored access status, or `null` when the id did not exist. |
| `skipped[].reason` | string | Why it was skipped, including translated Telegram errors such as `"FLOOD_WAIT: ..."`. |
| `warnings` | string[] | Always empty on this path today. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Even when every source was skipped. |
| 401 | Unauthorized | Telegram session unusable (the only whole-call failure). |
| 422 | Validation error | Malformed body. |

<div class="callout warn">
<strong>Pagination is not exposed here (Partially Implemented).</strong> The request accepts a <code>cursor</code>, but <code>MultiSourceSearchResponse</code> has no <code>pagination</code>/<code>next_cursor</code> field — so there is no way to page a multi-source sweep. Also note a single <code>cursor</code> is applied to <em>every</em> source, though cursors are per-channel <code>offset_id</code> values; passing one from a different channel's response will produce meaningless offsets. Leave <code>cursor</code> null and use per-source search (5.22) when you need to page.
</div>

<div class="callout warn">
<strong>Cost scales linearly.</strong> Omitting <code>source_ids</code> issues at least two Telegram calls (resolve + search) for <em>every</em> monitoring-enabled source, sequentially, in one HTTP request. With many sources this is a long request and a large burst against one account's rate budget. Prefer an explicit <code>source_ids</code> list.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/search/sources \
  -H "Content-Type: application/json" \
  -d '{"q": "invoice", "source_ids": [1, 3], "limit": 25}'
```

## 5.25 POST /api/telegram/search/results/save

**Purpose.** Promotes one search result into stored data: registers the source if needed, **re-fetches the exact message live from Telegram**, and persists it through the same evidence-preserving path monitoring and backfill use.

**When to use.** This is the **only** way a search result is ever written to the database. Search itself is read-only and ephemeral.

**Authentication.** None for the caller. Requires an authorized Telegram session.

**Request Body**

```json
{
  "identifier": "@examplechannel",
  "telegram_message_id": 72697,
  "collection_method": "manual_collection",
  "monitoring_enabled": false
}
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `identifier` | string | **Yes** | `@username`, `t.me` link, or numeric id of the **source channel** the result came from. | `"@examplechannel"` |
| `telegram_message_id` | integer | **Yes** | Telegram's message id from the search result (`results[].message_id`). | `72697` |
| `collection_method` | string | No (default `manual_collection`) | Recorded on the message row. Free-form string; not validated against the `CollectionMethod` enum. | `"global_search"` |
| `monitoring_enabled` | boolean | No (default `false`) | Passed to registration if the source is new. Does not start monitoring. | `false` |

**Processing Flow**

```text
Client Request
      |
      v
SearchService.save_search_result()
      |
      v
SourceService.register_source(identifier)   [full dedup + access probe]
      |    already exists -> locate by the id carried on the error, else by identifier
      v
access_status in SEARCHABLE_STATUSES?  --no--> 403/404
      |
      v
_require_session() -> Telegram: get_entity -> Telegram: get_messages(ids=telegram_message_id)
      |
      +-- message is None -> 404 NOT_FOUND
      v
collector.normalize_message()
      |
      v
MessageService.persist_single_message()
      |    write data/raw_messages/<source>/<id>.json
      |    INSERT ... ON CONFLICT DO NOTHING  -> inserted true/false
      |    DELIBERATELY does not touch collection_checkpoints
      v
audit SEARCH_RESULT_SAVED -> API Response 201
```

**Response** (201)

```json
{
  "source": { "id": 1, "identifier": "@examplechannel", "access_status": "PUBLIC_ACCESSIBLE", "...": "..." },
  "message": { "id": 12805, "source_id": 1, "telegram_message_id": 72697, "text": "Example matching message", "...": "..." },
  "inserted": true
}
```

| Field | Type | Description |
| --- | --- | --- |
| `source` | object | The `SourceOut` for the (possibly newly registered) source. |
| `message` | object or null | The persisted `MessageOut` row. |
| `inserted` | boolean | `false` when the message was already stored (saved twice, or already collected by monitoring/backfill) — the call still succeeds with 201. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 201 | Created | Saved, or already present (`inserted: false`). |
| 401 | Unauthorized | Telegram session unusable. |
| 403 | Forbidden | The source is not currently searchable/readable. |
| 404 | Not Found | Message id not found on that source. |
| 409 | Conflict | `{"error": {"code": "NOT_FOUND", ...}}` — the source exists but could not be located (rare edge case). |
| 422 | Unprocessable | `identifier` is not a valid Telegram identifier. |

<div class="callout info">
The message is <strong>re-fetched from Telegram</strong> rather than trusting the client's copy, so stored evidence always matches what Telegram actually served at save time. The client only resubmits the identifying pair (source + message id).
</div>

<div class="callout warn">
<strong>Side effect:</strong> if <code>identifier</code> is not yet registered, this endpoint <strong>creates a source row</strong> and performs a live access probe. It is not a pure "save" operation.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/search/results/save \
  -H "Content-Type: application/json" \
  -d '{"identifier": "@examplechannel", "telegram_message_id": 72697, "collection_method": "global_search"}'
```

<!--PAGEBREAK-->

## 5.26 GET /api/telegram/channels/search

**Purpose.** **Channel/group discovery** — finds candidate channels matching a keyword via `contacts.search`. Returns **channels, not messages**.

**When to use.** Finding sources worth registering. Follow up with `POST /api/telegram/channels/{telegram_id}/register` or `POST /api/sources`.

**Authentication.** None for the caller. Requires an authorized Telegram session.

**Request**

```http
GET /api/telegram/channels/search?q=example&limit=20 HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `q` | string (query) | **Yes** | 1–256 characters. | `example` |
| `limit` | integer (query) | No (default 20) | 1–100. | `20` |

**Processing Flow**

```text
Client Request -> SearchService.discover_channels -> _require_session()
      -> rate_policy.run_bounded_search
      -> Telegram: contacts.search(q, limit)
      -> keep Channel/Chat results only, de-duplicate by id
      -> derive a COARSE access hint from entity flags (no extra RPC per result)
      -> audit CHANNEL_DISCOVERY -> API Response 200
```

**Response** (200)

```json
{
  "results": [
    {
      "telegram_id": "1234567890",
      "access_hash": "-7654321098765432109",
      "title": "Example Channel",
      "username": "examplechannel",
      "type": "channel",
      "public": true,
      "verified": false,
      "member_count": 15234,
      "access_status": "PUBLIC_LIKELY",
      "source_url": "https://t.me/examplechannel"
    }
  ],
  "pagination": { "limit": 20, "next_cursor": null, "has_more": false },
  "warnings": ["contacts.search has no native pagination - this is Telegram's complete response for this query."]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `results[].telegram_id` | string | Telegram's channel id. Use it with the register endpoint. |
| `results[].access_hash` | string or null | Telegram access hash, as a string. |
| `results[].title` / `username` | string or null | As returned. |
| `results[].type` | string | `channel` (broadcast) or `group` (megagroup/legacy chat). |
| `results[].public` | boolean | True when a username exists. |
| `results[].verified` | boolean or null | Telegram's verified flag. |
| `results[].member_count` | integer or null | `participants_count`, when exposed. |
| `results[].access_status` | string | **Coarse hint only**: `ALREADY_MEMBER`, `PUBLIC_LIKELY`, or `ACCESS_UNKNOWN`. These are **not** `SourceAccessStatus` values. |
| `results[].source_url` | string or null | `https://t.me/<username>` when public. |
| `pagination.next_cursor` / `has_more` | null / boolean | **Always `null` / `false`** — `contacts.search` has no pagination. |
| `warnings` | string[] | Always includes the no-pagination note. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Discovery executed. |
| 401 | Unauthorized | Telegram session unusable. |
| 422 | Unprocessable | `q`/`limit` invalid. |
| 429 | Too Many Requests | `FLOOD_WAIT`. |

<div class="callout warn">
<code>access_status</code> here is a <strong>guess derived from flags already present in the search response</strong>, deliberately avoiding an extra RPC per result. It is not the verified state machine value. Always confirm with <code>POST /api/sources</code> + <code>POST /api/sources/{id}/check-access</code> before treating a discovered channel as accessible.
</div>

**Example**

```bash
curl -G "http://localhost:8000/api/telegram/channels/search" --data-urlencode "q=example"
```

## 5.27 POST /api/telegram/channels/{telegram_id}/register

**Purpose.** Registers a channel discovered through 5.26 as a source. It is a thin wrapper over the same registration path as `POST /api/sources`.

**When to use.** Immediately after discovery, when you have a numeric `telegram_id` rather than a username.

**Authentication.** None for the caller. Telegram session needed for the immediate access probe.

**Request**

```http
POST /api/telegram/channels/1234567890/register?monitoring_enabled=true HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `telegram_id` | string (path) | Yes | The `telegram_id` from a discovery result (or any identifier `normalize_identifier` accepts). | `1234567890` |
| `monitoring_enabled` | boolean (**query**) | No (default `true`) | **Query parameter, not a body field.** Sets the flag only; does not start monitoring. | `false` |

**Request Body.** None.

**Processing Flow**

```text
Client Request -> SourceService.register_source(telegram_id, monitoring_enabled)
      -> identical path to POST /api/sources: normalize, dedup by identifier,
         insert DISCOVERED, check_access, dedup by telegram_entity_id
      -> API Response 201 (SourceOut)
```

**Response** (201) — `SourceOut` (see 5.7).

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 201 | Created | Registered. |
| 409 | Conflict | Already registered under this or another identifier (e.g. previously added by `@username`). |
| 422 | Unprocessable | Identifier not recognizable. |

<div class="callout info">
A channel already registered by <code>@username</code> and then re-registered by numeric id is correctly rejected with <strong>409</strong> — the second de-duplication pass compares the resolved <code>telegram_entity_id</code>. That 409 is the endpoint's contract, not a failure.
</div>

**Example**

```bash
curl -X POST "http://localhost:8000/api/telegram/channels/1234567890/register?monitoring_enabled=false"
```

## 5.28 POST /api/telegram/discovery/extract-links

**Purpose.** Scans free text for Telegram links, classifies each one, and reports its **live access state** — a read-only preview. Nothing is registered, joined or monitored.

**When to use.** Triaging links found inside a collected message before deciding what to track.

**Authentication.** None for the caller. A Telegram session is needed to resolve each candidate; without one, candidates are returned with `access_status: null` and the session error in `reason`.

**Request Body**

```json
{ "text": "join us at https://t.me/examplechannel and https://example.com/ignored" }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `text` | string | **Yes** | Free text to scan. Only `http(s)://` URLs are matched. | see above |

**Processing Flow**

```text
Client Request
      |
      v
SourceService.discover_linked_channels()
      |
      v
extract_telegram_urls(text)     regex for http(s) URLs; urllib parsing ONLY - NEVER fetched
      |
      v
Keep hosts in {t.me, telegram.me, www.*} only; everything else discarded as non-Telegram
      |
      v
Cap at MAX_LINKS_PER_EXTRACTION = 20 candidates
      |
      v
FOR EACH candidate: look up existing source -> verify_authorized() -> access_manager.check_access()
      |                (one live Telegram probe per candidate)
      v
audit TELEGRAM_URL_CLASSIFIED per candidate -> COMMIT -> API Response 200
```

**Response** (200)

```json
{
  "candidates": [
    {
      "raw_url": "https://t.me/examplechannel",
      "identifier": "@examplechannel",
      "access_status": "PUBLIC_ACCESSIBLE",
      "title": "Example Channel",
      "username": "examplechannel",
      "already_registered_source_id": 1,
      "reason": null
    }
  ]
}
```

| Field | Type | Description |
| --- | --- | --- |
| `candidates[].raw_url` | string | The URL exactly as found in the text. |
| `candidates[].identifier` | string or null | Normalized identifier; `null` when the Telegram URL could not be parsed into one. |
| `candidates[].access_status` | string or null | A real `SourceAccessStatus` from a live probe; `null` when no probe ran. |
| `candidates[].title` / `username` | string or null | From resolution. |
| `candidates[].already_registered_source_id` | integer or null | Existing source id, if this is already tracked. |
| `candidates[].reason` | string or null | Probe explanation, parse failure, or session error. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Always, including an empty `candidates` array. |
| 422 | Validation error | `text` missing. |

<div class="callout ok">
<strong>Hard rule enforced in <code>url_classifier.py</code>:</strong> non-Telegram URLs are classified from the string alone and <strong>never fetched</strong>. Only <code>t.me</code> and <code>telegram.me</code> are recognized as Telegram hosts — shorteners and unknown domains are discarded, never followed to "find out".
</div>

<div class="callout warn">
<strong>Cost:</strong> up to 20 live Telegram probes in a single HTTP request (each up to two RPCs). This endpoint is not covered by the search concurrency semaphore. Use it on individual messages, not in a loop over a corpus.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/discovery/extract-links \
  -H "Content-Type: application/json" \
  -d '{"text": "see https://t.me/examplechannel and https://example.com/ignored"}'
```

## 5.29 POST /api/telegram/bots/{source_id}/start

**Purpose.** The API equivalent of pressing "Start Bot" in the Telegram app: sends exactly one `/start` message to a registered bot, waits (bounded) for its reply, and registers any Telegram links found in that reply as **dormant** sources.

**When to use.** When a bot is the gateway to a set of channels. Operator-initiated only — nothing schedules this.

**Authentication.** None for the caller. Requires an authorized Telegram session **and** a source whose `source_type` is exactly `bot`.

**Request**

```http
POST /api/telegram/bots/5/start HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Id of a registered source whose `source_type == "bot"`. | `5` |

**Request Body.** None.

**Processing Flow**

```text
Client Request
      |
      v
BotService.start_bot()
      |
      v
Load source -> 404 if missing -> 422 if source_type != "bot"
      |
      v
verify_authorized() -> Telegram: get_entity
      |
      v
audit TELEGRAM_BOT_START_REQUESTED -> COMMIT
      |
      v
bot_interaction.start_bot()
      |    Telegram: messages.sendMessage("/start")     <-- the ONLY message this service ever sends
      |    poll Telegram: iter_messages(limit=10) every BOT_START_POLL_INTERVAL_SECONDS
      |    until a newer inbound message appears or BOT_START_TIMEOUT_SECONDS elapses
      v
Scan reply text + inline button URLs for t.me/telegram.me links ONLY
      |    non-Telegram URLs classified and discarded, NEVER fetched
      |    callback buttons surfaced (text only) but NEVER executed
      |    cap at MAX_LINKS_PER_BOT_RESPONSE = 10
      v
FOR EACH link: register as DORMANT source (monitoring_enabled = false,
               discovery_type = "BOT_RESPONSE", discovered_from_source_id = bot)
               or refresh access if already registered
      |
      v
audit TELEGRAM_LINK_DISCOVERED / TELEGRAM_INVITE_RESOLVED -> API Response 200
```

**Response** (200)

```json
{
  "source": { "id": 5, "identifier": "@examplebot", "source_type": "bot", "...": "..." },
  "status": "started",
  "response_text": "Welcome! Pick a channel: https://t.me/examplechannel",
  "response_message_id": 900,
  "response_date": "2026-09-06T09:45:03Z",
  "buttons": [ { "text": "Open channel", "url": "https://t.me/examplechannel" } ],
  "discovered": [
    {
      "source_id": 8,
      "identifier": "@examplechannel",
      "access_status": "PUBLIC_ACCESSIBLE",
      "source_type": "channel",
      "already_existed": false,
      "raw_url": "https://t.me/examplechannel"
    }
  ],
  "error": null
}
```

| Field | Type | Description |
| --- | --- | --- |
| `source` | object | The bot source (`SourceOut`). |
| `status` | string | `"started"`, `"timed_out"`, or `"failed"`. |
| `response_text` | string or null | The bot's reply body. |
| `response_message_id` | integer or null | Telegram id of the reply. |
| `response_date` | datetime or null | UTC timestamp of the reply. |
| `buttons[].text` | string | Inline button label. |
| `buttons[].url` | string or null | Button URL; `null` for callback buttons (surfaced but never executed). |
| `discovered[]` | array | Sources registered or refreshed from the reply's links. |
| `discovered[].already_existed` | boolean | True when the link pointed at a source already tracked. |
| `error` | string or null | Failure or timeout reason. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Including `status: "timed_out"` and `status: "failed"` — these are body states, not HTTP errors. |
| 404 | Not Found | No such source. |
| 422 | Unprocessable | The source exists but is not a bot. |

**Error Responses**

```json
{ "detail": "source 1 is a 'channel', not a bot - the bot-start workflow only applies to SourceType.BOT sources" }
```

```json
{ "source": { "...": "..." }, "status": "timed_out", "error": "no response from bot within timeout", "buttons": [], "discovered": [] }
```

<div class="callout crit">
<strong>This is the only endpoint in the service that sends a message to Telegram.</strong> It is unauthenticated. The message is always the literal string <code>/start</code> and nothing else — no arbitrary text, no button clicks, no callback execution — but repeated calls do send repeated <code>/start</code> messages, with no cooldown or de-duplication. That is visible outbound activity on the account.
</div>

<div class="callout info">
Discovered channels are registered <strong>dormant</strong>: monitoring disabled, never auto-joined. Acting on them stays an explicit operator decision via <code>POST /api/sources/{id}/request-access</code> and <code>.../monitoring/start</code>.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/bots/5/start
```

## 5.30 POST /api/telegram/invites/join

**Purpose.** Joins — or submits an approval request for — a Telegram source using a raw invite link.

**When to use.** You hold a `t.me/+hash` or `t.me/joinchat/hash` link. For plain `@usernames`, use `POST /api/sources` followed by `.../request-access` instead.

**Authentication.** None for the caller. Requires an authorized Telegram session.

**Request Body**

```json
{ "identifier": "https://t.me/+AbCdEfGhIjKlM" }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `identifier` | string | **Yes** | Invite link (`t.me/+hash`, `t.me/joinchat/hash`) or the bare hash. **A username is rejected with 422.** | `"https://t.me/+AbCdEfGhIjKlM"` |

**Processing Flow**

```text
Client Request
      |
      v
SourceService.join_by_invite()
      |
      v
normalize_identifier -> must be INVITE_HASH, else 422
      |
      v
register_source(monitoring_enabled = false)   [Telegram: messages.checkChatInvite via check_access]
      |    already exists -> load it and re-check access
      v
Branch on resulting access_status:
      ACCESSIBLE/JOINED/PUBLIC_ACCESSIBLE/MONITORING -> "already_member"   (no join RPC sent)
      JOIN_REQUEST_PENDING                           -> "already_pending"  (no duplicate request)
      not JOIN_REQUEST_REQUIRED and not ERROR        -> "failed"
      |
      v
request_access()  ->  Telegram: messages.importChatInvite
      |
      v
final status -> "joined" | "pending_approval" | "failed"
      |
      v
audit TELEGRAM_JOIN_REQUESTED / _COMPLETED / _REQUEST_PENDING / _ALREADY_MEMBER / _FAILED
      |
      v
API Response 200
```

**Response** (200)

```json
{
  "source": { "id": 9, "identifier": "https://t.me/+AbCdEfGhIjKlM", "access_status": "JOIN_REQUEST_PENDING", "...": "..." },
  "status": "pending_approval",
  "reason": "join request sent - awaiting admin approval"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `source` | object | The registered source (`SourceOut`). |
| `status` | string | `"joined"`, `"already_member"`, `"pending_approval"`, `"already_pending"`, or `"failed"`. |
| `reason` | string or null | The source's `status_reason`, or the failure explanation. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | All join outcomes, including `"failed"`. |
| 422 | Unprocessable | Not an invite link, or an unparseable identifier. |

**Error Responses**

```json
{ "detail": "'@examplechannel' is not a Telegram invite link (t.me/+hash or t.me/joinchat/hash) - use POST /api/sources for a channel/group username instead" }
```

<div class="callout ok">
<strong>Idempotent by design:</strong> an already-accessible source is reported as <code>already_member</code> with <em>no</em> RPC sent, and a pending request returns <code>already_pending</code> — a duplicate join is never submitted, however many times this is called.
</div>

<div class="callout crit">
Unauthenticated, and it changes the Telegram account's real-world membership. A caller who reaches this port can make the account join channels. Rapid repeated joins are a well-known trigger for Telegram account restrictions (see section 10).
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/telegram/invites/join \
  -H "Content-Type: application/json" \
  -d '{"identifier": "https://t.me/+AbCdEfGhIjKlM"}'
```

<!--PAGEBREAK-->

## 5.31 POST /api/sources/{source_id}/backfill

**Purpose.** Queues a **background** job that walks *backward* into a source's history, collecting older messages. Returns immediately with a job id.

**When to use.** After monitoring is established and you need history older than the point collection began.

**Authentication.** None for the caller. Requires a source with verified access; the Telegram session is validated inside the background job.

**Request**

```http
POST /api/sources/1/backfill HTTP/1.1
Host: localhost:8000
Content-Type: application/json
```

**Request Body** (optional — omit entirely for defaults)

```json
{ "from_date": "2026-01-01T00:00:00Z", "to_date": "2026-06-01T00:00:00Z", "limit": 1000 }
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Source id. | `1` |
| `from_date` | datetime or null | No | **Stop** boundary — the walk breaks when messages older than this are reached. | `2026-01-01T00:00:00Z` |
| `to_date` | datetime or null | No | **Start** boundary — used as `offset_date` on a fresh job. Ignored when resuming (the job's own checkpoint wins). | `2026-06-01T00:00:00Z` |
| `limit` | integer | No (default 1000) | Maximum messages, 1–10000 (schema-validated and re-clamped in the service). | `2000` |

**Processing Flow**

```text
Client Request
      |
      v
BackfillService.start_backfill()
      |
      v
Source exists?  --no--> 404
access_status in {PUBLIC_ACCESSIBLE, ACCESSIBLE, JOINED, MONITORING}?  --no--> 409
      |
      v
INSERT backfill_jobs (status = PENDING) + audit BACKFILL_STARTED + COMMIT
      |
      v
asyncio.create_task(_run_backfill_job(...))     <-- fire and forget, same process
      |
      v
API Response 202 (job id)          [the background task then runs independently:]
                                    status = RUNNING, own DB session
                                    verify_authorized -> get_entity
                                    Telegram: iter_messages backward
                                        (offset_id = job checkpoint, else offset_date = to_date)
                                    persist per message; advance job.checkpoint_message_id
                                        (the OLDEST id seen - never the monitoring checkpoint)
                                    status = COMPLETED | PARTIAL | FAILED
                                    audit BACKFILL_COMPLETED / BACKFILL_FAILED
```

**Response** (202)

```json
{
  "id": 1,
  "source_id": 1,
  "status": "PENDING",
  "from_date": "2026-01-01T00:00:00Z",
  "to_date": "2026-06-01T00:00:00Z",
  "requested_limit": 1000,
  "messages_found": 0,
  "messages_inserted": 0,
  "messages_skipped": 0,
  "checkpoint_message_id": null,
  "error_count": 0,
  "error_message": null,
  "started_at": null,
  "completed_at": null,
  "created_at": "2026-09-06T10:00:00Z"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `id` | integer | Job id — use it with `GET .../backfill/{job_id}`. |
| `status` | string | `PENDING`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`. |
| `requested_limit` | integer | The clamped limit actually applied. |
| `messages_found` / `messages_inserted` / `messages_skipped` | integer | Progress counters; `skipped` means already stored (dedup). |
| `checkpoint_message_id` | integer or null | Oldest message id persisted so far — the resume point. |
| `error_count` / `error_message` | integer / string | Persistence or walk failures. |
| `started_at` / `completed_at` | datetime or null | Job lifecycle timestamps. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 202 | Accepted | Job queued. |
| 404 | Not Found | No such source. |
| 409 | Conflict | Source access not positively verified. |

**Error Responses**

```json
{ "detail": "source 2 is in state JOIN_REQUEST_PENDING; backfill requires verified access first" }
```

<div class="callout warn">
<strong>Durability gap (ISS-02).</strong> The job runs as an in-process <code>asyncio</code> task. If the service restarts while it is running, the row stays <code>RUNNING</code> forever and nothing resumes it — there is no startup sweep and no cancel endpoint. Recovery is manual: start a new backfill, which resumes safely from <code>checkpoint_message_id</code>.
</div>

<div class="callout info">
Backfill maintains its <strong>own</strong> checkpoint and never advances <code>collection_checkpoints</code>, so it cannot corrupt the forward monitoring pipeline. There is also no concurrency guard: several backfills can be queued for one source at once, all competing for the same Telegram budget.
</div>

**Example**

```bash
curl -X POST http://localhost:8000/api/sources/1/backfill \
  -H "Content-Type: application/json" \
  -d '{"limit": 500, "from_date": "2026-01-01T00:00:00Z"}'
```

```python
import httpx, time

job = httpx.post("http://localhost:8000/api/sources/1/backfill", json={"limit": 500}).json()
while True:
    j = httpx.get(f"http://localhost:8000/api/sources/1/backfill/{job['id']}").json()
    print(j["status"], j["messages_inserted"], "/", j["requested_limit"])
    if j["status"] in ("COMPLETED", "PARTIAL", "FAILED"):
        break
    time.sleep(5)
```

## 5.32 GET /api/sources/{source_id}/backfill

**Purpose.** Lists backfill jobs for one source, newest first (most recent 20).

**Authentication.** None. Database only.

**Request Parameters**

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Source id. | `1` |

**Processing Flow**

```text
Client Request -> BackfillService.list_backfill_jobs -> SELECT ... ORDER BY id DESC LIMIT 20
```

**Response** (200) — array of `BackfillJobResponse` (fields as in 5.31).

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Including an empty array for an unknown source (no 404). |

**Example**

```bash
curl http://localhost:8000/api/sources/1/backfill
```

## 5.33 GET /api/sources/{source_id}/backfill/{job_id}

**Purpose.** Polls one backfill job's progress.

**Authentication.** None. Database only.

**Request Parameters**

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `source_id` | integer (path) | Yes | Source id — must match the job's owner. | `1` |
| `job_id` | integer (path) | Yes | Job id from the 202 response. | `1` |

**Processing Flow**

```text
Client Request -> BackfillService.get_backfill_job -> SELECT WHERE id = ? AND source_id = ? -> 404 if None
```

**Response** (200) — one `BackfillJobResponse`.

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Job found for that source. |
| 404 | Not Found | `{"detail": "backfill job 7 not found for source 1"}`. |

**Example**

```bash
curl http://localhost:8000/api/sources/1/backfill/1
```

## 5.34 GET /api/notifications

**Purpose.** Reads persisted application notifications — significant access/collection state changes.

**When to use.** Polling for `TELEGRAM_ACCESS_GRANTED` after a join request, or `TELEGRAM_COLLECTION_ERROR` alerts.

**Authentication.** None. Database only.

**Request**

```http
GET /api/notifications?limit=50&offset=0&unread_only=true HTTP/1.1
Host: localhost:8000
```

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `limit` | integer (query) | No (default 50) | Page size, unbounded in code. | `50` |
| `offset` | integer (query) | No (default 0) | Rows to skip. | `0` |
| `unread_only` | boolean (query) | No (default `false`) | Only rows with `is_read = false`. | `true` |

**Processing Flow**

```text
Client Request -> NotificationService.list_notifications -> SELECT ... ORDER BY id DESC LIMIT/OFFSET
```

**Response** (200)

```json
[
  {
    "id": 1,
    "event_type": "TELEGRAM_ACCESS_GRANTED",
    "source_id": 2,
    "telegram_entity_id": "1234567890",
    "source_name": "Example Private Channel",
    "previous_status": "JOIN_REQUEST_PENDING",
    "new_status": "JOINED",
    "payload": {
      "event_type": "TELEGRAM_ACCESS_GRANTED",
      "source_id": 2,
      "telegram_entity_id": "1234567890",
      "source_name": "Example Private Channel",
      "previous_status": "JOIN_REQUEST_PENDING",
      "new_status": "JOINED",
      "timestamp": "2026-09-06T21:20:00Z"
    },
    "is_read": false,
    "created_at": "2026-09-06T21:20:00Z"
  }
]
```

| Field | Type | Description |
| --- | --- | --- |
| `id` | integer | Notification id. |
| `event_type` | string | `TELEGRAM_ACCESS_GRANTED`, `TELEGRAM_ACCESS_REJECTED`, or `TELEGRAM_COLLECTION_ERROR`. (`TELEGRAM_MONITORING_STARTED` is declared but never emitted.) |
| `source_id` | integer or null | Related source. |
| `telegram_entity_id` | string or null | Telegram id of that source. |
| `source_name` | string or null | Title, username, or identifier. |
| `previous_status` / `new_status` | string or null | The transition that raised it. |
| `payload` | object | Full JSON payload, also what any registered dispatcher receives. |
| `is_read` | boolean | Set by `POST /api/notifications/{id}/read`. |
| `created_at` | datetime | Creation timestamp. |

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Including an empty array. |

<div class="callout info">
The <code>processed</code> column (used by the dispatch job) is <strong>not</strong> exposed in the response — <code>is_read</code> is an operator-facing flag, <code>processed</code> an internal dispatch flag. With no dispatcher registered (the default), notifications simply accumulate here.
</div>

**Example**

```bash
curl "http://localhost:8000/api/notifications?unread_only=true&limit=20"
```

## 5.35 POST /api/notifications/{notification_id}/read

**Purpose.** Marks one notification as read.

**Authentication.** None. Database only.

**Request Parameters**

| Parameter | Type | Required | Description | Example |
| --- | --- | --- | --- | --- |
| `notification_id` | integer (path) | Yes | Notification id. | `1` |

**Processing Flow**

```text
Client Request -> NotificationService.mark_read -> is_read = true -> flush (NO COMMIT)
      -> response serialized from the in-memory object
      -> get_db closes the session -> TRANSACTION ROLLED BACK -> flag lost
```

<div class="callout crit">
<strong>Confirmed defect ISS-01 (same root cause as PATCH /api/sources/{id}).</strong> <code>NotificationRepository.mark_read</code> flushes but never commits, and no caller commits. The endpoint returns <strong>200 with <code>is_read: true</code></strong>, but the row is unchanged on the next read — so <code>?unread_only=true</code> keeps returning the same notification forever. Verified against SQLAlchemy 2.0.52 + aiosqlite. One-line fix: commit in <code>NotificationService.mark_read</code>.
</div>

**Response** (200) — the updated `NotificationOut` with `is_read: true` (in the response body only, see above).

**HTTP Status Codes**

| Status | Meaning | When It Occurs |
| --- | --- | --- |
| 200 | OK | Marked read. |
| 404 | Not Found | `{"detail": "notification not found"}`. |

**Example**

```bash
curl -X POST http://localhost:8000/api/notifications/1/read
```
<!--PAGEBREAK-->

# 6. End-to-End Workflows

Each workflow below is labelled **Implemented**, **Partially Implemented**, or **Not Implemented** based on what the code actually does.

## 6.1 The access state machine (the backbone of every workflow)

Thirteen states, with every transition validated against an explicit allow-list in `access_manager.ALLOWED_TRANSITIONS`. An invalid transition is refused and logged; it does **not** raise through to the caller — `_apply_status` leaves `access_status` untouched and records the attempt in `status_reason`.

```text
                              DISCOVERED
                                   |
        +------------------+-------+-------+------------------+---------------+
        |                  |               |                  |               |
        v                  v               v                  v               v
PUBLIC_ACCESSIBLE     ACCESSIBLE   JOIN_REQUEST_REQUIRED  ACCESS_DENIED    NOT_FOUND
        |                  |               |                  |               |
        |                  |               v                  v               v
        |                  |     JOIN_REQUEST_PENDING       DISABLED       DISABLED
        |                  |          |            |
        |                  |  approved|            |rejected
        |                  |          v            v
        |                  |  JOIN_REQUEST_    JOIN_REQUEST_
        |                  |    APPROVED         REJECTED
        |                  |          |               |
        |                  |          v               v
        |                  |       JOINED         DISABLED
        |                  |          |
        +------------------+----------+
                           |
                           v
                      MONITORING  <---- (self-transition allowed)
                           |
                           v
                      ERROR / DISABLED

ERROR is recoverable: an explicit POST /check-access can move it back to anything
DISCOVERED can reach. DISABLED can only return to DISCOVERED.
```

**Hard invariant, enforced centrally in `SourceService._apply_status`:** `monitoring_enabled` is forced to `false` whenever `access_status` is set to `ERROR`, `ACCESS_DENIED`, `NOT_FOUND`, `JOIN_REQUEST_REQUIRED`, `JOIN_REQUEST_PENDING`, `JOIN_REQUEST_REJECTED` or `DISABLED`.

## 6.2 Telegram Account Authentication Flow — **Implemented**

```text
Configure .env (TELEGRAM_API_ID / TELEGRAM_API_HASH)
      |
      v
Start service  ->  lifespan: init_db -> connect (best effort) -> scheduler.start()
      |
      v
GET /api/telegram/status          authenticated == false
      |
      v
POST /api/telegram/auth/send-code            -> Telegram: auth.sendCode
      |                                         phone_code_hash held IN MEMORY only
      v
POST /api/telegram/auth/verify-code          -> Telegram: auth.signIn
      |
      +-- requires_password == true --> POST /api/telegram/auth/verify-password
      |                                    -> Telegram: auth.checkPassword
      v
Session file authorized at TELEGRAM_SESSION_PATH   (clear_session_invalid())
      |
      v
GET /api/telegram/status          authenticated == true, masked account returned
      |
      v
Every restart from here on reconnects and reuses the session - no repeat login
```

<div class="callout warn">
<strong>Restart caveat:</strong> the pending <code>phone_code_hash</code> lives only in process memory. If the service restarts between <code>send-code</code> and <code>verify-code</code>, <code>verify-code</code> returns <code>"no pending code request for this phone - call send-code first"</code> and the flow must restart. The multi-step login is also <strong>not safe across multiple service replicas</strong> — a second replica has neither the pending hash nor the session file.
</div>

## 6.3 Source Discovery and Registration Flow — **Implemented**

```text
Operator input (keyword)  OR  a link found in a collected message
      |                                    |
      v                                    v
GET /api/telegram/channels/search    POST /api/telegram/discovery/extract-links
   (Telegram: contacts.search)          (URL parsing only; then one live probe
      |                                  per candidate, capped at 20)
      |                                    |
      +----------------+-------------------+
                       |
                       v
        Review candidates - COARSE hints only, not verified access
                       |
                       v
        POST /api/sources                (or POST /api/telegram/channels/{id}/register)
                       |
                       v
        normalize_identifier -> dedup by identifier -> INSERT (DISCOVERED)
                       |
                       v
        check_access -> Telegram probe -> real access_status recorded
                       |
                       v
        dedup by resolved telegram_entity_id -> 409 if the same channel already tracked
                       |
                       v
        Source registered with a verified status
```

<div class="callout ok">
<strong>Discovery is never monitoring.</strong> No discovery path auto-joins or auto-monitors. Every candidate requires an explicit operator call to be tracked, and bot-discovered sources are registered dormant.
</div>

## 6.4 Private Source Access Flow (join request) — **Implemented**, with a Telegram-imposed blind spot

```text
POST /api/sources                     -> access_status = JOIN_REQUEST_REQUIRED
      |
      v
POST /api/sources/{id}/request-access
      |
      +-- a PENDING request already exists? -> return it, send NOTHING to Telegram
      |
      v
Telegram: channels.joinChannel (username)  OR  messages.importChatInvite (invite hash)
      |
      +-- joined immediately        -> JOINED       + notification TELEGRAM_ACCESS_GRANTED
      +-- InviteRequestSentError    -> JOIN_REQUEST_PENDING (access-request row, next_check_at +12h)
      +-- error / FloodWait         -> ERROR
      |
      v
Background job reconcile_pending_access, every ACCESS_RECONCILIATION_INTERVAL_HOURS (12)
      |
      +-- invite hash  -> Telegram: messages.checkChatInvite
      |        ChatInviteAlready              -> APPROVED
      |        InviteHashExpired/Invalid      -> REJECTED
      |        ChatInvite                     -> still PENDING
      |
      +-- username     -> Telegram: get_entity, inspect the `left` flag
               left == False                  -> APPROVED
               otherwise                      -> still PENDING  (see blind spot below)
      |
      v
APPROVED -> JOIN_REQUEST_APPROVED -> JOINED -> notification -> start_monitoring() AUTOMATICALLY
REJECTED -> JOIN_REQUEST_REJECTED -> monitoring_enabled = false -> notification
            No alternative access mechanism is ever attempted afterwards.
```

<div class="callout warn">
<strong>Telegram platform limitation:</strong> for a <em>username-based</em> join request that an admin silently declines, Telegram exposes no decline signal to the requesting account. The request accurately stays <code>JOIN_REQUEST_PENDING</code> indefinitely. This is what the API exposes, not a defect — but it means a pending request is not proof that a decision is still outstanding.
</div>

## 6.5 Message Monitoring / Collection Flow — **Implemented**

```text
POST /api/sources/{id}/monitoring/start
      |
      v
verify_authorized -> status check -> transition to MONITORING -> immediate first cycle
      |
      v
Scheduler job run_collection_cycle, every COLLECTION_INTERVAL_MINUTES (5), max_instances=1
      |
      v
FOR EACH source WHERE monitoring_enabled = true AND access_status = 'MONITORING':
      |
      v
verify_authorized()
      |   transient network failure -> skip this cycle, monitoring stays ON
      |   session invalid           -> source -> ERROR, monitoring OFF, stop
      v
Telegram: get_entity
      |
      v
checkpoint = collection_checkpoints.get_or_create(source)
      |
      v
Telegram: iter_messages(min_id = checkpoint.last_message_id, reverse=True,
                        limit = COLLECTION_BATCH_LIMIT)      forward, oldest-first
      |
      +-- FloodWaitError        -> stop, report wait, keep what was already fetched
      +-- ChannelPrivateError   -> "access was revoked" -> source -> ERROR + notification
      +-- session invalid       -> latch + source -> ERROR
      |
      v
FOR EACH message:  normalize -> write raw evidence JSON -> INSERT ... ON CONFLICT DO NOTHING
                   -> advance checkpoint -> COMMIT     (one transaction per message)
      |
      v
Optional media download when MEDIA_DOWNLOAD_ENABLED=true, within size/type policy
      |
      v
Update source.last_message_id / last_collected_at -> audit -> structured JSON log line
```

<div class="callout ok">
<strong>Restart safety:</strong> per-message commits mean the checkpoint only ever advances past durably persisted messages. An interrupted cycle resumes at exactly the right point, and the DB unique constraint makes re-collection a no-op rather than a duplicate.
</div>

## 6.6 Live Search Flow — **Implemented**

```text
Search request
      |
      v
_require_session()        -> 401 immediately if the session is latched invalid (no RPC)
      |
      v
rate_policy.run_bounded_search(key)
      |   semaphore: max 3 concurrent search RPCs across all callers
      |   in-flight de-duplication: identical concurrent calls share ONE execution
      v
Global search                        Channel search
      |                                     |
Telegram: checkSearchPostsFlood       registered source: trust stored status
      |                               arbitrary channel: fresh live probe
      +-- quota -> searchPosts               |
      +-- else  -> searchGlobal              v
      |                               Telegram: messages.search
      v                                      |
Normalize -> local filters -> sort -> cursor -+
      |
      v
audit SEARCH_GLOBAL / SEARCH_CHANNEL + structured log
      |
      v
API response      NOTHING IS PERSISTED - search is read-only and ephemeral
      |
      v
(optional) POST /api/telegram/search/results/save   <- the ONLY path search data is stored
```

## 6.7 Historical Backfill Flow — **Partially Implemented**

Implemented: queueing, access gating, the backward walk, an independent per-job checkpoint, resume-from-checkpoint on a *new* job, progress polling, `PARTIAL`/`FAILED` outcomes.
Not implemented: crash recovery, cancellation, concurrency limits per source, and progress push.

```text
POST /api/sources/{id}/backfill  -> 202 with job id (status PENDING)
      |
      v
asyncio.create_task(_run_backfill_job)      IN-PROCESS, fire and forget
      |
      v
status = RUNNING; open an independent DB session
      |
      v
verify_authorized -> get_entity     (failure -> FAILED with the reason)
      |
      v
resume_before_id = job.checkpoint_message_id ; remaining = limit - messages_found
      |
      v
Telegram: iter_messages backward (offset_id = resume point, else offset_date = to_date)
      stop at from_date or when the limit is reached
      |
      v
persist per message -> advance job.checkpoint_message_id to the OLDEST id seen
      (collection_checkpoints is deliberately NEVER touched)
      |
      v
COMPLETED | PARTIAL (error but some inserted) | FAILED -> audit -> structured log
```

## 6.8 Bot Interaction Flow — **Implemented**

```text
POST /api/sources  with a bot username  -> source_type resolved to "bot"
      |                                    audit TELEGRAM_BOT_DETECTED
      v
POST /api/telegram/bots/{id}/start
      |
      v
Telegram: sendMessage("/start")      one message, nothing else, ever
      |
      v
Bounded poll (BOT_START_TIMEOUT_SECONDS / BOT_START_POLL_INTERVAL_SECONDS)
      |   returns as soon as a reply arrives; never waits out the full timeout needlessly
      v
Reply text + inline button URLs scanned for t.me/telegram.me links only
      |   non-Telegram URLs discarded, never fetched
      |   callback buttons surfaced but NEVER executed
      |   capped at 10 links
      v
Each link -> register as DORMANT source (monitoring off, discovery_type = BOT_RESPONSE)
      |
      v
Operator decides next steps via request-access / monitoring-start
```

<div class="callout info">
The bot's reply is <strong>never written to <code>telegram_messages</code></strong>, and no wider bot chat history is fetched. Only the one reply is inspected.
</div>

## 6.9 Notification Flow — **Partially Implemented**

```text
State change (access granted / rejected / collection error)
      |
      v
NotificationService.create() -> INSERT notifications (processed = false, is_read = false)
      |
      v
Scheduler job process_notifications, every NOTIFICATION_PROCESSING_INTERVAL_MINUTES (1)
      |
      v
notification_manager.dispatch(payload) -> every registered dispatcher
      |
      |   *** NO DISPATCHER IS REGISTERED BY DEFAULT ***
      |   The loop runs, dispatches to nobody, and marks the row processed.
      v
mark_processed -> COMMIT
      |
      v
GET /api/notifications        <- the only way anyone learns about it today
```

**Not Implemented:** WebSocket, webhook, email or dashboard delivery. `NotificationManager.register()` exists and works; no provider is wired in.

## 6.10 Not Implemented workflows

| Workflow | Status | Note |
| --- | --- | --- |
| Event-driven access-change fast path | **Not Implemented (dead code)** | `event_handler.register_access_change_handlers` is fully written and unit-tested but **never called** by the application. Contradicts README §11. |
| Sending arbitrary messages / posting / replying | **Not Implemented** | Only the literal `/start` to a bot. |
| Media re-download, retention or cleanup | **Not Implemented** | Downloads happen only during collection when enabled; nothing prunes. |
| Message export (CSV/JSON bulk) | **Not Implemented** | Only paged JSON via the API. |
| Multi-account rotation, proxies | **Not Implemented** | Single account, single connection. |
| NLP / OCR / entity extraction / correlation / risk scoring | **Not Implemented** | Explicit downstream scope. |
| Deleting or editing content on Telegram | **Not Implemented** | The service is read-only against Telegram apart from joins and `/start`. |

<!--PAGEBREAK-->

# 7. Telegram Accessibility and Privacy Behavior

Everything the service can see is exactly **what the one authenticated Telegram user account can see** — no more. This section states that boundary precisely.

## 7.1 Access by source type

| Source type | What the service can do | Status |
| --- | --- | --- |
| **Public channel** (has a username) | Resolve, read history, search within it, monitor, backfill, discover via `contacts.search`. Probe result: `PUBLIC_ACCESSIBLE`. | **Supported** |
| **Public group / supergroup** (has a username) | Same as public channels. Sender identity is usually richer than in broadcast channels. | **Supported** |
| **Private channel/group — account is already a member** | Full read, search, monitor, backfill. Probe result: `ACCESSIBLE` (`left == False`). | **Supported** |
| **Private channel/group with a username, not a member** | Detected as `JOIN_REQUEST_REQUIRED`. No content until a join is approved. | **Partially Supported** (access must be granted first) |
| **Private, invite link, joins immediately** | `messages.importChatInvite` succeeds -> `JOINED` -> readable. | **Supported** |
| **Private, invite link, approval required** | Request submitted once -> `JOIN_REQUEST_PENDING`. Content only after Telegram approves. | **Partially Supported** |
| **Private, no username and no invite** | `ACCESS_DENIED`. Nothing is attempted. | **Not Supported (by design)** |
| **Invite link expired / invalid** | `ACCESS_DENIED` / `NOT_FOUND`. No retry with a mutated hash, ever. | **Not Supported (by design)** |
| **Bot** | Detected as `source_type = "bot"`. Message search is explicitly refused (`NOT_A_CHANNEL`, 422). Only the `/start` workflow applies. | **Partially Supported** (start only) |
| **User account (a person)** | `check_access` returns `ACCESSIBLE` for any resolvable user **without a read probe**. See the privacy note below. | **Partially Supported** |
| **Restricted content** (geo/age/copyright restricted by Telegram) | Not handled specially. Telegram's own restriction applies to the account; results simply do not appear. | **Not Supported** |
| **Deleted or unavailable content** | Not recoverable. `save_search_result` returns 404 when the message is gone. Already-collected copies remain in local storage. | **Not Supported** |

<div class="callout crit">
<strong>Privacy note — user sources.</strong> <code>access_manager._check_username_or_id_access</code> returns <code>ACCESSIBLE</code> for a <code>User</code> entity immediately, skipping the read probe applied to channels. Because <code>ACCESSIBLE</code> is a monitorable state, a <em>person</em> or a <em>bot</em> can be registered as a source and put into <code>MONITORING</code>, at which point <code>collect_new_messages</code> walks <code>iter_messages(user_entity)</code> — which is the <strong>one-to-one conversation history between the service's account and that user</strong>. Only <em>channel search</em> rejects bots; registration, monitoring and backfill do not. The live database contains one such source today (a bot registered as <code>source_type = "user"</code>, in <code>MONITORING</code>, with 0 messages collected). This is a capability of the generic source path rather than a documented feature, and it deserves an explicit policy decision before the service is used more widely.
</div>

## 7.2 Capability matrix

| Capability | Status | Evidence in code |
| --- | --- | --- |
| Search public Telegram content beyond joined channels | **Partially Supported** | `channels.searchPosts` — only while free daily quota remains; the service never spends Stars. |
| Search Telegram-wide as a fallback | **Partially Supported** | `messages.searchGlobal` is account-scoped; a warning says so and `visibility` is `account_scoped`. |
| Read messages from public channels/groups | **Supported** | `iter_messages` after a verified probe. |
| Search messages within one channel | **Supported** | `messages.search`, native `from_user`/media/date filters. |
| Discover channels by keyword | **Supported** | `contacts.search`; no pagination available from Telegram. |
| Join public groups/channels | **Supported** | `channels.joinChannel` via `request-access`. |
| Join private groups via invite link | **Supported** | `messages.importChatInvite` via `invites/join`. |
| Access invite-only sources requiring approval | **Partially Supported** | Request submitted once; content only after approval; approval detected within 12h. |
| Detect that a join request was declined | **Partially Supported** | Invite-hash requests: yes. Username-based: **no signal exists** — stays PENDING. |
| Access content without appropriate Telegram authorization | **Not Supported — deliberately** | No invite guessing, no membership spoofing, no `ChannelPrivateError` workaround anywhere. |
| Send messages | **Partially Supported** | Exactly one literal `/start`, only to a registered bot source. Nothing else. |
| Retrieve historical messages | **Supported** | Backfill, max 10,000 per job, subject to Telegram history visibility. |
| Monitor sources continuously | **Supported** | 5-minute collection cycle, checkpointed. |
| Scrape/search messages | **Supported** within the account's access | Both live search and local search over collected data. |
| Download media | **Supported, disabled by default** | `MEDIA_DOWNLOAD_ENABLED=false`; metadata is always recorded regardless. |
| Read message reactions, poll results, comment threads, stories | **Not Implemented** | Not requested from Telegram and not modelled. Only `reply_count` is captured. |
| Enumerate channel members / participant lists | **Not Implemented** | No participants RPC is called anywhere. |
| Read the account's own dialog list | **Not Implemented** | `get_dialogs` is never called. |
| Access another Telegram account's data | **Not Supported** | Single account; no impersonation. |

## 7.3 What "public" means in this service

Three distinct meanings appear in responses, and conflating them is the most common misreading of the data:

| Value | Where it appears | Meaning |
| --- | --- | --- |
| `visibility: "public"` | Search results | The result came from `channels.searchPosts` (genuinely cross-membership), **or** from a channel that has a username. |
| `visibility: "account_scoped"` | Global search results | From `messages.searchGlobal` — Telegram's index **for this account**, in practice mostly channels it has joined. **Not** a search of all public Telegram. |
| `visibility: "member"` | Channel search results | The channel has no username; the service can read it only because the account is a member. |

<!--PAGEBREAK-->

# 8. Telegram API Limitations

Limitations are separated by who imposes them. This distinction matters for review: platform limitations cannot be engineered away; application limitations can.

## 8.1 Telegram Platform Limitations

| # | Limitation | Effect on this service |
| --- | --- | --- |
| TP-1 | **Access equals the account's permissions.** | Nothing can be collected that the authenticated account cannot see. No configuration changes this. |
| TP-2 | **`channels.searchPosts` is quota-gated.** A small free daily allowance (`channels.checkSearchPostsFlood`), then Telegram Stars or Premium. | Cross-membership public search degrades to the account-scoped fallback once the quota is spent. The service never pays automatically. |
| TP-3 | **`messages.searchGlobal` is account-scoped.** No Telegram method full-text searches every public channel for a standard account. | "Global search" results are not a complete view of Telegram. Surfaced via `visibility` and a `warnings` entry. |
| TP-4 | **`contacts.search` has no pagination.** No offset/cursor parameter exists. | Channel discovery is a single bounded call; `has_more` is always `false`. Documented rather than faked. |
| TP-5 | **No decline signal for username-based join requests.** | A silently declined request is indistinguishable from a pending one and stays `JOIN_REQUEST_PENDING`. |
| TP-6 | **`FloodWaitError`** — Telegram dictates an exact wait period after too many requests. | Every affected operation stops and reports `retry_after_seconds`. Never bypassed. |
| TP-7 | **Session invalidation** (revoked, logged out elsewhere, account deactivated/banned). | Latched; all Telegram operations return 401 until re-authentication. |
| TP-8 | **Metadata availability varies.** Sender identity, view counts and reply counts are often absent, especially in broadcast channels. | Stored as `NULL`, never fabricated. |
| TP-9 | **Deleted content is gone.** | Not recoverable. Already-collected copies persist locally. |
| TP-10 | **Private sources require membership or approval.** | Enforced by Telegram; the state machine simply reflects it. |
| TP-11 | **Invite links expire and can be revoked.** | Mapped to `ACCESS_DENIED` / rejection. No retry with a modified hash. |
| TP-12 | **Anti-abuse enforcement.** Rapid joins, bulk history reads and repeated searches can trigger restrictions up to a permanent ban. | Mitigated only partially — see section 10. |
| TP-13 | **History visibility rules.** A group may hide history from members who joined later. | Backfill silently returns less than requested; not distinguishable from "no more messages". |
| TP-14 | **Telegram may change API behaviour at any time.** | The pinned floor is `telethon>=1.36.0` with no upper bound — a breaking Telethon or Telegram change can surface without warning. |

## 8.2 Application Limitations

| # | Limitation | Impact | Fixable? |
| --- | --- | --- | --- |
| AP-1 | **No caller authentication or authorization.** | Anyone who can reach the port controls the Telegram account. | Yes — SEC-01. |
| AP-2 | **`PATCH /api/sources/{id}` and `POST /api/notifications/{id}/read` never commit.** | Both silently discard the change while returning 200. | Yes — ISS-01, one line each. |
| AP-3 | **Backfill jobs do not survive a restart.** | Rows stuck in `RUNNING`; no resume, no cancel. | Yes — ISS-02. |
| AP-4 | **Single Telegram account, single process.** | No horizontal scaling; API traffic and collection share one rate budget. | Architectural. |
| AP-5 | **No application-level rate limiting.** | Only a 3-way concurrency cap on *search*. Collection, access checks, joins and link extraction are uncapped. | Yes. |
| AP-6 | **No FloodWait backoff or scheduling.** | The wait is reported, never honoured automatically; a caller can immediately retry and worsen the situation. | Yes. |
| AP-7 | **Multi-source search returns no cursor.** | Sweeps cannot be paged. | Yes. |
| AP-8 | **Local search is substring-only.** | No boolean/nested queries, no full-text index, no relevance, no result totals. | Yes. |
| AP-9 | **Unbounded `limit` on stored-message and notification endpoints.** | A single request can attempt to materialize the whole table. | Yes — ISS-05. |
| AP-10 | **SQLite-specific insert.** `MessageRepository` uses `sqlalchemy.dialects.sqlite.insert`. | The "swap `DATABASE_URL` to Postgres" claim does not hold for this one method without a change. | Yes. |
| AP-11 | **No migration tool.** `create_all` plus a SQLite-only additive-column patch list. | Schema changes on other backends, and any column type change, are unmanaged. | Yes. |
| AP-12 | **`next_status_check_at` is never written.** | Always `null` in responses; no scheduled re-probing of source access exists. | Yes. |
| AP-13 | **Event-driven access detection is dead code.** | Approval detection latency is up to 12 hours, not near-real-time. | Yes — DOC-01. |
| AP-14 | **`telegram_accounts` identity columns are never populated.** | `telegram_user_id`, `username`, `phone_masked` stay `NULL`; the table records little more than "a session exists". | Yes. |
| AP-15 | **`request-access` returns 500 on an unusable session.** | `RuntimeError` is unmapped by the route. | Yes — ISS-03. |
| AP-16 | **Deleting a source orphans its raw-evidence files.** | `data/raw_messages/<id>/` is left on disk; audit rows keep a dangling `source_id`. | Yes. |
| AP-17 | **`source_type` in `SourceCreate` is accepted and ignored.** | Misleading API contract. | Yes. |
| AP-18 | **Stuck `scheduler_jobs` rows.** An unclean shutdown leaves `status = "RUNNING"` forever. One such row exists in the live database. | Cosmetic/observability. | Yes. |
| AP-19 | **`processing_status` is always `"raw"`.** | The `normalized`/`enriched` states are declared but never set. | By design (downstream scope). |

<!--PAGEBREAK-->

# 9. Security

Findings are rated **Critical / High / Medium / Low / Informational**. Only issues supported by the code are listed; nothing here is speculative.

## 9.1 Security architecture as implemented

| Area | Implementation |
| --- | --- |
| **API authentication** | **None.** No API key, token, cookie, CORS policy or IP restriction anywhere in `app/`. |
| **Authorization** | **None.** No roles, no per-source ownership, no scoping. Every caller can do everything. |
| **Telegram credentials** | `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` are read from the environment/`.env` only. No hard-coded defaults. Passed straight to Telethon; never logged, never returned by any endpoint. |
| **Session material** | The Telethon session file is a bearer credential for the Telegram account. Stored at `TELEGRAM_SESSION_PATH`, gitignored, never exposed through any endpoint. |
| **Secrets in transit** | Verification codes and the 2FA password are accepted as plain JSON over plain HTTP. |
| **Secrets management** | `.env` on local disk. No vault, no KMS, no encryption at rest. |
| **Database security** | Local SQLite file, no encryption, no DB authentication, filesystem permissions only. |
| **Input validation** | Pydantic v2 on every body; `Query` constraints on the search endpoints (`min_length`, `max_length`, `ge`, `le`, `pattern`); `normalize_identifier` rejects anything that is not a valid Telegram identifier; `clamp_limit` bounds page sizes in the discovery layer; `decode_cursor` rejects malformed cursors. |
| **SQL injection** | Not reachable — all queries are SQLAlchemy expression constructs. The only raw SQL is the startup `PRAGMA table_info` / `ALTER TABLE` patcher, built from a hard-coded internal list. |
| **SSRF** | Actively prevented: `url_classifier` parses URLs with `urllib` and **never performs an HTTP request**; only `t.me`/`telegram.me` are recognized, everything else is discarded rather than fetched. |
| **Logging** | Structured JSON via `log_structured`; search logs record `query_length`, not the query text. `mask_phone` masks phone numbers. Credential material is never logged. |
| **Sensitive data at rest** | Collected third-party message content and raw evidence JSON are stored unencrypted under `data/`. |
| **Error handling** | Stable error codes; messages carry only exception class names and Telegram's own public text. No stack traces are returned. |

## 9.2 Findings

### SEC-01 — No authentication or authorization on any endpoint · **High**

*(Critical in any deployment reachable beyond loopback — which the default `API_HOST=0.0.0.0` produces.)*

All 35 endpoints are open. An unauthenticated caller can start a Telegram login, submit the 2FA password, make the account **join channels** (`/api/telegram/invites/join`, `/api/sources/{id}/request-access`), **send `/start` messages** (`/api/telegram/bots/{id}/start`), drive unbounded Telegram search (risking FloodWait or an account restriction), read all collected third-party message content, and **delete sources with all their data**.

**Evidence:** no `middleware`, `Depends(security)`, `HTTPBearer`, `OAuth2`, `api_key` or `jwt` reference exists anywhere under `app/`. `app/main.py` registers only routers and one exception handler.

**Recommendation:** add an API-key or bearer dependency at router level; bind to `127.0.0.1` by default; place the service behind an authenticating reverse proxy. Treat the write/Telegram-affecting endpoints (auth, joins, bot-start, delete) as privileged even after that.

### SEC-02 — Telegram session file is an unencrypted bearer credential on disk · **Medium**

`data/telegram_service.session` grants full API access to the Telegram account without any password or 2FA challenge. It is correctly gitignored and never exposed through the API, but it is unencrypted and protected only by filesystem permissions. A copy of that file elsewhere is a full account takeover.

**Recommendation:** restrict directory permissions, exclude `data/` from backups that leave the host, and document a revocation procedure ("terminate other sessions" in Telegram) for suspected compromise.

### SEC-03 — Credentials accepted over plain HTTP · **Medium**

`auth/send-code`, `auth/verify-code` and `auth/verify-password` accept a phone number, a login code and the 2FA cloud password in plain JSON. The service speaks HTTP; no TLS is configured or referenced.

**Recommendation:** terminate TLS at a reverse proxy and never expose these endpoints beyond a trusted network segment.

### SEC-04 — `.env` with live credentials present in the working tree · **Medium**

A populated `.env` exists alongside the code with real `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` values. It is correctly gitignored (as are `*.session` and `data/`), so this is a host-security and handling concern, not a repository leak. *(No credential values are reproduced anywhere in this document.)*

**Recommendation:** move to a secret manager or environment injection for anything beyond a developer machine; rotate the api_hash if the host is shared.

### SEC-05 — Collected third-party content stored unencrypted with no retention policy · **Medium**

The live database holds **12,805 messages and 8,134 media metadata rows**, plus one raw-evidence JSON file per message under `data/raw_messages/`. This is real third-party personal data, unencrypted, with no expiry, no access control and no deletion mechanism beyond `DELETE /api/sources/{id}` (which does not remove the evidence files).

**Recommendation:** define retention and access rules before this data set grows or is shared; consider full-disk or per-directory encryption; add an evidence-cleanup path to source deletion.

### SEC-06 — No rate limiting on the application's own API · **Medium**

The only throttle is `SEARCH_CONCURRENCY_LIMIT = 3` on search RPCs. Nothing bounds request *rate*, and collection, access checks, link extraction (up to 20 live probes per call) and joins are not covered at all. Combined with SEC-01 this makes it straightforward for an unauthenticated caller to drive the account into a FloodWait or a restriction.

**Recommendation:** add per-client rate limiting at the proxy, and a global outbound Telegram request budget in `rate_policy`.

### SEC-07 — Unbounded `limit` on stored-data endpoints · **Low**

`GET /api/messages`, `GET /api/sources/{id}/messages`, `GET /api/sources` and `GET /api/notifications` declare `limit: int` with no `le` constraint. `limit=1000000` is accepted and attempted — a memory/CPU denial-of-service against the service itself (not against Telegram).

**Recommendation:** apply `Query(default=50, ge=1, le=200)` consistently.

### SEC-08 — Destructive endpoints without confirmation or soft-delete · **Low**

`DELETE /api/sources/{id}` cascades to messages, media, access requests and the checkpoint with no confirmation, no soft-delete and no undo. Combined with SEC-01, an accidental or hostile call is unrecoverable from the database alone.

**Recommendation:** soft-delete, or require an explicit confirmation parameter.

### SEC-09 — SQLite foreign keys not enforced · **Low**

The app never sets `PRAGMA foreign_keys=ON`, so `ON DELETE SET NULL` on `audit_logs.source_id` and `notifications.source_id` never fires. The code compensates in exactly one place (`AuditService.detach_source` during duplicate rejection); every other deletion path leaves dangling references.

**Recommendation:** enable the pragma on connect, or accept and document dangling audit references.

### SEC-10 — Dependency versions are floors with no upper bound · **Low**

Every entry in `requirements.txt` is `>=`, with no lock file. Builds are not reproducible and a breaking Telethon or FastAPI release can be pulled in silently.

**Recommendation:** pin exact versions or add a lock file.

### SEC-11 — Good practices confirmed · **Informational**

Verified positives, worth stating explicitly for review:

* `api_hash`, session data, verification codes and 2FA passwords are **never** logged or returned. `GET /api/telegram/status` masks the phone number.
* Search audit logs record `query_length`, never the query text.
* Non-Telegram URLs are never fetched (no SSRF surface).
* No raw SQL string interpolation on any user-controlled path.
* Error responses expose no stack traces and no internal file paths.
* `.gitignore` correctly excludes `.env`, `*.session*`, `data/` and PoC output.
* Callback buttons on bot replies are surfaced but never executed.

<!--PAGEBREAK-->

# 10. Account Safety and Operational Considerations

The Telegram account is the single most valuable and most fragile asset in this system. If it is restricted or banned, the service stops entirely.

## 10.1 Protections that ARE implemented

| Protection | Implementation | Where |
| --- | --- | --- |
| Invalid-session latch | Once Telegram rejects the auth key, every operation short-circuits with **no further RPC** until re-authentication. | `client.py` |
| Mandatory pre-flight | `verify_authorized()` before every Telegram operation, so a known-bad session never issues the caller's actual RPC. | `client.py` and all services |
| No retry-on-FloodWait loop | `FloodWaitError` stops the operation and reports `retry_after_seconds`. Nothing sleeps and retries. | collector, access_manager, join_request_manager, errors |
| Search concurrency cap | At most **3** concurrent search RPCs across all callers. | `rate_policy.SEARCH_CONCURRENCY_LIMIT` |
| In-flight de-duplication | Identical concurrent searches share one Telegram call instead of issuing N. | `rate_policy.InFlightDeduplicator` |
| No overlapping jobs | Every APScheduler job is `max_instances=1`, `coalesce=True`. | `scheduler/jobs.py` |
| Single join request per source | A PENDING request short-circuits the endpoint; no duplicate is ever sent. | `source_service.request_access` |
| No alternative access after rejection | A rejected request only moves toward `DISABLED`; nothing retries by another route. | state machine |
| No automatic payment | Exhausted `searchPosts` quota falls back rather than spending Telegram Stars. | `global_search.py` |
| Bounded page sizes | `clamp_limit` caps every search at 100; collection at `COLLECTION_BATCH_LIMIT` (200); backfill at 10,000. | pagination, config |
| Bounded link resolution | 20 probes per `extract-links` call, 10 per bot reply. | `source_service`, `bot_service` |
| Single `/start` per call | One message, no button clicks, no callback execution, bounded wait. | `bot_interaction.py` |
| Reconciliation stops early on session invalidation | The 12-hour job breaks out mid-cycle rather than hammering the remaining requests. | `scheduler/jobs.py` |
| Session-invalid detection disables monitoring | A source moves to `ERROR` and monitoring is switched off rather than retrying forever. | `monitoring_service` |
| Full audit trail | Every access check, join, search, collection and bot interaction writes an `audit_logs` row. | `audit_service` |

## 10.2 Protections that are NOT implemented (recommended)

<div class="callout warn">
Everything in this table is a <strong>recommendation</strong>. None of it exists in the code today.
</div>

| Gap | Risk | Recommendation |
| --- | --- | --- |
| **No FloodWait backoff or scheduling** | The wait is reported to the caller, but nothing enforces it. A retrying client can immediately re-trigger it and escalate toward a restriction. | Record the flood deadline centrally and refuse Telegram operations until it passes. |
| **No global outbound request budget** | Only search concurrency is bounded. Collection, access checks, joins and link extraction are uncapped. | Add a token-bucket in `rate_policy` covering every outbound RPC. |
| **No jitter or spacing between collection cycles** | Every monitored source is collected back-to-back on a fixed 5-minute boundary — a recognizably mechanical pattern. | Add jitter and inter-source delays. |
| **No cap on monitored sources** | The cycle iterates every monitoring-enabled source with no ceiling; adding sources linearly increases per-cycle request volume. | Cap sources per cycle, or stagger them across cycles. |
| **No join-rate limiting** | `invites/join` and `request-access` are unauthenticated and uncapped. Rapid joining is one of the most reliable triggers of Telegram restrictions. | Enforce a minimum interval and a daily join budget. |
| **No repeated-search guard** | `SEARCH_GLOBAL` audit rows are written but never consulted. Identical searches, spaced out in time, are re-issued freely. | Add short-lived result caching and a per-query cooldown. |
| **No circuit breaker on repeated FloodWait** | Successive floods produce no escalating restraint. | Trip a breaker after N floods in a window and pause outbound activity. |
| **No account-health monitoring or alerting** | Nothing watches for a rising FloodWait rate or a spike in `ERROR` sources. | Export metrics; alert on `session_invalid`, FloodWait frequency and collection failure rate. |
| **No multi-account support** | Single account, no rotation, no isolation of risky operations. | If throughput must grow, isolate high-risk operations onto a separate account rather than raising the rate on one. |
| **No `/start` cooldown** | Repeated calls send repeated `/start` messages to the same bot. | De-duplicate or rate-limit per bot source. |

## 10.3 Operational risk by activity

| Activity | Frequency in code | Account risk | Notes |
| --- | --- | --- | --- |
| Stored-message reads (`/api/messages`, `/api/sources/{id}/messages`) | Unlimited | **None** | No Telegram traffic at all. Prefer these. |
| Scheduled collection | Every 5 min per monitoring source | **Low–Medium** | Grows linearly with source count; incremental (`min_id`) so batches stay small once caught up. |
| Access check / registration | On demand | **Low** | 1–2 RPCs each. |
| Per-channel live search | On demand | **Medium** | 2 RPCs for a registered source, 3+ for an arbitrary channel (fresh probe every call). |
| Global search | On demand | **Medium** | Quota-tracked by Telegram; `checkSearchPostsFlood` is itself an RPC. |
| Multi-source search | On demand | **Medium–High** | Sequential search across many sources in one request. |
| `extract-links` | On demand | **Medium–High** | Up to 20 live probes per call, outside the concurrency cap. |
| Historical backfill | On demand | **High** | Up to 10,000 messages of bulk history reading — the classic FloodWait trigger. |
| Joining channels | On demand | **High** | The most common cause of Telegram account restrictions. |
| Sending `/start` | On demand | **Medium** | Visible outbound messaging activity. |

## 10.4 Evidence from the running deployment

Observed in the live SQLite database at the time of analysis:

* **12,805** messages collected across **4** sources; **12,804** by monitoring and **1** by a saved search result.
* **71** completed collection cycles, plus **1** row still marked `RUNNING` — evidence of an unclean shutdown (AP-18).
* **288** audit rows. Only **1** failure recorded (a `SEARCH_CHANNEL`); **no** FloodWait-driven collection failures.
* **0** access requests, **0** backfill jobs, **0** notifications — those paths are implemented and unit-tested but have **never been exercised in this deployment**.
* **5** collection checkpoints, all with `error_count = 0`.

The account has therefore been operating well inside Telegram's limits so far. That reflects modest usage, not the presence of a rate-limiting safety net.

<!--PAGEBREAK-->

# 11. Database Interaction

## 11.1 Schema overview — 11 tables

| Table | Purpose | Key columns |
| --- | --- | --- |
| `telegram_accounts` | One row describing the service's Telegram session. | `session_path`, `is_authenticated`, `connected_at`, `last_seen_at`. **`telegram_user_id` / `username` / `phone_masked` are never written (AP-14).** |
| `telegram_sources` | The central entity: every registered channel/group/bot/user. | `identifier` (normalized), `telegram_entity_id`, `source_type`, `access_status`, `monitoring_enabled`, `last_message_id`, `last_probe_*`, `discovered_from_source_id`, `discovery_type` |
| `telegram_access_requests` | One row per submitted join request. | `source_id`, `status`, `requested_at`, `next_check_at`, `attempt_count`, `approved_at`, `rejected_at` |
| `telegram_messages` | Collected messages. **`UNIQUE(source_id, telegram_message_id)`** is the real dedup guarantee. | `telegram_message_id`, sender fields, `message_date`, `text`, `views`/`forwards`/`reply_count`, `media_type`, `raw_data_hash`, `raw_data_ref`, `collection_method` |
| `telegram_media` | Media metadata (always) and download state (optional). | `message_id`, `media_type`, `filename`, `mime_type`, `file_size`, `local_path`, `sha256`, `downloaded` |
| `telegram_entities` | Resolution cache. **Never proof of access.** | `telegram_id` (unique), `entity_type`, `username`, `title` |
| `collection_checkpoints` | Forward monitoring anchor, one per source. | `source_id` (unique), `last_message_id`, `last_success_at`, `last_error_at`, `error_count` |
| `backfill_jobs` | Historical collection jobs with their **own** backward checkpoint. | `source_id`, `status`, `from_date`/`to_date`, `requested_limit`, `messages_*`, `checkpoint_message_id` |
| `notifications` | Persisted state-change events. | `event_type`, `source_id`, `previous_status`, `new_status`, `payload` (JSON), `is_read`, `processed` |
| `audit_logs` | Append-only trail of every significant operation. | `event_type`, `source_id`, `actor`, `details` (JSON), `success`, `timestamp` |
| `scheduler_jobs` | Background job run history (observability, **not** a lock). | `job_name`, `started_at`, `finished_at`, `status`, `details` (JSON) |

## 11.2 Entity relationships

```text
                        telegram_accounts        (standalone; one row, session state only)

                        telegram_entities        (standalone resolution cache, keyed by telegram_id)

                          telegram_sources
                                 |
   +--------------+--------------+---------------+----------------+---------------+
   |              |              |               |                |               |
   v              v              v               v                v               v
telegram_     telegram_    collection_      backfill_jobs    notifications     audit_logs
access_       messages     checkpoints       (CASCADE)       (SET NULL*)       (SET NULL*)
requests      (CASCADE)     (CASCADE,
(CASCADE)         |          1:1)
                  v
            telegram_media
              (CASCADE)

  telegram_sources.discovered_from_source_id --> telegram_sources.id   (self-reference,
                                                                        SET NULL*, provenance)

  * ON DELETE SET NULL is declared but does NOT fire: SQLite foreign keys are
    not enabled (SEC-09). Only AuditService.detach_source clears references,
    and only during duplicate-source rejection.
```

## 11.3 Write paths

| Operation | Tables written | Transaction behaviour |
| --- | --- | --- |
| Register source | `telegram_sources`, `audit_logs`, `telegram_entities` | Commit after insert, then again after the access check. |
| Check access | `telegram_sources`, `telegram_entities`, `audit_logs` | Single commit at the end. |
| Request access | `telegram_access_requests`, `telegram_sources`, `audit_logs`, `notifications` | Single commit at the end. |
| Monitoring cycle | `telegram_messages`, `telegram_media`, `collection_checkpoints`, `telegram_sources`, `audit_logs`, `notifications` | **One commit per message**, then a final commit for source/audit updates. |
| Backfill batch | `telegram_messages`, `telegram_media`, `backfill_jobs`, `audit_logs` | One commit per message, advancing the job's own checkpoint. |
| Save search result | `telegram_sources`, `telegram_messages`, `telegram_media`, `audit_logs` | Commit per persistence step. |
| Live search | `audit_logs` only | One commit. **No message rows are ever written by a search.** |
| Bot start | `telegram_sources` (discovered), `audit_logs` | Commits between phases. |
| Scheduler jobs | `scheduler_jobs`, plus each job's own tables | Commit at start and finish of each run. |
| **PATCH source / mark notification read** | **nothing** | **Flush without commit — the change is rolled back (ISS-01).** |

## 11.4 Data integrity mechanisms

* **De-duplication:** `UNIQUE(source_id, telegram_message_id)` with `INSERT ... ON CONFLICT DO NOTHING`. The insert's `rowcount` is what decides inserted-vs-skipped — never an application pre-check, so it stays correct under concurrency.
* **Source de-duplication:** two passes — the normalized identifier, then the resolved `telegram_entity_id` (which catches `@name` versus numeric id for the same channel).
* **Checkpoint correctness:** advanced only after a message is durably committed, and only ever forward.
* **Checkpoint separation:** backfill has its own `checkpoint_message_id` and never touches `collection_checkpoints`.
* **Evidence integrity:** every persisted message stores a SHA-256 of the raw Telegram payload plus a path to the preserved JSON; downloaded media is SHA-256 hashed too.
* **State-machine gating:** every `access_status` write funnels through `_apply_status`, which validates the transition and enforces the monitoring invariant.

## 11.5 Migrations

`init_db()` runs `Base.metadata.create_all` (creates missing tables only) and then `_apply_additive_column_patches`, a hard-coded list of `ALTER TABLE ... ADD COLUMN` statements applied **only on SQLite** for columns added after a table may already exist. There is no Alembic setup, no version table, and no support for column changes, drops or renames.

<div class="callout warn">
The additive patcher covers exactly six columns: <code>telegram_messages.collection_method</code> and <code>telegram_sources.last_probe_status / last_probe_reason / last_probe_at / discovered_from_source_id / discovery_type</code>. Any future column must be added to that list by hand, and on PostgreSQL the patcher is a no-op — an existing Postgres database would silently miss every one of them.
</div>

<!--PAGEBREAK-->

# 12. Configuration

All configuration is environment-driven through `pydantic-settings`, loaded from `telegram_service/.env`. No secret has a hard-coded default. Values are cached process-wide with `lru_cache`, so **changing `.env` requires a restart**.

## 12.1 Environment variables

| Variable | Required | Purpose | Example / Default |
| --- | --- | --- | --- |
| `TELEGRAM_API_ID` | **Yes**, for any Telegram operation | MTProto application id from `my.telegram.org/apps`. | `<TELEGRAM_API_ID>` |
| `TELEGRAM_API_HASH` | **Yes**, for any Telegram operation | MTProto application hash. **Secret.** | `<TELEGRAM_API_HASH>` |
| `TELEGRAM_SESSION_PATH` | No | Where Telethon persists the authorized session. **Treat as a credential.** | `./data/telegram_service.session` |
| `TELEGRAM_PHONE` | No | Convenience only; the login flow takes the phone in the request body. Never logged (a `mask_phone()` helper exists). | `<TELEGRAM_PHONE>` |
| `DATABASE_URL` | No | SQLAlchemy async URL. | `sqlite+aiosqlite:///./data/telegram_service.db` |
| `MEDIA_DOWNLOAD_ENABLED` | No | Master switch for downloading media bytes. Metadata is stored regardless. | `false` |
| `MEDIA_STORAGE_PATH` | No | Directory for downloaded media. | `./data/media` |
| `MAXIMUM_MEDIA_SIZE_BYTES` | No | Per-file download ceiling; larger files are skipped, metadata kept. | `20971520` (20 MB) |
| `ALLOWED_MEDIA_TYPES` | No | Comma-separated download allow-list. | `photo,document,video,audio,voice` |
| `RAW_EVIDENCE_STORAGE_PATH` | No | Root for per-message raw JSON evidence. | `./data/raw_messages` |
| `COLLECTION_BATCH_LIMIT` | No | Max messages fetched per source per collection cycle. | `200` |
| `BOT_START_TIMEOUT_SECONDS` | No | How long to wait for a bot's `/start` reply. | `15` |
| `BOT_START_POLL_INTERVAL_SECONDS` | No | Poll interval while waiting for that reply. | `1` |
| `ACCESS_RECONCILIATION_INTERVAL_HOURS` | No | Pending join-request reconciliation cadence. | `12` |
| `COLLECTION_INTERVAL_MINUTES` | No | Monitoring collection cadence. | `5` |
| `CONNECTION_HEALTH_INTERVAL_MINUTES` | No | Telegram connection health-check cadence. | `5` |
| `NOTIFICATION_PROCESSING_INTERVAL_MINUTES` | No | Notification dispatch cadence. | `1` |
| `LOG_LEVEL` | No | Root log level. | `INFO` |
| `API_HOST` | No | Uvicorn bind address. **`0.0.0.0` exposes an unauthenticated API on every interface** — see SEC-01. | `0.0.0.0` |
| `API_PORT` | No | Uvicorn port. | `8000` |

<div class="callout info">
There is <strong>no</strong> configuration for API authentication, TLS, CORS, rate limiting, proxies, or a second Telegram account — those features do not exist in the code, so no setting can enable them.
</div>

## 12.2 Example `.env` (placeholders only)

```ini
# --- Telegram MTProto credentials (https://my.telegram.org/apps) ---
TELEGRAM_API_ID=<TELEGRAM_API_ID>
TELEGRAM_API_HASH=<TELEGRAM_API_HASH>
TELEGRAM_SESSION_PATH=./data/telegram_service.session
TELEGRAM_PHONE=<TELEGRAM_PHONE>

# --- Database ---
DATABASE_URL=sqlite+aiosqlite:///./data/telegram_service.db

# --- Collection / media policy ---
MEDIA_DOWNLOAD_ENABLED=false
MEDIA_STORAGE_PATH=./data/media
MAXIMUM_MEDIA_SIZE_BYTES=20971520
ALLOWED_MEDIA_TYPES=photo,document,video,audio,voice
RAW_EVIDENCE_STORAGE_PATH=./data/raw_messages
COLLECTION_BATCH_LIMIT=200

# --- Bot-start workflow ---
BOT_START_TIMEOUT_SECONDS=15
BOT_START_POLL_INTERVAL_SECONDS=1

# --- Scheduler intervals ---
ACCESS_RECONCILIATION_INTERVAL_HOURS=12
COLLECTION_INTERVAL_MINUTES=5
CONNECTION_HEALTH_INTERVAL_MINUTES=5
NOTIFICATION_PROCESSING_INTERVAL_MINUTES=1

# --- App ---
LOG_LEVEL=INFO
API_HOST=127.0.0.1          # recommended: do NOT expose an unauthenticated API on 0.0.0.0
API_PORT=8000
```

<div class="callout crit">
Never commit a real <code>.env</code>, session file, or database. <code>.gitignore</code> already excludes <code>.env</code>, <code>*.session*</code>, <code>data/</code> and <code>poc/output/*.json</code> — keep it that way. This document deliberately contains <strong>no</strong> real credential values.
</div>
<!--PAGEBREAK-->

# 13. How to Run the API

## 13.1 Prerequisites

| Requirement | Detail |
| --- | --- |
| **Python** | 3.11 or newer. The codebase uses PEP 604 unions (`str \| None`) at runtime in Pydantic models and `datetime.UTC`-style patterns; 3.11+ is the safe floor. |
| **Telegram developer credentials** | An `api_id` and `api_hash` from `https://my.telegram.org/apps`, created with the Telegram account the service will operate as. |
| **A Telegram account** | A real account with a phone that can receive the login code. The service inherits exactly this account's permissions. |
| **Database** | None to install — SQLite is created automatically at `data/telegram_service.db`. |
| **Network** | Outbound access to Telegram's MTProto endpoints. |
| **Dependencies** | `fastapi`, `uvicorn[standard]`, `telethon>=1.36.0`, `sqlalchemy>=2.0.30`, `aiosqlite`, `pydantic>=2.7`, `pydantic-settings`, `apscheduler`, `python-dotenv`, `python-multipart`, plus `pytest`, `pytest-asyncio`, `httpx` for tests. |

## 13.2 Installation

```bash
cd telegram_service
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 13.3 Configuration

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Then edit `.env` and set at minimum:

```ini
TELEGRAM_API_ID=<TELEGRAM_API_ID>
TELEGRAM_API_HASH=<TELEGRAM_API_HASH>
API_HOST=127.0.0.1        # strongly recommended - the API has no authentication
```

## 13.4 Starting the service

```bash
uvicorn app.main:app --reload            # development
uvicorn app.main:app --host 127.0.0.1 --port 8000    # explicit bind
```

Startup performs, in order: create missing tables and apply additive column patches, attempt a Telegram connection (a failure is a warning, not fatal), start all four scheduler jobs. **There is no separate scheduler process to launch.**

Expected startup log lines:

```text
[2026-09-06 10:00:00,000] INFO telegram_service.main: Started server process
[2026-09-06 10:00:00,120] WARNING telegram_service.main: startup: could not connect to Telegram yet (...) - will retry via health-check job
INFO:     Uvicorn running on http://127.0.0.1:8000
```

## 13.5 Verifying it is up

```bash
curl http://localhost:8000/health     # {"status":"ok"}
curl http://localhost:8000/ready      # {"status":"ready","telegram_configured":true}
curl http://localhost:8000/api/telegram/status
```

## 13.6 API documentation

FastAPI's built-in docs are enabled (they are never disabled in `app/main.py`):

```text
http://localhost:8000/docs         Swagger UI  - interactive, executes real calls
http://localhost:8000/redoc        ReDoc       - reference view
http://localhost:8000/openapi.json OpenAPI 3.x - the machine-readable contract
```

<div class="callout crit">
Swagger UI issues <strong>real</strong> requests against the live Telegram account. "Try it out" on <code>/api/telegram/invites/join</code> genuinely joins a channel; on <code>/api/telegram/bots/{id}/start</code> it genuinely sends a message; on <code>DELETE /api/sources/{id}</code> it genuinely deletes collected data. Since nothing is authenticated, treat <code>/docs</code> as a privileged console.
</div>

## 13.7 Running the tests

```bash
pytest -v            # service suite (tests/), no network and no on-disk DB required
cd poc && python -m pytest tests    # the archived proof-of-concept's own suite, run separately
```

The suite uses an in-memory SQLite database and a fake Telethon-shaped client. `tests/test_endpoint_coverage.py` asserts that the set of endpoints exercised equals the set the app publishes, so a new route cannot land untested.

<div class="callout info">
<strong>Documentation drift (DOC-02):</strong> the README states "68 tests". The suite currently defines <strong>189</strong> test functions across 26 files. The count in the README is stale; the tests themselves are not.
</div>

<!--PAGEBREAK-->

# 14. How to Use the API

A complete walkthrough, in the order a new developer should follow it. Every request below matches the real schemas.

```text
Step 1 -> Start the API and confirm health
Step 2 -> Authenticate the Telegram account (once)
Step 3 -> Discover or register a source
Step 4 -> Verify the source's real access status
Step 5 -> (private sources only) Request access and wait for approval
Step 6 -> Start monitoring
Step 7 -> Search live, or read what has been collected
Step 8 -> (optional) Backfill history
Step 9 -> Monitor status and notifications
```

### Step 1 — Start the API and confirm health

**Endpoint:** `GET /health`, `GET /ready` · **Input:** none

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

```json
{ "status": "ok" }
```

```json
{ "status": "ready", "telegram_configured": true }
```

### Step 2 — Authenticate the Telegram account

**Endpoints:** `GET /api/telegram/status`, then the three `auth/*` steps · **Input:** phone, code, and 2FA password if enabled

```bash
curl http://localhost:8000/api/telegram/status
# -> {"connected":true,"authenticated":false,...}

curl -X POST http://localhost:8000/api/telegram/auth/send-code \
  -H "Content-Type: application/json" -d '{"phone":"+15551234567"}'
# -> {"ok":true,"requires_password":false,"error":null}

curl -X POST http://localhost:8000/api/telegram/auth/verify-code \
  -H "Content-Type: application/json" -d '{"phone":"+15551234567","code":"12345"}'
# -> {"ok":true,...}   or  {"ok":false,"requires_password":true,"error":"2FA password required"}

curl -X POST http://localhost:8000/api/telegram/auth/verify-password \
  -H "Content-Type: application/json" -d '{"password":"<SECRET>"}'
```

Confirm:

```bash
curl http://localhost:8000/api/telegram/status
# -> {"connected":true,"authenticated":true,"account":{"id":"...","username":"...","phone":"+1***67"},...}
```

*Do this once. The session file is reused on every restart.*

### Step 3 — Discover or register a source

**Option A — you already know the channel.** `POST /api/sources`

```bash
curl -X POST http://localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"identifier":"@examplechannel","monitoring_enabled":true}'
```

```json
{ "id": 1, "identifier": "@examplechannel", "source_type": "channel",
  "access_status": "PUBLIC_ACCESSIBLE", "monitoring_enabled": true, "...": "..." }
```

**Option B — find one first.** `GET /api/telegram/channels/search`, then register by id.

```bash
curl -G "http://localhost:8000/api/telegram/channels/search" --data-urlencode "q=example"
curl -X POST "http://localhost:8000/api/telegram/channels/1234567890/register?monitoring_enabled=false"
```

### Step 4 — Verify the real access status

**Endpoint:** `GET /api/sources/1/access-status` (or `POST .../check-access` to re-probe)

```bash
curl http://localhost:8000/api/sources/1/access-status
```

```json
{ "source_id": 1, "access_status": "PUBLIC_ACCESSIBLE", "status_reason": null,
  "last_probe_status": "PUBLIC_ACCESSIBLE", "last_probe_at": "2026-09-06T09:15:02Z", "...": "..." }
```

Decision point:

| `access_status` | Next step |
| --- | --- |
| `PUBLIC_ACCESSIBLE`, `ACCESSIBLE`, `JOINED` | Go to Step 6 (start monitoring). |
| `JOIN_REQUEST_REQUIRED` | Go to Step 5. |
| `ACCESS_DENIED`, `NOT_FOUND` | Stop — this account cannot reach it. |
| `ERROR` | Re-run `POST .../check-access`; if it persists, check `GET /api/telegram/status`. |

### Step 5 — Request access (private sources only)

**Endpoint:** `POST /api/sources/2/request-access` · **Input:** none

```bash
curl -X POST http://localhost:8000/api/sources/2/request-access
```

```json
{ "id": 1, "source_id": 2, "status": "PENDING",
  "requested_at": "2026-09-06T09:20:00Z", "next_check_at": "2026-09-06T21:20:00Z", "attempt_count": 1 }
```

Then wait. The 12-hour reconciliation job detects approval, moves the source to `JOINED`, writes a `TELEGRAM_ACCESS_GRANTED` notification, and **starts monitoring automatically**. Calling this endpoint again while `PENDING` is safe — it returns the existing request and sends nothing to Telegram.

### Step 6 — Start monitoring

**Endpoint:** `POST /api/sources/1/monitoring/start` · **Input:** none

```bash
curl -X POST http://localhost:8000/api/sources/1/monitoring/start
```

```json
{ "source_id": 1, "monitoring_enabled": true, "access_status": "MONITORING",
  "monitoring_started_at": "2026-09-06T09:30:00Z",
  "last_collected_at": "2026-09-06T09:30:04Z", "last_message_id": 72697 }
```

Remember: registering with `monitoring_enabled: true` is **not** enough — this call is what actually starts collection. It also runs the first cycle synchronously, so allow a generous timeout.

### Step 7 — Search live, or read collected data

**Live, against Telegram:**

```bash
curl -G "http://localhost:8000/api/sources/1/search" --data-urlencode "q=invoice"
curl -G "http://localhost:8000/api/telegram/search/global" --data-urlencode "q=invoice" --data-urlencode "limit=20"
```

**Local, against collected data (free, no Telegram traffic):**

```bash
curl "http://localhost:8000/api/messages?keyword=invoice&source_id=1&limit=20"
curl "http://localhost:8000/api/sources/1/messages?media_type=photo&limit=20"
```

**Promote one live result into stored data:**

```bash
curl -X POST http://localhost:8000/api/telegram/search/results/save \
  -H "Content-Type: application/json" \
  -d '{"identifier":"@examplechannel","telegram_message_id":72697,"collection_method":"global_search"}'
```

### Step 8 — Backfill history (optional)

```bash
curl -X POST http://localhost:8000/api/sources/1/backfill \
  -H "Content-Type: application/json" -d '{"limit":500}'
# -> 202 {"id":1,"status":"PENDING",...}

curl http://localhost:8000/api/sources/1/backfill/1
# -> {"status":"RUNNING","messages_inserted":120,...}
```

### Step 9 — Monitor status and notifications

```bash
curl http://localhost:8000/api/sources/1/monitoring-status
curl "http://localhost:8000/api/notifications?unread_only=true"
curl http://localhost:8000/api/telegram/status
```

### Complete Python client

```python
"""End-to-end walkthrough against a running telegram_service."""
import time
import httpx

BASE = "http://localhost:8000"

with httpx.Client(base_url=BASE, timeout=120) as c:
    # 1 - health
    assert c.get("/health").json()["status"] == "ok"

    # 2 - the Telegram account must already be authorized
    status = c.get("/api/telegram/status").json()
    if not status["authenticated"]:
        raise SystemExit(f"authenticate first: {status['error']}")

    # 3 - register (409 simply means it already exists)
    r = c.post("/api/sources", json={"identifier": "@examplechannel", "monitoring_enabled": True})
    if r.status_code == 409:
        source = next(s for s in c.get("/api/sources").json()
                      if s["identifier"] == "@examplechannel")
    else:
        r.raise_for_status()
        source = r.json()
    sid = source["id"]

    # 4 - verify real access
    access = c.get(f"/api/sources/{sid}/access-status").json()
    print("access:", access["access_status"], access["status_reason"])

    # 5 - private source: request access once, then wait for reconciliation
    if access["access_status"] == "JOIN_REQUEST_REQUIRED":
        print("join request:", c.post(f"/api/sources/{sid}/request-access").json()["status"])
        raise SystemExit("approval pending - reconciliation runs every 12h")

    # 6 - start monitoring (runs the first collection synchronously)
    if access["access_status"] in ("PUBLIC_ACCESSIBLE", "ACCESSIBLE", "JOINED", "MONITORING"):
        m = c.post(f"/api/sources/{sid}/monitoring/start")
        m.raise_for_status()
        print("monitoring:", m.json())

    # 7 - live search, then the local copy
    live = c.get(f"/api/sources/{sid}/search", params={"q": "invoice", "limit": 10})
    if live.status_code == 200:
        print("live hits:", len(live.json()["results"]))
    else:
        print("live search refused:", live.json())

    stored = c.get("/api/messages", params={"keyword": "invoice", "source_id": sid, "limit": 10})
    print("stored hits:", len(stored.json()))

    # 8 - backfill and poll
    job = c.post(f"/api/sources/{sid}/backfill", json={"limit": 200}).json()
    while True:
        j = c.get(f"/api/sources/{sid}/backfill/{job['id']}").json()
        print("backfill:", j["status"], j["messages_inserted"])
        if j["status"] in ("COMPLETED", "PARTIAL", "FAILED"):
            break
        time.sleep(5)
```

<!--PAGEBREAK-->

# 15. Postman / cURL Examples

## 15.1 Postman setup

1. **Create an environment** with one variable: `base_url` = `http://localhost:8000`.
2. **Authentication:** set Auth type to **No Auth** on every request — the API has none.
3. **Headers:** add `Content-Type: application/json` only on requests that send a body (`POST`/`PATCH`).
4. **Import the contract:** `File > Import > Link` -> `http://localhost:8000/openapi.json` generates the whole collection automatically, which is preferable to hand-building it.

<div class="callout warn">
Postman's Collection Runner against these endpoints drives real Telegram traffic. Do not loop search or join requests — the account, not Postman, absorbs the consequences.
</div>

## 15.2 Request reference

**Register a source**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/sources` |
| Headers | `Content-Type: application/json` |
| Auth | None |
| Body (raw JSON) | `{"identifier": "@examplechannel", "monitoring_enabled": true}` |
| Expected | `201` with a `SourceOut`; `409` if already registered; `422` for a bad identifier |

```bash
curl -X POST http://localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"identifier":"@examplechannel","monitoring_enabled":true}'
```

**Check access**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/sources/1/check-access` |
| Body | none |
| Expected | `200` with the refreshed `SourceOut`; `404` if the source does not exist |

```bash
curl -X POST http://localhost:8000/api/sources/1/check-access
```

**Start monitoring**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/sources/1/monitoring/start` |
| Body | none |
| Expected | `200`; `409` if access is not verified — allow a long timeout, the first cycle runs inline |

```bash
curl -X POST http://localhost:8000/api/sources/1/monitoring/start
```

**Live global search**

| | |
| --- | --- |
| Method | `GET` |
| URL | `{{base_url}}/api/telegram/search/global` |
| Params | `q=invoice`, `limit=20`, `sort=date_desc` |
| Expected | `200`; `429` with `retry_after_seconds` on FloodWait; `401` if the session is invalid |

```bash
curl -G "http://localhost:8000/api/telegram/search/global" \
  --data-urlencode "q=invoice" --data-urlencode "limit=20"
```

**Multi-source search**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/telegram/search/sources` |
| Headers | `Content-Type: application/json` |
| Body | `{"q": "invoice", "source_ids": [1, 3], "limit": 25}` |
| Expected | `200`; inaccessible sources appear in `skipped`, never as an error |

```bash
curl -X POST http://localhost:8000/api/telegram/search/sources \
  -H "Content-Type: application/json" \
  -d '{"q":"invoice","source_ids":[1,3],"limit":25}'
```

**Save a search result**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/telegram/search/results/save` |
| Body | `{"identifier": "@examplechannel", "telegram_message_id": 72697}` |
| Expected | `201` with `inserted: true` (or `false` when already stored) |

```bash
curl -X POST http://localhost:8000/api/telegram/search/results/save \
  -H "Content-Type: application/json" \
  -d '{"identifier":"@examplechannel","telegram_message_id":72697}'
```

**Search stored messages (no Telegram traffic)**

| | |
| --- | --- |
| Method | `GET` |
| URL | `{{base_url}}/api/messages` |
| Params | `keyword=invoice`, `source_id=1`, `media_type=photo`, `limit=20` |
| Expected | `200` with a `MessageOut` array |

```bash
curl "http://localhost:8000/api/messages?keyword=invoice&source_id=1&limit=20"
```

**Join by invite link**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/telegram/invites/join` |
| Body | `{"identifier": "https://t.me/+AbCdEfGhIjKlM"}` |
| Expected | `200` with `status` ∈ `joined` / `already_member` / `pending_approval` / `already_pending` / `failed` |

```bash
curl -X POST http://localhost:8000/api/telegram/invites/join \
  -H "Content-Type: application/json" \
  -d '{"identifier":"https://t.me/+AbCdEfGhIjKlM"}'
```

**Start a bot**

| | |
| --- | --- |
| Method | `POST` |
| URL | `{{base_url}}/api/telegram/bots/5/start` |
| Body | none |
| Expected | `200` with `status` ∈ `started` / `timed_out` / `failed`; `422` if the source is not a bot |

```bash
curl -X POST http://localhost:8000/api/telegram/bots/5/start
```

**Queue and poll a backfill**

```bash
curl -X POST http://localhost:8000/api/sources/1/backfill \
  -H "Content-Type: application/json" -d '{"limit":500}'
curl http://localhost:8000/api/sources/1/backfill/1
```

## 15.3 Handling errors in a client

```bash
# Show the status code alongside the body
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/sources/9999
# 404

curl -s -G "http://localhost:8000/api/sources/2/search" --data-urlencode "q=x" | python -m json.tool
# {"error": {"code": "JOIN_PENDING", "message": "...", "retryable": false}}
```

```python
import httpx

def call(client, method, url, **kw):
    r = client.request(method, url, **kw)
    if r.status_code == 429:                      # FloodWait
        body = r.json()["error"]
        raise RuntimeError(f"rate limited, wait {body['retry_after_seconds']}s")
    if r.status_code == 401:                      # session invalid
        raise RuntimeError("re-authenticate the Telegram account")
    if r.status_code in (403, 404, 409, 422):
        payload = r.json()
        detail = payload.get("detail") or payload.get("error", {}).get("message")
        raise RuntimeError(f"{r.status_code}: {detail}")
    r.raise_for_status()
    return r.json()
```

<!--PAGEBREAK-->

# 16. Error Handling

## 16.1 The two error envelopes

The service returns **two different error shapes**. Clients must handle both.

**(a) FastAPI `HTTPException`** — used by routes for domain errors:

```json
{ "detail": "source already registered as id=1" }
```

**(b) `TelegramSearchError`** — Telegram-facing failures on the search/discovery/save paths, rendered by the global handler in `app/main.py`:

```json
{ "error": { "code": "FLOOD_WAIT", "message": "Telegram is rate-limiting this account - wait 42s before retrying.", "retryable": true, "retry_after_seconds": 42 } }
```

**(c) Pydantic validation** — FastAPI's standard 422 body:

```json
{ "detail": [ { "type": "missing", "loc": ["body", "identifier"], "msg": "Field required", "input": {} } ] }
```

`retry_after_seconds` appears **only** on `FLOOD_WAIT`.

## 16.2 Centralized error reference

| Error | HTTP Status | Cause | Recommended Action |
| --- | --- | ---: | --- |
| Pydantic validation failure | 422 | Missing/invalid body or query field. | Fix the request; inspect `detail[].loc`. |
| `q` empty or > 256 chars | 422 | Route or discovery-layer guard. | Shorten or supply the query. |
| `limit` out of range | 422 | `Query(ge=1, le=100)` on search endpoints. | Use 1–100. |
| `sort` not `date_desc`/`relevance` | 422 | Route pattern constraint. | Use a supported value. |
| `INVALID_REQUEST` / invalid identifier | 422 | `normalize_identifier` rejected the string. | Use `@username`, a `t.me` link, or a numeric id. |
| `"...is not a Telegram invite link"` | 422 | A username was sent to `invites/join`. | Use `POST /api/sources` + `request-access`. |
| `USERNAME_INVALID` | 422 | Telegram: `UsernameInvalidError`. | Correct the username. |
| `NOT_A_CHANNEL` | 422 | The entity is a bot; bots have no searchable message source. | Use the bot-start workflow. |
| `INVITE_HASH_INVALID` | 422 | Telegram: `InviteHashInvalidError`. | Obtain a valid invite. |
| Source is not a bot | 422 | `SourceIsNotABotError`. | Only call bot-start on `source_type == "bot"`. |
| `"source not found"` / `"message not found"` | 404 | No such row. | Check the id. |
| `NOT_FOUND` (Telegram) | 404 | `UsernameNotOccupiedError` / `ChannelInvalidError`, or a saved message that no longer exists. | The entity or message is gone. |
| Backfill job not found | 404 | Wrong `job_id`/`source_id` pair. | Use the ids from the 202 response. |
| Duplicate source | 409 | Same identifier, or same resolved `telegram_entity_id`. | Use the existing source id from the message. |
| Invalid access-request state | 409 | Source is not `JOIN_REQUEST_REQUIRED`/`ERROR`. | No join is needed, or resolve the current state first. |
| `MonitoringStateError` | 409 | Access not verified, or the Telegram session is unusable. | Run `check-access`; re-authenticate if needed. |
| `BackfillNotAllowedError` | 409 | Source access not positively verified. | Verify access first. |
| `NOT_FOUND` on save | 409 | Source exists but could not be located after a duplicate error. | Re-register or locate the source manually. |
| `NOT_AUTHENTICATED` | 401 | `verify_authorized()` failed before any RPC. | Complete the auth flow. |
| `TELEGRAM_SESSION_INVALID` | 401 | Auth key unregistered/revoked, session expired, account deactivated or banned. | **Re-authenticate.** Nothing else will work until then. |
| `AUTHENTICATION_ERROR` | 401 | `AuthKeyError` / `SessionPasswordNeededError` outside the login flow. | Re-authenticate. |
| `PREMIUM_OR_QUOTA_REQUIRED` | 402 | `PremiumAccountRequiredError` — Premium or remaining quota needed. | Rely on the automatic fallback, or accept reduced coverage. |
| `CHANNEL_PRIVATE` | 403 | `ChannelPrivateError` — private or inaccessible. | Register and request access. |
| `CHAT_ADMIN_REQUIRED` | 403 | Admin privileges required. | Not achievable with this account. |
| `USER_NOT_PARTICIPANT` | 403 | Not a member of the chat. | Join first. |
| `ACCESS_DENIED` | 403 | Stored status is `ACCESS_DENIED`. | Unreachable for this account. |
| `JOIN_REQUIRED` | 403 | Stored status is `JOIN_REQUEST_REQUIRED`. | Call `request-access`. |
| `JOIN_PENDING` | 403 | A join request is awaiting approval. | Wait for reconciliation. |
| `ACCESS_REJECTED` | 403 | The join request was rejected. | No alternative is attempted, by policy. |
| `INVITE_HASH_EXPIRED` | 410 | The invite link expired. | Obtain a fresh invite. |
| `FLOOD_WAIT` | 429 | Telegram is rate-limiting the account. | **Wait `retry_after_seconds` before any further Telegram call.** Nothing enforces this for you. |
| `TELEGRAM_RPC_ERROR` | 502 | Any other Telethon `RPCError`. | Inspect logs; usually transient. |
| `NETWORK_ERROR` | 503 | `ConnectionError` / `OSError` / `asyncio.TimeoutError`. | Retry with backoff; the health-check job reconnects on its own cadence. |
| `UNKNOWN_ERROR` | 500 | Unmapped exception. | Report with the logged class name. |
| Unmapped `RuntimeError` from `request-access` | 500 | Telegram session unusable during a join (ISS-03). | Check `GET /api/telegram/status`; treat as 401. |

## 16.3 Errors that never surface as HTTP errors

Several failure modes are recorded as **state**, not raised:

| Situation | What the caller sees |
| --- | --- |
| `FloodWaitError` during a collection cycle | Cycle stops, partial batch persisted, `checkpoint.error_count` incremented. The API is unaffected — the failure is visible only in audit logs and the checkpoint. |
| `ChannelPrivateError` during collection ("access revoked") | Source moves to `ERROR`, monitoring disabled, a `TELEGRAM_COLLECTION_ERROR` notification is written. |
| Access-check failure | Recorded in `access_status` / `last_probe_status`; `POST /check-access` still returns **200**. |
| Bot `/start` timeout | `200` with `status: "timed_out"`. |
| Backfill failure | Job row becomes `FAILED`/`PARTIAL` with `error_message`; the original call already returned 202. |
| Blocked state transition | Logged as a warning, `status_reason` records it, `access_status` is left unchanged, HTTP 200. |
| Notification dispatch failure | Logged; other dispatchers still run; the row is still marked processed. |

<div class="callout warn">
Because of this design, <strong>a 200 response does not mean the Telegram operation succeeded.</strong> Always inspect <code>access_status</code>, <code>status</code>, or <code>error</code> in the body.
</div>

<!--PAGEBREAK-->

# 17. Current Implementation Status

Legend: ✅ Implemented · ⚠️ Partially Implemented · ❌ Not Implemented · 🔒 Restricted by Telegram

| Feature | Status | Endpoint | Notes |
| --- | --- | --- | --- |
| Telegram authentication (code + 2FA) | ✅ | `POST /api/telegram/auth/*` | Non-interactive, three steps; pending hash is in-memory only. |
| Session reuse across restarts | ✅ | — | Telethon session file at `TELEGRAM_SESSION_PATH`. |
| Connection/auth status reporting | ✅ | `GET /api/telegram/status` | Masked phone; never leaks session data. |
| Invalid-session detection and latch | ✅ | all Telegram endpoints | Stops repeated RPCs with a known-bad key. |
| Source registration + normalization | ✅ | `POST /api/sources` | `@username`, `t.me` URL, invite hash, numeric id. |
| Duplicate prevention (identifier + entity) | ✅ | `POST /api/sources` | Two-pass dedup; 409 on either. |
| Live access probing | ✅ | `POST /api/sources/{id}/check-access` | Real read probe, never inference. |
| Access state machine (13 states) | ✅ | — | Validated allow-list; monitoring invariant enforced centrally. |
| Public source access | ✅ | `POST /api/sources` | `PUBLIC_ACCESSIBLE`. |
| Private source access via join request | ⚠️ 🔒 | `POST /api/sources/{id}/request-access` | Submitted once; content only after Telegram approves. |
| Invite-link join | ✅ | `POST /api/telegram/invites/join` | Idempotent; never duplicates a request. |
| Join-request rejection detection | ⚠️ 🔒 | (background job) | Invite-hash: yes. Username-based: no decline signal exists. |
| 12-hour reconciliation | ✅ | (background job) | `max_instances=1`; stops early on session invalidation. |
| Event-driven access fast path | ❌ | — | `event_handler.py` is complete but **never wired in** (DOC-01). |
| Automatic monitoring after approval | ✅ | (background job) | Starts only after membership is verified. |
| Start/stop monitoring | ✅ | `POST /api/sources/{id}/monitoring/start` and `/stop` | Start runs the first cycle synchronously. |
| Incremental collection with checkpoints | ✅ | (background job, every 5 min) | Per-message commits; restart-safe. |
| DB-level message de-duplication | ✅ | — | `UNIQUE(source_id, telegram_message_id)` + `ON CONFLICT DO NOTHING`. |
| Raw evidence preservation + SHA-256 | ✅ | — | One JSON file per message under `data/raw_messages/`. |
| Media metadata capture | ✅ | — | Always recorded. |
| Media download | ⚠️ | — | Implemented but **off by default**; size/type policy applies. |
| Historical backfill | ⚠️ | `POST /api/sources/{id}/backfill` | Works; no crash recovery, no cancel, no per-source concurrency guard. |
| Live global search | ⚠️ 🔒 | `GET /api/telegram/search/global` | `searchPosts` is quota-gated; the fallback is account-scoped. |
| Live per-channel search | ✅ | `GET /api/sources/{id}/search` | Native sender/media/date filters. |
| Live arbitrary-channel search | ✅ | `GET /api/telegram/channels/{channel_id}/search` | Fresh probe on every call. |
| Multi-source search | ⚠️ | `POST /api/telegram/search/sources` | Works, but returns **no pagination cursor**. |
| Channel discovery | ⚠️ 🔒 | `GET /api/telegram/channels/search` | `contacts.search` offers no pagination; access status is a coarse hint. |
| Register a discovered channel | ✅ | `POST /api/telegram/channels/{telegram_id}/register` | Same path as `POST /api/sources`. |
| Telegram link extraction/classification | ✅ | `POST /api/telegram/discovery/extract-links` | Never fetches a URL; capped at 20 probes. |
| Save a search result | ✅ | `POST /api/telegram/search/results/save` | The only path search data reaches the DB. |
| Bot `/start` workflow | ✅ | `POST /api/telegram/bots/{source_id}/start` | One message; no callback execution; capped at 10 links. |
| Stored-message retrieval | ✅ | `GET /api/sources/{id}/messages`, `GET /api/messages/{id}` | No Telegram traffic. |
| Local message search | ⚠️ | `GET /api/messages` | Flat filters only — no boolean/nested queries, no ranking, no totals. |
| Notifications persisted | ✅ | `GET /api/notifications` | Written on access granted/rejected and collection errors. |
| Mark notification read | ❌ | `POST /api/notifications/{id}/read` | Returns 200 but **does not persist** (ISS-01). |
| Update a source (PATCH) | ❌ | `PATCH /api/sources/{id}` | Returns 200 but **does not persist** (ISS-01). |
| Notification dispatch (websocket/webhook/email) | ❌ | — | Registry exists; **no provider registered**. |
| Audit logging | ✅ | — | ~30 event types; 5 declared but never emitted. |
| Structured logging | ✅ | — | Single-line JSON; query text never logged. |
| Health/readiness probes | ✅ | `GET /health`, `GET /ready` | `/ready` reflects configuration only. |
| Swagger / OpenAPI docs | ✅ | `/docs`, `/redoc`, `/openapi.json` | Enabled and unauthenticated. |
| **API authentication / authorization** | ❌ | — | **None anywhere** (SEC-01). |
| API rate limiting | ❌ | — | Only a 3-way search concurrency cap. |
| FloodWait backoff / retry scheduling | ❌ | — | Reported, never enforced. |
| Multi-account support / rotation | ❌ | — | Single account by design. |
| Proxy support | ❌ | — | Not present. |
| Database migrations (Alembic) | ❌ | — | `create_all` + a SQLite-only additive patcher. |
| Message export (bulk CSV/JSON) | ❌ | — | Paged JSON API only. |
| NLP / OCR / entity extraction / correlation | ❌ | — | Explicit downstream scope. |
| Deployment tooling (Docker/CI) | ❌ | — | Not present. |
| Sending arbitrary messages | ❌ | — | Only the literal `/start` to a bot. |
| Reading reactions / polls / comment threads / stories | ❌ | — | Not requested from Telegram. |
| Participant/member enumeration | ❌ | — | No participants RPC anywhere. |
| Test suite | ✅ | — | 189 test functions in 26 files; a coverage test forbids untested endpoints. |
<!--PAGEBREAK-->

# 18. Known Issues and Recommendations

Three clearly separated categories: **defects in current behaviour**, **technical debt**, and **future improvements**. Nothing here is speculative — every item cites the code.

## 18.1 Current Issues (defects in behaviour today)

### ISS-01 — Two endpoints return 200 but discard the change · **High**

`SourceService.update_source` and `NotificationService.mark_read` both call a repository method that `flush()`es without `commit()`, and neither caller commits. The FastAPI `get_db` dependency closes the session when the request ends, rolling the transaction back. Both endpoints serialize the in-memory object, so the response *looks* successful.

* Affected: `PATCH /api/sources/{source_id}`, `POST /api/notifications/{notification_id}/read`.
* Symptom: `?unread_only=true` keeps returning the same notification forever; a renamed source reverts on the next read.
* **Verified** by reproducing the exact dependency + repository pattern against SQLAlchemy 2.0.52 / aiosqlite: the value read after the request was the original.
* Not caught by tests: the smoke test asserts only the status code, and no test re-reads after a PATCH.
* **Fix:** `await self.session.commit()` in each service method (one line each), plus a regression test that re-reads after mutating.

### ISS-02 — Backfill jobs do not survive a restart · **Medium**

`start_backfill` fires `asyncio.create_task(...)` in the API process. A restart kills the task; the row stays `RUNNING` forever. There is no startup sweep, no cancel endpoint, and no per-source concurrency guard (several jobs can run against one source at once).

* **Fix:** on startup, mark orphaned `RUNNING` jobs as `FAILED` (or resume them from `checkpoint_message_id`); add `DELETE /api/sources/{id}/backfill/{job_id}` to cancel; refuse a second `RUNNING` job for the same source.

### ISS-03 — `request-access` returns 500 when the session is unusable · **Low**

`SourceService.request_access` raises a bare `RuntimeError` when `verify_authorized()` fails, and `routes_sources` does not catch it — FastAPI turns it into a 500. Every comparable path returns 401 or 409.

* **Fix:** catch it in the route and return 409 (or a `TelegramSearchError` with 401) to match the rest of the API.

### ISS-04 — `PATCH` bypasses the access-status invariant · **Low**

`update_source` writes `monitoring_enabled` straight through the repository, skipping `_apply_status`, the choke point that guarantees monitoring is never enabled from a blocked state. Currently masked by ISS-01 (nothing persists), so fixing ISS-01 without fixing this would *introduce* the inconsistency.

* **Fix:** route `monitoring_enabled` changes through the monitoring service, or re-validate the invariant inside `update_source`.

### ISS-05 — Unbounded `limit` on stored-data endpoints · **Low**

`GET /api/messages`, `GET /api/sources/{id}/messages`, `GET /api/sources` and `GET /api/notifications` accept any integer `limit`. The live database already holds 12,805 messages, so `limit=1000000` is a self-inflicted denial of service.

* **Fix:** `Query(default=50, ge=1, le=200)` on all four.

### ISS-06 — Stale `RUNNING` scheduler rows · **Informational**

An unclean shutdown leaves `scheduler_jobs.status = "RUNNING"` permanently — one such row exists in the live database. Purely an observability wart; the no-overlap guarantee comes from APScheduler's `max_instances=1`, not this table.

* **Fix:** reconcile stale rows on startup.

### ISS-07 — Deleting a source orphans its evidence files · **Low**

`DELETE /api/sources/{id}` cascades in the database but leaves `data/raw_messages/<source_id>/` on disk, and (because SQLite foreign keys are off) leaves `audit_logs.source_id` / `notifications.source_id` pointing at a row that no longer exists.

* **Fix:** delete the evidence directory, and either enable `PRAGMA foreign_keys=ON` or call `detach_source` on every deletion path.

## 18.2 Documentation vs implementation discrepancies

The code is the source of truth; these are places where existing documents disagree with it.

### DOC-01 — README claims an event-driven access fast path that is not wired in · **Medium**

README §11 describes "Two complementary mechanisms", the first being event-driven detection via `app/telegram/event_handler.py`. That module is complete and unit-tested, but `register_access_change_handlers` is **never called** anywhere in the application — a grep across `app/` finds only docstring mentions. In production, approval detection latency is therefore **up to 12 hours**, not near-real-time.

* **Fix:** either wire it into the lifespan (register the handler after a successful connect, with a callback that triggers `check_access` for the affected source) or correct the README.

### DOC-02 — Test count is stale · **Informational**

README §17 and the closing summary both state "68 tests". The suite currently defines **189** test functions across 26 files.

### DOC-03 — "Swap `DATABASE_URL` to Postgres" is not quite true · **Low**

The README states that switching to PostgreSQL is a one-line change because nothing above the repository layer touches a specific dialect. `MessageRepository.upsert_ignore_duplicate` imports `sqlalchemy.dialects.sqlite.insert` and uses `on_conflict_do_nothing` — this is the single dedup guarantee in the system, and it would need a dialect-aware variant. The additive-column patcher is also SQLite-only and silently no-ops elsewhere.

### DOC-04 — The `poc/` technical report describes different software · **Informational**

`docs/technical-report.md` reviews the archived single-file proof-of-concept (`poc/main.py`), not this service. Its limits (100 results per call, 10 channels per run, no pagination, `--public-search` requiring Premium) **do not apply** to `telegram_service`, which paginates, has no channel-count cap, and falls back rather than requiring Premium. Keep the two clearly separated during review.

## 18.3 Technical debt

| # | Debt | Why it matters |
| --- | --- | --- |
| TD-1 | **No migration tool.** `create_all` plus a hand-maintained six-entry additive patch list, SQLite-only. | Any column rename/type change, or any non-SQLite deployment, is unmanaged. Adopt Alembic. |
| TD-2 | **Dependency floors with no lock file.** Every requirement is `>=`. | Non-reproducible builds; a breaking Telethon release can arrive silently. |
| TD-3 | **Dead / unreachable code.** `event_handler.py` (unused), the audit constants `TELEGRAM_AUTHENTICATED`, `MEDIA_DOWNLOADED`, `ERROR_OCCURRED`, `CHANNEL_REGISTERED_FROM_DISCOVERY`, the notification event `TELEGRAM_MONITORING_STARTED`, and the schemas `MessageSearchQuery`, `TelegramSearchErrorResponse`, `TelegramErrorDetail` (never referenced by a `response_model`, so absent from the OpenAPI schema). | Suggests capability that does not exist. Wire it up or remove it. |
| TD-4 | **`telegram_accounts` is nearly empty.** `telegram_user_id`, `username` and `phone_masked` are never written by any code path — confirmed `NULL` in the live database. | The table implies account tracking that does not happen. Populate it in the health-check job or drop the columns. |
| TD-5 | **`next_status_check_at` is never written.** | Always `null` in `SourceOut`/`AccessStatusOut`; implies a re-probe schedule that does not exist. |
| TD-6 | **`processing_status` never leaves `"raw"`.** | The `normalized`/`enriched` states anticipate a downstream pipeline that is out of scope. Acceptable, but should be labelled. |
| TD-7 | **`SourceCreate.source_type` accepted and ignored.** | Misleading contract; remove it or honour it as a hint. |
| TD-8 | **Duplicated media-filter maps.** `_MEDIA_FILTERS` is defined identically in `global_search.py` and `channel_search.py`. | Minor drift risk; extract to one module. |
| TD-9 | **`collection_method` is unvalidated free text** on `SaveSearchResultRequest`. | Any string reaches the database column; validate against the `CollectionMethod` enum. |
| TD-10 | **Multi-source search is sequential by necessity** (one shared `AsyncSession`). | Correct as written, but caps throughput. Would need per-source sessions to parallelize — and a rate budget before that would be safe. |
| TD-11 | **Two error envelope shapes** (`{"detail": ...}` vs `{"error": {...}}`). | Every client must handle both. Consider one envelope. |
| TD-12 | **No metrics endpoint.** Logs and `audit_logs` are the only observability. | No Prometheus/OpenTelemetry surface for FloodWait rate, collection lag, or error rate. |

## 18.4 Recommended improvements (future work — none of this exists today)

**Priority 1 — before any non-local deployment**

1. **Add API authentication and authorization** (SEC-01). An API-key dependency at router level is the minimum; treat auth/join/bot-start/delete as privileged.
2. **Bind to `127.0.0.1` by default** and change `.env.example` accordingly.
3. **Fix ISS-01** (two missing commits) and add regression tests that re-read after mutating.
4. **Terminate TLS** at a reverse proxy so login codes and the 2FA password are not sent in clear text.
5. **Bound every `limit`** (ISS-05).

**Priority 2 — account safety**

6. **Central FloodWait enforcement:** record the deadline and refuse outbound Telegram calls until it passes, instead of only reporting it.
7. **Global outbound request budget** in `rate_policy`, covering collection, access checks, joins and link extraction — not just search.
8. **Join-rate limiting** with a daily budget.
9. **Jitter and inter-source spacing** in the collection cycle.
10. **Account-health metrics and alerting** on `session_invalid`, FloodWait frequency and `ERROR` source count.

**Priority 3 — correctness and operations**

11. **Backfill lifecycle:** startup reconciliation of orphaned jobs, a cancel endpoint, and a per-source concurrency guard (ISS-02).
12. **Alembic migrations** (TD-1) and a dialect-agnostic upsert (DOC-03) before any Postgres move.
13. **Wire in or remove the event handler** (DOC-01); wiring it reduces approval latency from 12 hours to near-real-time.
14. **Enable SQLite foreign keys** or make every deletion path detach references (SEC-09, ISS-07).
15. **Pin dependencies / add a lock file** (TD-2).

**Priority 4 — functionality**

16. **Pagination for multi-source search** (AP-7).
17. **Richer local search:** boolean queries, a full-text index, result totals (AP-8).
18. **A notification dispatcher** (webhook is the smallest useful one) so state changes reach operators without polling.
19. **Retention and deletion policy** for collected content and raw evidence (SEC-05), including evidence cleanup on source deletion.
20. **An explicit policy decision on user/DM sources** (section 7.1) — either block `SourceType.USER`/`BOT` from monitoring, or document the behaviour deliberately.

<!--PAGEBREAK-->

# 19. API Sequence Diagrams

Only workflows that exist in the code are diagrammed.

## 19.1 Source registration with access verification

```text
Client          API Router        SourceService     access_manager     Telegram        Database
  |                 |                   |                 |               |               |
  | POST /api/sources                   |                 |               |               |
  |----------------> |                  |                 |               |               |
  |                 | register_source() |                 |               |               |
  |                 |-----------------> |                 |               |               |
  |                 |                   | normalize_identifier (no network)|              |
  |                 |                   | get_by_identifier                |              |
  |                 |                   |---------------------------------------------->  |
  |                 |                   |<--- none (else 409) --------------------------- |
  |                 |                   | INSERT source (DISCOVERED) + audit + COMMIT     |
  |                 |                   |---------------------------------------------->  |
  |                 |                   | check_access()  |               |               |
  |                 |                   |---------------->|               |               |
  |                 |                   |                 | verify_authorized()           |
  |                 |                   |                 | get_entity    |               |
  |                 |                   |                 |-------------->|               |
  |                 |                   |                 |<-- entity ----|               |
  |                 |                   |                 | getHistory(limit=1)           |
  |                 |                   |                 |-------------->|               |
  |                 |                   |                 |<-- ok / ChannelPrivateError --|
  |                 |                   |<-- AccessCheckResult ------------|               |
  |                 |                   | write last_probe_* ALWAYS                        |
  |                 |                   | _apply_status (state machine)                    |
  |                 |                   | upsert entity cache + audit + COMMIT             |
  |                 |                   |---------------------------------------------->  |
  |                 |                   | dedup by telegram_entity_id                      |
  |                 |                   |---------------------------------------------->  |
  |                 |<-- SourceOut ---- | (or delete row + 409)                            |
  |<-- 201 -------- |                   |                 |               |               |
```

## 19.2 Private source: join request and 12-hour reconciliation

```text
Client       API      SourceService   join_request_mgr   Telegram    Scheduler(12h)   Database
  |           |             |                |               |             |             |
  | POST /request-access    |                |               |             |             |
  |---------->|             |                |               |             |             |
  |           |-----------> | active PENDING request?                      |             |
  |           |             |-------------------------------------------------------->   |
  |           |             |<-- none (else return it, NO RPC) ----------------------    |
  |           |             | state == JOIN_REQUEST_REQUIRED? (else 409)   |             |
  |           |             | submit_join_request()          |             |             |
  |           |             |--------------->|               |             |             |
  |           |             |                | joinChannel / importChatInvite            |
  |           |             |                |-------------->|             |             |
  |           |             |                |<-- InviteRequestSentError --|             |
  |           |             |<-- PENDING ----|               |             |             |
  |           |             | INSERT access_request (next_check_at +12h) + COMMIT        |
  |           |             |-------------------------------------------------------->  |
  |<-- 200 (PENDING) -------|                |               |             |             |
  |           |             |                |               |             |             |
  |           |             |                |               | reconcile_pending_access  |
  |           |             |                |               |<------------|             |
  |           |             |                | check_pending_status()      |             |
  |           |             |                |<--------------------------- |             |
  |           |             |                | checkChatInvite / get_entity(left flag)   |
  |           |             |                |-------------->|             |             |
  |           |             |                |<-- ChatInviteAlready -------|             |
  |           |             |                | APPROVED      |             |             |
  |           |             |                |---------------------------->|             |
  |           |             |     JOIN_REQUEST_APPROVED -> JOINED, notification, COMMIT  |
  |           |             |                |               |             |---------->  |
  |           |             |     start_monitoring() AUTOMATICALLY (verified access)     |
  |           |             |                |               |             |---------->  |
  | GET /api/notifications  |                |               |             |             |
  |---------->|             |                |               |             |             |
  |<-- TELEGRAM_ACCESS_GRANTED ------------- |               |             |             |
```

## 19.3 Scheduled collection cycle

```text
Scheduler   MonitoringService   ClientManager   collector   Telegram   MessageService   Database
   |              |                   |             |          |             |             |
   | every COLLECTION_INTERVAL_MINUTES (max_instances=1)       |             |             |
   |------------->|                   |             |          |             |             |
   |              | list sources WHERE monitoring_enabled AND status = MONITORING          |
   |              |------------------------------------------------------------------->   |
   |              | FOR EACH source:  |             |          |             |             |
   |              | verify_authorized()             |          |             |             |
   |              |------------------>|             |          |             |             |
   |              |<-- ok / invalid --|             |          |             |             |
   |              |   invalid -> source = ERROR, monitoring off, next source                |
   |              | get_entity        |             |          |             |             |
   |              |-------------------------------->|--------->|             |             |
   |              | checkpoint.get_or_create()      |          |             |             |
   |              |------------------------------------------------------------------->   |
   |              | collect_new_messages(min_id, reverse=True, limit)                       |
   |              |------------------------------>  |--------->|             |             |
   |              |                                 |<-- messages / FloodWait / Private --  |
   |              |<-- CollectionOutcome (partial batch kept on error) ---                  |
   |              | persist_collected_messages()    |          |             |             |
   |              |---------------------------------------------------------->|            |
   |              |          FOR EACH message: raw evidence JSON -> INSERT ON CONFLICT      |
   |              |                             DO NOTHING -> advance checkpoint -> COMMIT  |
   |              |                                            |             |---------->  |
   |              | update source.last_message_id / last_collected_at + audit + COMMIT      |
   |              |------------------------------------------------------------------->   |
   |<-- structured JSON log: collection_completed --|          |             |             |
```

## 19.4 Live global search with quota fallback

```text
Client      API      SearchService   rate_policy   global_search   Telegram      Database
  |          |             |              |              |            |             |
  | GET /api/telegram/search/global?q=... |              |            |             |
  |--------->|             |              |              |            |             |
  |          |-----------> | _require_session()          |            |             |
  |          |             |------------------------------------------>            |
  |          |             |<-- ok (else 401, NO RPC issued) ---------              |
  |          |             | run_bounded_search(key)     |            |             |
  |          |             |------------->|              |            |             |
  |          |             |              | semaphore(3) + in-flight dedup           |
  |          |             |              |------------->|            |             |
  |          |             |              |              | checkSearchPostsFlood     |
  |          |             |              |              |----------->|             |
  |          |             |              |              |<-- remains / stars ------|
  |          |             |              |     free quota? --yes--> channels.searchPosts
  |          |             |              |              |----------->|  visibility=public
  |          |             |              |     --no--> warning, NO payment          |
  |          |             |              |              | messages.searchGlobal     |
  |          |             |              |              |----------->|  account_scoped
  |          |             |              |<-- results --|            |             |
  |          |             |<-- outcome --|              |            |             |
  |          |             | normalize -> local filters -> sort -> cursor            |
  |          |             | audit SEARCH_GLOBAL (query_length only) + COMMIT        |
  |          |             |------------------------------------------------------> |
  |          |<-- response |              |              |            |             |
  |<-- 200 --|  results + pagination + method_used + quota + warnings  |             |
  |                                                                                  |
  |  NOTHING IS PERSISTED. Only POST /api/telegram/search/results/save writes data.  |
```

## 19.5 Bot `/start` and link harvesting

```text
Client      API     BotService   bot_interaction   Telegram   url_classifier  SourceService  DB
  |          |          |              |              |             |              |         |
  | POST /api/telegram/bots/{id}/start |              |             |              |         |
  |--------->|--------> | load source; 404 / 422 if not a bot       |              |         |
  |          |          | verify_authorized -> get_entity            |              |        |
  |          |          |------------------------------>|            |              |        |
  |          |          | audit BOT_START_REQUESTED + COMMIT         |              |        |
  |          |          |----------------------------------------------------------------> |
  |          |          | start_bot()  |              |             |              |         |
  |          |          |------------->|              |             |              |         |
  |          |          |              | sendMessage("/start")      |              |         |
  |          |          |              |------------->|             |              |         |
  |          |          |              | poll iter_messages until reply or timeout |         |
  |          |          |              |------------->|             |              |         |
  |          |          |              |<-- reply ----|             |              |         |
  |          |          |<-- outcome --|              |             |              |         |
  |          |          | classify reply text + button URLs         |              |         |
  |          |          |------------------------------------------>|              |         |
  |          |          |<-- Telegram links only; others discarded, NEVER fetched -|         |
  |          |          | cap at 10; FOR EACH link: register DORMANT (monitoring off)        |
  |          |          |------------------------------------------------------->  |------->|
  |          |<-------- | audit LINK_DISCOVERED / INVITE_RESOLVED + COMMIT          |        |
  |<-- 200 --| status, response_text, buttons, discovered[]                          |        |
```

## 19.6 Backfill job (asynchronous)

```text
Client      API    BackfillService   asyncio task   Telegram   MessageService    Database
  |          |            |                |            |            |               |
  | POST /api/sources/{id}/backfill        |            |            |               |
  |--------->|----------> | source exists? access verified? (404 / 409)               |
  |          |            | INSERT backfill_jobs (PENDING) + audit + COMMIT           |
  |          |            |------------------------------------------------------->  |
  |          |            | create_task(_run_backfill_job)                            |
  |          |            |--------------->|            |            |               |
  |<-- 202 (job id) ------|                |            |            |               |
  |                                        | status = RUNNING; own DB session         |
  |                                        |------------------------------------->    |
  |                                        | verify_authorized -> get_entity          |
  |                                        |----------->|            |               |
  |                                        | iter_messages BACKWARD                   |
  |                                        |   offset_id = job checkpoint, else offset_date
  |                                        |----------->|            |               |
  |                                        |<-- older messages ------|               |
  |                                        | persist_backfill_batch()|               |
  |                                        |------------------------>|               |
  |                                        |    per message: evidence -> INSERT ON CONFLICT
  |                                        |    advance job.checkpoint_message_id (OLDEST)
  |                                        |    collection_checkpoints NEVER touched   |
  |                                        |                         |------------->  |
  |                                        | COMPLETED | PARTIAL | FAILED + audit     |
  | GET /api/sources/{id}/backfill/{job_id}|                         |------------->  |
  |--------->|----------> |----------------------------------------------------->     |
  |<-- 200 (status, messages_inserted, checkpoint_message_id) --------                |
```

<!--PAGEBREAK-->

# 20. Final Technical Summary

*For a Team Leader, Software Architect or Engineering Manager. This section stands alone.*

### 1. What does our Telegram API currently do?

It exposes **35 REST endpoints** that turn **one authenticated Telegram user account** into a controlled data-collection service. It registers Telegram channels/groups/bots as sources, determines each one's **real** access status by probing Telegram rather than assuming it, runs a legitimate join-request workflow for private sources with 12-hourly reconciliation, monitors accessible sources on a 5-minute checkpointed cycle, backfills history on demand, searches Telegram live, and stores everything in SQLite with per-message raw evidence and SHA-256 hashes. Its scope is deliberately **data collection and access management** — analysis is downstream.

Real usage to date: **12,805 messages collected across 4 sources over 71 completed collection cycles, with one recorded failure and no rate-limit incidents.**

### 2. What endpoints are available?

| Group | Count | Highlights |
| --- | ---: | --- |
| Health / status | 2 | `/health`, `/ready` |
| Telegram session / auth | 4 | 3-step non-interactive login, status |
| Source management | 8 | register, CRUD, request-access, check-access |
| Monitoring | 3 | start (runs the first cycle inline), stop, status |
| Stored messages | 3 | per-source, by id, flat-filter search |
| Live Telegram search | 5 | global, per-source, arbitrary channel, multi-source, save-result |
| Discovery | 3 | channel search, register discovered, extract links |
| Bots / invites | 2 | bot `/start`, join by invite |
| Backfill | 3 | start (202), list, poll |
| Notifications | 2 | list, mark read |

### 3. How does the data flow?

`Client -> FastAPI router -> Service -> (Telegram layer via Telethon | Repository layer via SQLAlchemy) -> Telegram MTProto | SQLite`. Routes never touch Telethon or SQL; `app/telegram/*` is the only Telethon importer; repositories are the only query builders. Live search is **read-only and ephemeral** — the sole path that writes search data is `POST /api/telegram/search/results/save`. Collection persists one message per transaction, advancing a checkpoint only past durably committed rows.

### 4. What Telegram capabilities are available?

Read public channels and groups; read private sources the account has joined; search within any accessible channel; discover channels by keyword; join public channels and invite links; submit approval-gated join requests; monitor continuously; backfill history; capture media metadata (and optionally download bytes); send exactly one `/start` to a registered bot.

### 5. What Telegram capabilities are restricted?

* Private content before Telegram approves access — **hard stop, by design**.
* Cross-membership public search — quota-gated by Telegram; the fallback is **account-scoped**, not a search of all Telegram.
* Detecting a silently declined username-based join request — **no signal exists**.
* Channel discovery pagination — Telegram provides none.
* Deleted or restricted content — not recoverable.
* Sender identity, view and reply counts — frequently absent; stored as `NULL`, never fabricated.
* Reactions, polls, comment threads, stories, participant lists — **not implemented**.

### 6. What are the major technical limitations?

**Platform:** access equals one account's permissions; FloodWait; search quotas; no decline signal; history-visibility rules.
**Application:** no caller authentication; **two endpoints (`PATCH /api/sources/{id}`, `POST /api/notifications/{id}/read`) return 200 without persisting**; backfill does not survive a restart; no application rate limiting or FloodWait enforcement; single account and single process; no migrations; the README's event-driven fast path is not wired in, so approval latency is up to 12 hours.

### 7. What security considerations exist?

**One High finding: the API has no authentication of any kind, and the default bind is `0.0.0.0`.** Anyone who can reach the port can make the Telegram account join channels, send `/start` messages, drive search until Telegram rate-limits it, read all collected third-party content, and delete sources with their data. Swagger UI at `/docs` is an unauthenticated console over all of it.

Medium findings: the session file is an unencrypted bearer credential; login codes and the 2FA password cross the wire in clear text; a populated `.env` sits in the working tree; 12,805 collected messages are stored unencrypted with no retention policy; there is no API rate limiting.

Confirmed positives: credentials are never logged or returned; phone numbers are masked; search queries are logged only by length; no SSRF surface (non-Telegram URLs are never fetched); no SQL injection surface; secrets are correctly gitignored.

### 8. What should be improved next?

**Before any non-local deployment**
1. Add API authentication; bind to `127.0.0.1`; put TLS in front.
2. Fix the two missing commits (**ISS-01**) — one line each, plus regression tests.
3. Bound every `limit` parameter.

**Then, for account safety**
4. Enforce FloodWait centrally instead of only reporting it.
5. Add a global outbound Telegram request budget covering collection, joins and link extraction — not just search.
6. Rate-limit joins; add jitter to collection cycles; alert on account health.

**Then, for correctness and operations**
7. Backfill lifecycle: startup reconciliation, cancellation, per-source concurrency guard.
8. Alembic migrations and a dialect-agnostic upsert before any PostgreSQL move.
9. Wire in or delete the event handler; correct the README where it disagrees with the code.
10. Decide policy on user/DM sources — the generic source path can monitor a one-to-one conversation, which is very likely not intended.

### Overall assessment

The architecture is **sound and unusually disciplined for a proof of concept**: strict layering that holds under inspection, a validated state machine that refuses to guess access, restart-safe checkpointing backed by a database-level uniqueness guarantee, evidence preservation, comprehensive audit logging, and an explicit, well-documented refusal to bypass Telegram's controls. The test suite (189 tests, with a coverage guard that forbids untested endpoints) is a genuine strength.

The gaps are **operational, not architectural**: no authentication, two silent persistence bugs, and no enforcement of the rate-limit discipline the design otherwise respects. All are small, well-isolated changes. **Recommendation: keep the service on a trusted network only, fix ISS-01 and add authentication before any wider deployment, then address the account-safety backlog before increasing collection volume.**
