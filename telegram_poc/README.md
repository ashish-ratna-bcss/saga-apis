# Telegram OSINT Collection Service

A Telegram data-collection and access-management backend: authenticates one
Telegram account via MTProto (Telethon), registers sources to monitor,
determines their **real** access status, runs a legitimate join-request
workflow for private sources, and collects messages incrementally into
SQLite once — and only once — access has been verified.

This service is the **data collection and access management layer** of a
larger OSINT platform. It is not the whole platform: NLP, OCR, entity
extraction, correlation, knowledge graphs, risk scoring, and case/evidence
management are explicitly out of scope here and are expected to consume this
service's normalized SQLite tables through their own interfaces later (see
"Future OSINT pipeline" below).

## 0. Non-negotiable scope statement

**This system does not, and will not, bypass Telegram's access controls.**
It only ever collects data that:

1. Telegram already exposes to the authenticated account (public channels,
   channels/groups it has joined), or
2. becomes available after a **legitimate** join request that Telegram
   itself approves.

No invite-hash guessing, no membership spoofing, no scraping around
`ChannelPrivateError`, no repeated/duplicate join requests, no automatic
"alternative access" attempts after a rejection. See "Known Telegram
limitations" for what this means in practice.

---

## 1. Architecture

```
FastAPI routes (app/api/*)
        |
        v
Service layer (app/services/*)   <-- all business logic and orchestration
        |
        v
app/telegram/*  (Telethon)   <->   app/database/repositories/*  (SQLAlchemy)
        |                                   |
        v                                   v
   Telegram MTProto                     SQLite (swappable)
```

- **Routes never call Telethon or build SQL.** They only call a service.
- **Services** orchestrate `app/telegram/*` (Telegram operations) and
  `app/database/repositories/*` (persistence), and are the only place that
  enforces business rules (no duplicate joins, checkpoint correctness,
  state-machine transitions).
- **`app/telegram/*`** is the only code that imports Telethon. It is built
  around plain functions/dataclasses so it can be exercised in tests with a
  fake client - no real Telegram connection required for the test suite.
- **`app/database/*`** is plain SQLAlchemy async ORM. Swapping SQLite for
  Postgres later is a one-line `DATABASE_URL` change - no business logic
  touches raw SQL or a specific dialect.

## 2. Project structure

```
telegram_service/
├── app/
│   ├── main.py                 FastAPI app, lifespan (DB init, client connect, scheduler)
│   ├── config.py                Settings (env-driven, pydantic-settings)
│   ├── api/                     Routes - thin, service-only
│   ├── telegram/                Telethon-facing layer (client, auth, discovery,
│   │                             access_manager, join_request_manager, collector,
│   │                             event_handler)
│   ├── services/                Orchestration + business rules
│   ├── database/                SQLAlchemy models, engine/session, repositories/
│   ├── scheduler/                4 background jobs (APScheduler)
│   ├── schemas/                  Pydantic request/response models
│   └── notifications/            Pluggable notification dispatch
├── tests/                        pytest, no real Telegram/DB required
├── docs/
│   ├── technical-report.md       Capability/limitation analysis of the proof-of-concept
│   └── poc-guide.md              How to run the archived proof-of-concept CLI
├── poc/                          Archived single-file proof-of-concept (see below)
│   ├── main.py
│   ├── output/                   Retrieved message content (gitignored)
│   └── tests/                    Its own stdlib-unittest suite, run separately
├── data/                         SQLite DB, session file, raw evidence, media (gitignored, created at runtime)
├── .env.example
├── pytest.ini
├── requirements.txt
└── README.md
```

`poc/` is the original standalone Telethon script this service grew out of.
It is kept as reference prior art (the Telegram layer's connect/auth and
global-search behavior were derived from it) and is still runnable, but it is
not part of the service and shares none of its code. It needs no separate
environment - the service's own dependencies cover it:

```bash
cd poc
python main.py --help
python -m pytest tests          # 32 tests, no network
```

Its suite is deliberately excluded from the service's own test run
(`norecursedirs` in `pytest.ini`), because both define a `tests` package and
collecting them together would collide.

## 3. Installation

```bash
cd telegram_service
python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # then edit .env
```

## 4. Environment variables

See `.env.example` for the full list with defaults. Key ones:

| Variable | Purpose |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | MTProto app credentials from https://my.telegram.org/apps. Never hard-coded, never logged. |
| `TELEGRAM_SESSION_PATH` | Where Telethon persists the authorized session (a credential - see Security). |
| `DATABASE_URL` | SQLAlchemy URL. SQLite by default; swap to Postgres later without touching business logic. |
| `MEDIA_DOWNLOAD_ENABLED` / `MEDIA_STORAGE_PATH` / `MAXIMUM_MEDIA_SIZE_BYTES` / `ALLOWED_MEDIA_TYPES` | Media download policy - off by default. Metadata is always recorded regardless. |
| `RAW_EVIDENCE_STORAGE_PATH` | Where raw per-message JSON is preserved for evidence purposes. |
| `ACCESS_RECONCILIATION_INTERVAL_HOURS` | Mandatory pending-access check interval (default 12). |
| `COLLECTION_INTERVAL_MINUTES` / `CONNECTION_HEALTH_INTERVAL_MINUTES` / `NOTIFICATION_PROCESSING_INTERVAL_MINUTES` | Other scheduler cadences. |

## 5. Telegram API credentials setup

1. Log in at https://my.telegram.org/apps with the Telegram account you want
   to authenticate as.
2. Create an application; copy `api_id` and `api_hash` into `.env`.
3. These credentials belong to **one Telegram account** - this service
   operates with that account's actual permissions, nothing more.

## 6. Authentication process

The server cannot block on an interactive terminal prompt the way a CLI
script can, so login is split into explicit HTTP steps:

```
POST /api/telegram/auth/send-code       { "phone": "+1..." }
POST /api/telegram/auth/verify-code     { "phone": "+1...", "code": "12345" }
POST /api/telegram/auth/verify-password { "password": "..." }   # only if 2FA is enabled
GET  /api/telegram/status
```

Once authorized, the session file at `TELEGRAM_SESSION_PATH` is reused on
every restart - no repeated login. `GET /api/telegram/status` never returns
session data, api_hash, or an unmasked phone number:

```json
{
  "connected": true,
  "authenticated": true,
  "account": { "id": "123456789", "username": "someone", "phone": "+1***4567" }
}
```

## 7. SQLite setup

No manual migration step is needed for this PoC: `init_db()` runs
`Base.metadata.create_all` on startup, creating any missing tables. The 10
tables are: `telegram_accounts`, `telegram_sources`,
`telegram_access_requests`, `telegram_messages`, `telegram_media`,
`telegram_entities`, `collection_checkpoints`, `notifications`,
`audit_logs`, `scheduler_jobs`. `telegram_messages` has a
`UNIQUE(source_id, telegram_message_id)` constraint - the actual dedup
guarantee, not just application logic.

## 8. API endpoints

35 endpoints in total. The live contract is always `/openapi.json`, and
`tests/test_endpoint_coverage.py` fails if any endpoint listed there is not
exercised, so this table cannot silently drift from the code.

**Connection and login**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/telegram/status` | Connection/auth status |
| POST | `/api/telegram/auth/send-code` | Start login |
| POST | `/api/telegram/auth/verify-code` | Complete login (or trigger 2FA) |
| POST | `/api/telegram/auth/verify-password` | Complete 2FA |

**Sources**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/sources` | Register a source (resolves + checks access immediately) |
| GET | `/api/sources` | List sources |
| GET | `/api/sources/{id}` | Get one source |
| PATCH | `/api/sources/{id}` | Update (e.g. `monitoring_enabled`, `title`) |
| DELETE | `/api/sources/{id}` | Remove a source |
| POST | `/api/sources/{id}/request-access` | Submit a join request (idempotent - never duplicates) |
| GET | `/api/sources/{id}/access-status` | Current access status |
| POST | `/api/sources/{id}/check-access` | Re-probe Telegram for current access |

Registration is deduplicated twice: once on the normalized identifier, and
again on the resolved `telegram_entity_id`, so the same channel added as
`@name` and as its numeric id is rejected with 409 rather than tracked twice.

**Monitoring**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/sources/{id}/monitoring/start` | Start monitoring (requires verified access) + runs initial collection |
| POST | `/api/sources/{id}/monitoring/stop` | Stop monitoring |
| GET | `/api/sources/{id}/monitoring-status` | Monitoring status |

**Stored messages** - these read the local `telegram_messages` table and never call Telegram.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/sources/{id}/messages` | Collected messages for one source (filterable) |
| GET | `/api/messages/{id}` | One message |
| GET | `/api/messages` | Application-level search across all collected messages |

**Live Telegram search** - these call Telegram on every request and never read stored rows.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/telegram/search/global` | Telegram-wide search across public channels |
| POST | `/api/telegram/search/sources` | Search a selected set of registered sources |
| GET | `/api/sources/{id}/search` | Search within one registered source |
| GET | `/api/telegram/channels/{channel_id}/search` | Search within an arbitrary, not-yet-registered channel |
| POST | `/api/telegram/search/results/save` | Persist one search result (the only way search writes to the DB) |

**Discovery**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/telegram/channels/search` | Find candidate channels/groups (returns channels, not messages) |
| POST | `/api/telegram/channels/{telegram_id}/register` | Register a discovered channel as a source |
| POST | `/api/telegram/discovery/extract-links` | Classify Telegram links in free text (read-only preview) |

**Bots and invite links**

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/telegram/bots/{id}/start` | Send one `/start` to a registered bot and read its reply |
| POST | `/api/telegram/invites/join` | Join, or request to join, via `t.me/+hash` |

**Historical backfill** - walks backward into a source's history as a background job.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/sources/{id}/backfill` | Queue a backfill; returns 202 with a job id |
| GET | `/api/sources/{id}/backfill` | List backfill jobs for a source |
| GET | `/api/sources/{id}/backfill/{job_id}` | Poll one job's progress |

**Operational**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (reports whether Telegram is configured) |

## 9. Source lifecycle (access state machine)

```
DISCOVERED
  |
  +-- public / already readable --> PUBLIC_ACCESSIBLE --> MONITORING
  |
  +-- already a member ------------> ACCESSIBLE --------> MONITORING
  |
  +-- private, needs joining -------> JOIN_REQUEST_REQUIRED
  |                                        |
  |                                        v
  |                                 JOIN_REQUEST_PENDING
  |                                   |            |
  |                          approved v            v rejected
  |                        JOIN_REQUEST_APPROVED   JOIN_REQUEST_REJECTED
  |                                   |                  |
  |                                   v                  v
  |                                JOINED             DISABLED
  |                                   |
  |                                   v
  |                               MONITORING
  |
  +-- cannot access ----------------> ACCESS_DENIED
  +-- doesn't exist -----------------> NOT_FOUND
  +-- transient failure -------------> ERROR (recoverable via re-check)
```

Every transition is validated against an explicit allow-list
(`app/telegram/access_manager.py::ALLOWED_TRANSITIONS`) - e.g. you cannot
jump straight from `DISCOVERED` to `MONITORING`, and a `JOIN_REQUEST_REJECTED`
source can only reach `DISABLED`, never silently retried into `JOINED`.

Status is never inferred from "the entity resolved" - `check_access` always
attempts a real (lightweight) read against Telegram (or `CheckChatInviteRequest`
for invite links) to tell DISCOVERED apart from actually-accessible.

## 10. Join-request workflow

1. `check_access` determines `JOIN_REQUEST_REQUIRED`.
2. `POST /api/sources/{id}/request-access`:
   - If a `PENDING` request already exists for this source, it is returned
     as-is - **no second request is ever submitted.**
   - Otherwise, exactly one join attempt is made
     (`ImportChatInviteRequest` for invite links, `JoinChannelRequest` for
     public join-request-enabled channels).
   - If Telegram joins immediately, the source goes straight to `JOINED`.
   - If Telegram responds `INVITE_REQUEST_SENT` (approval required), the
     source becomes `JOIN_REQUEST_PENDING` and an access-request row is
     stored with `requested_at`, `next_check_at` (+12h), `attempt_count`.
3. No alternative access mechanism is ever attempted after a rejection.

## 11. 12-hour reconciliation mechanism

Two complementary mechanisms, per the requirement:

- **Event-driven (best-effort, fast path):** `app/telegram/event_handler.py`
  listens for a Telethon `ChatAction` update where the authenticated account
  itself appears to have joined a chat, and can trigger an immediate
  reconciliation for that one source. Telegram does not guarantee a
  distinct "request approved" event separate from ordinary membership
  updates, so this is explicitly a fast path, not the source of truth.
- **Mandatory 12-hour job (source of truth):** `reconcile_pending_access`
  (in `app/scheduler/jobs.py`) runs every `ACCESS_RECONCILIATION_INTERVAL_HOURS`
  (default 12) and checks every `JOIN_REQUEST_PENDING` request:
  - Invite-hash requests: re-issues `CheckChatInviteRequest`. `ChatInviteAlready`
    = approved + verified membership. `InviteHashExpiredError` /
    `InviteHashInvalidError` = treated as rejected (the invite is no longer
    usable).
  - Username-based join requests: re-fetches the entity and checks Telegram's
    own `left` flag. `left == False` = approved + verified membership.
  - **Known limitation, documented rather than worked around:** a declined
    (not just pending) join request on a public join-request-enabled
    channel does not expose an explicit "declined" signal to the requesting
    account through ordinary polling. Such requests accurately remain
    `JOIN_REQUEST_PENDING` - this reflects what the Telegram API actually
    exposes, not a bug.
  - This job never fires more than once concurrently (`max_instances=1` on
    every APScheduler job) and never polls faster than its configured
    interval - no tight/aggressive retry loop anywhere.

## 12. Notification mechanism

Every meaningful state change persists a `Notification` row first
(`notifications` table), e.g.:

```json
{
  "event_type": "TELEGRAM_ACCESS_GRANTED",
  "source_id": 123,
  "telegram_entity_id": "...",
  "source_name": "Example Channel",
  "previous_status": "JOIN_REQUEST_PENDING",
  "new_status": "JOINED",
  "timestamp": "..."
}
```

`GET /api/notifications` reads them back. Dispatch to an external channel
(websocket/webhook/email/dashboard) is pluggable and **not hard-coded**:
register a dispatcher with `app.notifications.manager.notification_manager.register(...)`
and the `process_notifications` job will call it for every unprocessed
notification. With nothing registered, notifications simply accumulate in
SQLite and are readable via the API.

## 13. Message collection mechanism

`app/telegram/collector.py::collect_new_messages` calls
`client.iter_messages(entity, min_id=<checkpoint>, reverse=True, limit=<batch>)`
so only messages newer than the checkpoint are ever fetched, oldest-first.
Each message is normalized (text, sender, dates, views/forwards/replies,
media metadata, a permalink if a username is known) and hashed
(`raw_data_hash`, SHA-256 of the raw Telethon payload) before persistence.
Fields Telegram doesn't expose for a given message are stored as `NULL`,
never guessed.

Media is always recorded as metadata; actual bytes are only downloaded when
`MEDIA_DOWNLOAD_ENABLED=true`, under `MAXIMUM_MEDIA_SIZE_BYTES`, and only for
`ALLOWED_MEDIA_TYPES` - downloaded files are SHA-256 hashed for evidence
integrity.

## 14. Checkpoint mechanism

Each source has one `collection_checkpoints` row (`last_message_id`,
`last_success_at`, `last_error_at`, `error_count`). `MessageService.persist_collected_messages`
commits **one message at a time**: the checkpoint only ever advances to a
message ID that is durably committed. If persistence fails partway through
a batch, everything already committed stays committed, the checkpoint stops
exactly there, and `error_count`/`last_error_message` record why - the next
cycle (scheduled or after an app restart) resumes from that exact point.
Combined with the DB-level `UNIQUE(source_id, telegram_message_id)`
constraint (`INSERT ... ON CONFLICT DO NOTHING`), re-collecting an
already-seen range is a safe no-op, not a duplicate.

## 15. Error handling

| Telegram error | Handling |
|---|---|
| `FloodWaitError` | Collection/access-check stops for that cycle, reports the wait time; no sleep-and-retry loop - the next scheduled cycle tries again. |
| `ChannelPrivateError` | Mapped to `JOIN_REQUEST_REQUIRED` or `ACCESS_DENIED` depending on whether a username/invite is known - never bypassed. |
| `ChatAdminRequiredError` | `ACCESS_DENIED`. |
| `UserNotParticipantError` / entity `left` flag | Used to positively confirm pending-vs-approved membership. |
| `UsernameNotOccupiedError` / `UsernameInvalidError` / `ChannelInvalidError` | `NOT_FOUND`. |
| `InviteHashInvalidError` / `InviteHashExpiredError` | `NOT_FOUND` (unknown invite) or treated as a rejection signal for a pending request. |
| Network/`ConnectionError`/`OSError` | Connection layer reports failure; health-check job retries on its own interval, never in a tight loop. |

Every collection cycle and access check writes an `audit_logs` row
regardless of outcome, and failures never crash the process - the source's
`status_reason` and the checkpoint's `error_count`/`last_error_message`
capture what happened.

## 16. Security

- `TELEGRAM_API_HASH`, verification codes, 2FA passwords, and phone numbers
  are **never logged or returned by any API response**. `GET
  /api/telegram/status` returns a masked phone only.
- The Telethon session file (`TELEGRAM_SESSION_PATH`) is a bearer credential
  for the Telegram account - treated like a secret, gitignored, never
  exposed through any endpoint.
- `.env`, `*.session*`, `data/` (DB, raw evidence, media) are all gitignored.
- Structured logs never include raw credential values.

## 17. Testing

```bash
pip install -r requirements.txt
pytest -v
```

68 tests, no real Telegram connection or on-disk database required (SQLite
in-memory + a fake Telethon-shaped client, mirroring the pattern already
used by `poc/tests/test_main.py`). Coverage includes the
state machine, identifier normalization, access-probing decisions for every
branch (public/already-member/private-needs-join/private-no-invite/invite-hash
variants), join-request submission and no-duplicate enforcement, pending-request
reconciliation (approval/rejection/still-pending), the collector's dedup and
FloodWait handling, checkpoint restart-safety, DB constraints/cascades, and
the full HTTP API surface. See "Acceptance scenarios" below for how these
map to Scenarios A-D.

## 18. Known Telegram limitations

**This system does not claim "all Telegram data can be collected."**
Specifically:

- Access is controlled entirely by Telegram and by this account's actual
  permissions - not by anything this codebase can grant itself.
- Private channels/groups may require an invitation or an approved join
  request; until Telegram approves it, no message data from that source is
  ever collected.
- A declined (not just pending) join request on a public join-request
  channel has no explicit "declined" API signal available to this account -
  such requests remain accurately `PENDING` rather than being guessed at.
- Some message metadata (e.g. sender identity, reply counts, view counts)
  may be unavailable depending on the source and account - stored as `NULL`,
  never fabricated.
- Deleted or otherwise inaccessible content is not assumed recoverable.
- `FloodWaitError` and other rate limits are respected, never bypassed.
- Public availability of a channel does not imply every Telegram API method
  can retrieve its entire history without restriction.

## 19. How to run locally

```bash
uvicorn telegram_app.main:app --reload
```

The scheduler (all 4 jobs) starts automatically as part of the app's
lifespan - there is no separate process to launch for a basic run.

## 20. How to run the scheduler

The scheduler is embedded in the FastAPI process (`app/main.py`'s
`lifespan`) via `app/scheduler/jobs.py::setup_scheduler`, using
`AsyncIOScheduler` so it shares the same event loop as the Telethon client
and the API. Running `uvicorn telegram_app.main:app` is sufficient - there is no
separate scheduler binary in this PoC. Each job's cadence is configurable
via the `*_INTERVAL_*` environment variables; every job carries
`max_instances=1` so overlapping runs of the same job are refused, not
merely discouraged.

## 21. Example API requests/responses

Register a public source:
```bash
curl -X POST http://localhost:8000/api/sources \
  -H "Content-Type: application/json" \
  -d '{"identifier": "@durov", "monitoring_enabled": true}'
```
```json
{
  "id": 1, "identifier": "@durov", "telegram_entity_id": "780220639",
  "username": "durov", "title": "Pavel Durov", "source_type": "user",
  "access_status": "ACCESSIBLE", "monitoring_enabled": true, ...
}
```

Start monitoring (runs the initial collection immediately):
```bash
curl -X POST http://localhost:8000/api/sources/1/monitoring/start
```

Request access to a private source:
```bash
curl -X POST http://localhost:8000/api/sources/2/request-access
```
```json
{ "id": 1, "source_id": 2, "status": "PENDING", "requested_at": "...", "next_check_at": "...", "attempt_count": 1 }
```

Search collected messages:
```bash
curl "http://localhost:8000/api/messages?keyword=example&media_type=photo&limit=20"
```

---

## Acceptance scenarios - what's verified

- **Scenario A (public channel, restart-safe incremental collection):**
  `tests/test_monitoring_service.py::test_scenario_a_public_channel_full_lifecycle`
- **Scenario B (private source, approval detected, auto-monitoring starts):**
  `tests/test_scheduler_jobs.py::test_scenario_b_reconciliation_detects_approval_and_starts_monitoring`
- **Scenario C (request rejected, monitoring never starts):**
  `tests/test_scheduler_jobs.py::test_scenario_c_reconciliation_detects_rejection_and_does_not_start_monitoring`
- **Scenario D (FloodWait during collection, no crash, checkpoint consistent):**
  `tests/test_monitoring_service.py::test_scenario_d_flood_wait_during_collection_does_not_crash_and_preserves_checkpoint`

## What's implemented vs. pending

**Implemented:** everything in sections 3-19 of the original requirements -
FastAPI app, Telethon client with session reuse and non-interactive login,
SQLite via async SQLAlchemy (10 tables, migrations via `create_all`), source
registration/normalization/resolution, the full access state machine,
public and private source handling, join-request submission with no-duplicate
enforcement, 12h reconciliation + best-effort event-driven fast path,
approval/rejection detection, persisted notifications with pluggable
dispatch, automatic monitoring start after verified approval, incremental
collection with restart-safe checkpointing and DB-level dedup, media
metadata capture with optional policy-gated download, raw-evidence JSON +
SHA-256 hashing, application-level search independent of Telegram's own
search RPCs, audit logging on every significant operation, structured
per-cycle collection logs, and 68 passing tests.

**Pending / explicitly out of scope for this PoC:**
- NLP, OCR, entity extraction, language detection, correlation, knowledge
  graph, risk scoring, alerting, and case/evidence management are not
  implemented - this service is designed to hand off its normalized SQLite
  tables to those as separate components (section 20 of the original spec).
- No WebSocket/webhook/email notification provider is wired in by default
  (the dispatch mechanism is ready; no specific provider was requested).
- No production deployment tooling (Docker/CI) was requested or added.
- Boolean/nested `AND/OR/NOT` query syntax for `/api/messages` search is not
  implemented - only flat filters (keyword, source, sender, date range,
  media type), as the base case explicitly required; nested boolean queries
  were listed as a later enhancement in the spec, not an initial requirement.
