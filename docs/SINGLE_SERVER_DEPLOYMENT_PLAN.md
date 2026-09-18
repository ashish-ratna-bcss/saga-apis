# Single-Server Deployment Plan — Bluweb / OSINT / reddit_server / telegram_poc

Status: **planning document only — nothing in this repo tree has been modified to produce this report.**
Scope: consolidate four independently-developed API services onto one server without changing any existing application behavior, code, schema, or endpoint contract.

---

## 1. Executive Summary

All four services are independent Python/FastAPI applications, each with its own git repo, virtualenv, and (for three of the four) its own `docker-compose.yml`/`Dockerfile`. They were never designed to run together: **all four default to TCP port 8000**, three of the four ship **zero application-level authentication**, and one (`telegram_poc`) has no containerization at all today.

The good news, discovered during code inspection rather than assumed:

- **No Redis is used by any of the four services.** There is nothing to share or namespace there.
- **No shared database is required.** Bluweb uses an external, pre-provisioned Postgres; OSINT optionally uses its own Postgres (or SQLite); reddit_server uses no database; telegram_poc uses its own local SQLite file. They can remain four fully separate data stores with zero code changes.
- **No shared filesystem paths are required.** Each service's persistent state (Bluweb's `storage/` Crawlee frontier + MinIO, OSINT's `osint.db`/`data/`, telegram_poc's `data/` directory, reddit_server's — nothing) lives entirely inside its own repo directory.
- Three of the four services (Bluweb, OSINT, reddit_server) already containerize cleanly with **Docker Compose**, which none of the others use a *different* tool for — so Compose is the natural common denominator, not a new choice imposed on the project.

**Recommended architecture (detail in §13/§25):** one new top-level `docker-compose.yml` at `~/Saga-APIs/` that builds all four existing Dockerfiles (adding one new Dockerfile for `telegram_poc`, since none exists) as independently-named, independently-restartable containers on one shared internal Docker network, fronted by a single Nginx reverse proxy container that is the only thing exposed to the internet. Each service keeps its own env file, its own data volume, and its own container — **no code merge, no shared process, no shared database.** Public routing uses **subdomains**, not path prefixes (justification in §14).

The one service that structurally cannot be run as more than one instance is `telegram_poc` (its MTProto session is a single-owner, non-shardable resource) — this is a hard constraint from Telegram's own protocol, not a limitation introduced by this plan.

---

## 2. Current Architecture (as discovered)

| | Bluweb | OSINT | reddit_server | telegram_poc |
|---|---|---|---|---|
| Language | Python 3.12 | Python 3.12 | Python ≥3.11 (3.12 in Docker) | Python ≥3.10 (unpinned) |
| Framework | FastAPI 0.141 + uvicorn | FastAPI 0.115 + uvicorn | FastAPI (0.115–1.0) + uvicorn | FastAPI + uvicorn |
| Entrypoint | `app/main.py:app` | `app/main.py:app` (+ `app/worker_main.py`) | `app/main.py:app` (via `run.py` locally, direct uvicorn in Docker) | `app/main.py:app` (no launcher script at all) |
| Containerized today? | Yes (Dockerfile + compose: `api` + `minio`) | Yes (Dockerfile + compose: `postgres`+`migrate`+`api`+`worker`) | Yes (Dockerfile + compose: `reddit-service`, single container) | **No** — no Dockerfile, no compose, no systemd unit; dev-only `uvicorn --reload` |
| Own git repo | Yes | Yes | Yes | Yes |
| Own `.venv` | Yes | Yes | Yes (no `.venv` dir but pip-installable package) | Yes (implied, not inspected) |

Each is a standalone product with its own README, its own `.env.example`, and (per each service's own docs) its own intended external caller — there is **no existing code-level coupling between any of the four services**. reddit_server's README does mention it mirrors conventions from sibling "telegram_service"/"discord_service" projects (not this `telegram_poc`), which only means naming conventions are similar by convention, not that they call each other.

---

## 3. Service Inventory

### 3.1 Bluweb — Web Intelligence Collection Backend
Crawls arbitrary public websites, extracts structured content/entities, tracks change over time. Single-process async app; the crawl engine and monitoring scheduler run as `asyncio` tasks *inside* the API process (no separate worker).

- Default port: **8000**, bound `0.0.0.0` (hardcoded in Dockerfile/compose, not overridable via env var).
- Auth: **none**.
- DB: external Postgres (not run by this app), single unified table `webintel_unified`, schema applied manually from `docs/unified_schema.sql`. Alembic migrations exist but are explicitly obsolete/unused — never run `alembic upgrade head` against the real DB.
- Object storage: MinIO (ports 9000 API / 9001 console), stores raw crawled HTML/PDF bytes.
- Cache/queue: none (no Redis anywhere).
- Heavy runtime deps: torch (CPU-only build required), spaCy, GLiNER/transformers (entity extraction, all CPU), Playwright + Chromium (up to 8 concurrent browser processes globally, each a real OS process).
- Currently deployed, per `integration.md`, directly on an AWS EC2 box's public IP:8000 with no reverse proxy and no auth.

### 3.2 OSINT — Investigation / Identifier-Lookup Backend
Fans out phone/email/username/domain/person lookups across free OSINT sources (Sherlock, Maigret, holehe, Wikidata, HIBP, a self-hosted SearxNG for public web search), correlates results, supports async multi-step "investigations" with pivoting.

- Default port: **8000** (configurable via `OSINT_PORT`; docker-compose hardcodes it to 8000 anyway).
- Auth: **none** — API-key auth existed once and was **deliberately removed** (migration `a1c6b3dbae10_drop_api_key_auth.py`); docs state this is permanent policy ("no login step, ever").
- DB: dual-mode — SQLite by default (`osint.db` at repo root, currently present from dev use) or Postgres for production (own docker-compose Postgres container on host port 5434, non-default on purpose). Postgres itself doubles as the job queue (`SELECT ... FOR UPDATE SKIP LOCKED`) — this is why OSINT needs no Redis.
- Background workers: `app/worker_main.py`, separate process(es) in Postgres mode only, horizontally scalable (`docker compose up --scale worker=N`); in SQLite mode the same work runs in-process inside the API.
- Real external architectural dependency: a **self-hosted SearxNG instance** (native process or container, port 8890 by default) — `public_web` lookups and `/api/v1/search` degrade gracefully without it, they don't hard-fail the whole service.
- Cache/queue: none (Postgres/SKIP LOCKED substitutes for both).

### 3.3 reddit_server — Reddit Data Provider API
Stateless read-only proxy over Reddit: official OAuth2 REST API for authenticated lookups, plus a separate public-RSS/Atom path needing no Reddit credentials at all. Built for an external consumer ("SOC Eye") to poll.

- Default port: **8000**, hardcoded in the Docker image (`EXPOSE 8000`, CMD `--port 8000`); the `PORT` env var only works via the local-dev `run.py` path, not the containerized path.
- Auth: shared-secret `X-API-Key` header, checked with constant-time comparison, controlled by `API_KEYS` env var. **Disabled by default** if `API_KEYS` is empty — must be set for production.
- DB: **none**. Cache/queue: **none shared** — has its own **in-process, per-replica** RSS feed cache + token-bucket rate limiter, which is why this service must run as **exactly one process/replica** (documented in its own DEPLOYMENT.md) or the RSS rate budget/cache silently splits N ways.
- No filesystem state at all — fully stateless container, safe with a read-only root filesystem.

### 3.4 telegram_poc — Telegram OSINT Collection Service
Wraps one authorized Telegram **user account** (via Telethon/MTProto, not the Bot API) to resolve/monitor/search Telegram channels, collect messages, download media, and expose both a stateful (DB-backed) and a stateless ("provider API") view of the data. Includes a self-served browser operator console at `/console`.

- Default port: **effectively undefined today** — `API_HOST`/`API_PORT` settings exist but are read nowhere in the code; the only documented run command (`uvicorn app.main:app --reload`) falls back to uvicorn's own default of `127.0.0.1:8000`. **A real host/port must be decided explicitly at the deployment layer**, since the app does not enforce one itself.
- Auth: **none at the API layer** (caller → API). Separately, real Telegram user-account auth (send-code/verify-code/verify-password handshake) governs API → Telegram, and is stored as a single Telethon **session file** — a stateful, single-owner, non-shardable credential tied to one phone number/device registration.
- DB: local SQLite (`data/telegram_service.db`), swappable to Postgres later via `DATABASE_URL` with no code changes needed, but not done today.
- Background jobs: 4 APScheduler interval jobs running **in the same process/event loop** as the API (not a separate worker) — access reconciliation, message collection, connection health, notification dispatch.
- **Structural constraint, not a bug**: this service must run as **exactly one process, one replica, ever**. Multiple copies would each try to drive the same Telegram session and the same 4 scheduled jobs independently, corrupt the in-memory login handshake state, and double up on Telegram's own per-account rate limits.
- No Dockerfile/compose/systemd unit exists in the repo today — needs one designed (packaging only, zero application code changes).

---

## 4. Endpoint Inventory

Full per-endpoint detail (method, path, auth, dependencies, response shape, recommended public/internal URL) is in the companion document **[`docs/SINGLE_SERVER_ENDPOINT_MAP.md`](./SINGLE_SERVER_ENDPOINT_MAP.md)**. Summary counts:

| Service | Endpoint count | Base path(s) | Streaming/WS/webhooks? | Long-running/async endpoints |
|---|---|---|---|---|
| Bluweb | 37 | `/api/v1/*` + unprefixed `/health*`, `/metrics` | None | `POST /api/v1/crawls` (202, background task), `POST /api/v1/search/instant` (blocks up to 30s) |
| OSINT | 20 | `/api/v1/*` + unprefixed `/health`, `/ready`, `/metrics` | None | `POST /api/v1/investigations` (201, async worker picks it up), username lookup ("can take minutes", synchronous) |
| reddit_server | 19 | `/api/reddit/*` (RSS sub-prefixed `/api/reddit/rss/*`) + unprefixed `/health`, `/ready` | None | None — every call is synchronous request/response |
| telegram_poc | 47 (44 JSON + `/console` + `/health` + `/ready`) | `/api/telegram/*`, `/api/sources/*`, `/api/messages*`, `/api/notifications/*` | None (media endpoints return binary, not streamed) | `POST /api/sources/{id}/backfill` (202, `asyncio.create_task`, not restart-safe) |

**Path collision found**: Bluweb and OSINT both use the **identical** base path `/api/v1/*` and both expose unprefixed `/health` and `/metrics`. This is harmless today (each runs on its own host/port) but becomes a real problem the moment two of them share one origin — see §14 for why the recommendation is subdomains, not a shared path space.

---

## 5. Dependency Inventory (per service)

```
Bluweb:
  FastAPI/uvicorn (async, single worker)
    -> SQLAlchemy 2.0 async -> Postgres (external, pre-provisioned, unified schema)
    -> MinIO (raw artifact bytes)
    -> Playwright/Chromium (browser-fallback fetch, up to 8 concurrent OS processes)
    -> torch (CPU) + spaCy + GLiNER/transformers (entity extraction, in-process)
    -> Crawlee (on-disk request-queue frontier, local `storage/` dir)
    -> Hugging Face Hub (one-time model download, HF_TOKEN)
    -> arbitrary public target websites (the thing being crawled)

OSINT:
  FastAPI/uvicorn (async)
    -> SQLAlchemy 2.0 -> SQLite (dev default) OR Postgres (prod, own container)
    -> app.worker_main (Postgres mode only, separate process, horizontally scalable)
    -> self-hosted SearxNG (native process or container, port 8890)
    -> Sherlock / Maigret / holehe (Python libraries, in-process/in-thread, NOT subprocess)
    -> nslookup / whois (real subprocess calls, OS binaries required: dnsutils, optional whois)
    -> Wikidata public REST API
    -> HIBP Pwned-Passwords k-anonymity API (only genuine 3rd-party API call in the whole service)

reddit_server:
  FastAPI/uvicorn (async, single worker mandatory)
    -> Reddit OAuth2 REST API (oauth.reddit.com) -- optional, needs client id/secret
    -> Reddit public RSS/Atom feeds (www.reddit.com/*.rss) -- needs no credentials
    -> (no DB, no cache service, no other dependency)

telegram_poc:
  FastAPI/uvicorn (async, single process mandatory) + APScheduler (same process)
    -> Telethon -> Telegram MTProto API (one authorized user-account session, on-disk session file)
    -> SQLAlchemy 2.0 async -> SQLite (local file, swappable to Postgres later, not done)
    -> local filesystem: session file, media cache, avatar cache, raw evidence JSON
```

### 5.1 Global Dependency Graph — what can share vs. what must isolate

**CAN SHARE freely (infrastructure layer, zero code impact):**
- The host OS and kernel.
- The Docker daemon.
- Nginx (single reverse-proxy/TLS-termination layer in front of all four).
- A common log-shipping/monitoring agent (reads stdout from each container — none of the four write meaningful log files themselves, see §10).
- NTP/time sync, base OS packages (`build-essential`, `dnsutils`, `curl`, etc. — install once at the host or shared-base-image level).

**MAY share, but only with care and only where each app already supports it:**
- A single physical Postgres **server** — technically possible since Bluweb needs external Postgres and OSINT can optionally use Postgres — **but only as separate logical databases/schemas with separate credentials**, never a shared database or shared schema (Bluweb's single unified table name `webintel_unified` and OSINT's investigation tables must never collide). Recommendation: keep them as two separate Postgres **instances** (simplest, matches "least invasive," avoids ever debugging one server's load spikes affecting the other) unless resource constraints force consolidation later.
- The Docker bridge network (all four containers can safely sit on one shared user-defined network — internal traffic between them is not needed, but sharing a network for nginx→backend routing is harmless since none of them make interservice calls today).

**MUST ISOLATE (proven necessary by the code, not assumed):**
- **Virtual environments / Python dependency sets** — Bluweb pins `torch`, `gliner`, `playwright`, `crawlee`, `scrapling`; OSINT pins `maigret`, `sherlock_project`, `holehe`; reddit_server and telegram_poc have much lighter, non-overlapping dependency sets. No shared venv is possible or desirable — each already has its own, keep it that way. (Containerizing each independently makes this automatic.)
- **Environment variables** — see §8; several services reuse generic names (`DATABASE_URL`, `HOST`/`PORT`, `LOG_LEVEL`) with **different meanings**. Never merge into one `.env`.
- **Credentials** — Bluweb's Postgres/MinIO creds, OSINT's (currently empty) secret surface, reddit_server's Reddit OAuth client secret + API_KEYS, telegram_poc's Telegram API hash + session file are all independent secrets with no legitimate reason to be shared.
- **Databases/schemas** — four independent data models; only OSINT and Bluweb use Postgres at all, and even then with completely different schemas (§3, §9).
- **Filesystem paths** — Bluweb's `storage/`, OSINT's `osint.db`/`data/`, telegram_poc's `data/` (containing the Telegram session — the single most sensitive file in this entire plan) must never share a mount point or volume.
- **Process/worker model** — OSINT's worker(s) can scale horizontally; telegram_poc **must never** run more than one replica (protocol-level constraint, not configurable away); reddit_server **must** stay single-replica for its in-process RSS cache/rate-limiter to mean anything; Bluweb is single-process by current design (its crawl/scheduler tasks are in-process asyncio, not distributed).

---

## 6. Port Matrix

| Service | Container-internal port | Host-exposed today (dev) | Recommended host/public exposure | Protocol | Process | Notes |
|---|---|---|---|---|---|---|
| Bluweb API | 8000 | 8000 (hardcoded) | **Not exposed to host at all** — reached only via Docker network as `bluweb:8000` from nginx | HTTP | uvicorn, 1 worker | Cannot change internal port without editing Dockerfile CMD — not required, since Docker network isolation makes the internal 8000 harmless |
| Bluweb MinIO | 9000 (API), 9001 (console) | 9000/9001 | Not exposed to host (internal `bluweb-minio:9000`) unless an operator needs console access, then bind to localhost only | HTTP | minio server | Hardcoded creds in Bluweb's own compose file — keep as-is inside the isolated network, do not expose port 9001 publicly |
| OSINT API | 8000 (configurable via `OSINT_PORT`) | 8000 | Not exposed to host — internal `osint:8000` | HTTP | uvicorn | |
| OSINT Postgres (if used) | 5432 | 5434 (already non-default, dev habit) | Not exposed to host — internal `osint-postgres:5432` | TCP | postgres:16-alpine | Already namespaced away from 5432 by the repo's own compose file — good practice already in place |
| OSINT SearxNG | 8890 | 8890 (native) | Not exposed to host — internal `osint-searxng:8080` (or whatever the container listens on) | HTTP | searxng | Needs its own container added to the unified compose (see §13) since OSINT's own compose file does not run it |
| reddit_server | 8000 (hardcoded in image) | 8000 | Not exposed to host — internal `reddit:8000` | HTTP | uvicorn, 1 worker (mandatory) | |
| telegram_poc | undecided (recommend fixing at 8000 inside its new container) | none today (no container) | Not exposed to host — internal `telegram:8000` | HTTP | uvicorn, exactly 1 replica (mandatory) | Must explicitly pass `--host 0.0.0.0 --port 8000` in the new Dockerfile CMD, since in-app `API_HOST`/`API_PORT` are dead settings |
| Nginx | 80, 443 | 80, 443 | **80/443 are the only ports reaching the internet** | HTTP/HTTPS | nginx | Single public entry point for all four services |

**Conflict found and resolved**: all four services independently default to port **8000**. Because each runs in its own Docker container with its own network namespace, this is a non-issue as long as **no service's port is published to the host** except through nginx — the "same port" fact only matters if you try to run them as bare processes on one host OS without containers, or if you publish (`ports:`) more than one of them to the same host port simultaneously. The plan in §13 avoids `ports:` publishing entirely for the four app containers; only nginx publishes 80/443.

---

## 7. Resource Matrix

| Service | Baseline RAM | Peak RAM | CPU profile | GPU | Disk growth | Concurrency notes |
|---|---|---|---|---|---|---|
| Bluweb | ~500MB–1GB (torch+transformers+spaCy loaded at first use) | 3–5GB+ under active crawling (up to 8 concurrent headless Chromium processes, ~150–300MB each) | Bursty, CPU-heavy during entity extraction and browser-fallback fetches; the clear resource bottleneck of the four | None (CPU-only torch build required — installing the default PyPI `torch` wheel pulls unwanted CUDA packages, must use the CPU-only index) | MinIO artifact storage grows unbounded with crawl volume — needs disk monitoring | Single uvicorn worker; internal caps: 10 concurrent monitoring jobs, 5 pages/job, 8 concurrent browser processes globally, DB pool 10+20 |
| OSINT | ~150–300MB per process (API + N in-process workers, or API + separate worker processes in Postgres mode) | Short CPU bursts during `username_lookup` (Sherlock+Maigret fan-out across hundreds of sites) and `email_lookup` (holehe, ~140 sites) | Moderate, spiky, I/O-bound (many concurrent outbound HTTP probes) rather than CPU-bound | None | SQLite file small; Postgres mode grows with investigation history, bounded by optional retention env vars | Horizontally scalable via `worker_main.py` in Postgres mode; per-process semaphore caps (`OSINT_SOURCE_MAX_CONCURRENCY`=3) and a global concurrent-investigation cap (default 10) |
| reddit_server | ~100–150MB | Low, flat | Low — pure I/O proxy, no CPU-heavy work | None | None (stateless) | Must stay single replica (in-process RSS cache/limiter) |
| telegram_poc | ~150–250MB | Higher briefly during media downloads/backfill (network + disk I/O, not CPU) | Low except during backfill/large media batches | None | `data/` grows with messages/media/raw-evidence JSON — **no retention policy exists today** (documented gap, SEC-05 in the repo's own audit) — needs disk monitoring or it will grow indefinitely | Must stay exactly one replica (Telegram session + in-process scheduler) |

**Bottleneck**: Bluweb, by a wide margin — it is the only one of the four with real CPU/RAM pressure (ML models + headless browser processes). If the shared server becomes resource-constrained, Bluweb is where to apply CPU/RAM limits or scale down concurrency (`crawl_max_concurrent_browser_global`, `scheduler_max_concurrent_jobs`) first.

**Starvation risks to guard against:**
- Bluweb's up-to-8 concurrent Chromium processes could starve the other three of CPU/RAM if unbounded at the container level — **recommend a hard memory limit** (e.g. Docker `mem_limit`/`deploy.resources.limits`) on the Bluweb container so a crawl spike can't OOM-kill sibling containers.
- OSINT's username-lookup fan-out opens many concurrent outbound connections — file-descriptor limits (`ulimit -n`) should be raised at the container level if this is heavily used alongside Bluweb's own many outbound crawl connections, so neither starves the other of ephemeral ports/FDs.
- telegram_poc's single-process model means a slow media download or backfill could delay the 4 in-process scheduler jobs (including the account's own connection-health check) — not a cross-service risk, but worth noting for that service's own health monitoring.

**Sizing recommendation**: minimum 4 vCPU / 8GB RAM for all four to coexist comfortably at light load; 8 vCPU / 16GB recommended if Bluweb crawls and OSINT investigations are expected to run concurrently at real volume. No GPU is needed or used by any of the four services.

---

## 8. Environment Variable Matrix

Each service already reads env vars under its own naming convention; the conflicts below only matter if someone tries to merge all four into **one** `.env` file — which this plan explicitly avoids (see §9/§13: one env file per service, injected only into its own container).

| Variable name | Used by | Meaning in that service | Collision risk if merged |
|---|---|---|---|
| `DATABASE_URL` | Bluweb (Postgres asyncpg URL, required) **and** telegram_poc (SQLite/Postgres URL, has a default) | Completely different databases, completely different values | **High** — same bare name, different services, would silently misconfigure one if ever combined into a shared file |
| `HOST` / `PORT` | reddit_server (bare names) | Bind address | Low risk of *value* confusion but the bare name is generic enough to warrant its own env file anyway |
| `API_HOST` / `API_PORT` | telegram_poc (currently dead/unused — see §3.4) | Intended bind address (not wired up) | N/A today, but don't let it collide once someone wires it up |
| `OSINT_HOST` / `OSINT_PORT` | OSINT | Bind address | Already prefixed — no collision |
| `LOG_LEVEL` | reddit_server, telegram_poc | Logging verbosity | Low risk (same semantics, different processes) but still keep separate |
| `API_KEYS` | reddit_server | Comma-separated valid `X-API-Key` values | Unique to this service |

**Recommendation**: one `.env` file per service — `.env.bluweb`, `.env.osint`, `.env.reddit`, `.env.telegram` — each injected only into its own container via that service's own `env_file:` entry in the unified compose file. This makes every naming collision above moot with zero code changes (each app only ever sees its own env file's contents inside its own container), and matches each app's existing `.env.example` 1:1.

### 8.1 Env var categorization (secret vs. not), per service

**Bluweb** — secrets: `DATABASE_URL` (contains credentials), `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `HF_TOKEN` (optional). Everything else (timeouts, page caps, concurrency caps, `CRAWLER_USER_AGENT`, `BLOCK_PRIVATE_NETWORKS`, `ALLOWED_SCHEMES`) is non-secret tuning/feature-flags.

**OSINT** — **no secrets exist in its env surface today** (by design — free/keyless sources only). All vars are tuning knobs, feature flags, or `OSINT_DATABASE_URL`/`OSINT_SEARXNG_URL` (URLs, not credentials, unless the DB URL itself embeds a Postgres password in prod mode — treat `OSINT_DATABASE_URL` as secret whenever it's a Postgres URL).

**reddit_server** — secrets: `API_KEYS`, `REDDIT_CLIENT_SECRET`, `REDDIT_PASSWORD` (optional), `REDDIT_RSS_FEED` (treated as secret even though not a login credential). `REDDIT_CLIENT_ID`/`REDDIT_USERNAME` are secret-adjacent (identify the account) but not secrets themselves.

**telegram_poc** — secrets: `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_PATH` (the file it points to is the real secret — a live, authorized session), `TELEGRAM_PHONE`/`TELEGRAM_API_ID` are secret-adjacent.

No real secret values were read or printed anywhere in this discovery process, per instructions.

---

## 9. Database / Storage Analysis

| | Bluweb | OSINT | reddit_server | telegram_poc |
|---|---|---|---|---|
| Engine | Postgres (external, pre-provisioned) | SQLite (default) or Postgres (prod, own container) | none | SQLite (local file) |
| Schema | Single unified table `webintel_unified` (STI via `record_kind`), applied manually from `docs/unified_schema.sql` — Alembic migrations present but explicitly obsolete, **never run them** | ~8 tables incl. `investigations`, dropped `api_keys`/`rate_limit_buckets` (removed by migration) | n/a | 11 tables incl. `telegram_sources`, `telegram_messages` (unique on source+message id), `backfill_jobs` |
| Migrations | Alembic present but deprecated/unused — schema is applied by hand | Alembic, actively used (`alembic upgrade head` is a required deploy step in Postgres mode) | n/a | No Alembic — `Base.metadata.create_all` + a hand-rolled additive-column patcher (SQLite-only, explicitly not migration-grade) |
| Data ownership | Whatever Bluweb crawls — no other service reads/writes this DB | Whatever OSINT investigates — no other service reads/writes this DB | n/a | Whatever this account's Telegram session collects — no other service reads/writes this DB |
| Cross-service read risk | **None found** — no shared connection strings, no shared schema, no code path in any of the four services that opens another service's database | | | |
| Credentials shared? | No — each service's DB credentials are its own, defined in its own `.env` | | | |
| Tenant/owner identification | Not applicable — none of these four services are multi-tenant; each is a single-operator backend | | | |

**Priority preserved, as required**: existing data model → preserved unchanged → isolated at the deployment/infrastructure level (separate containers, separate volumes, separate credentials) rather than touched at the application level.

---

## 10. Redis / Cache Analysis

**No service uses Redis.** Confirmed by direct code/dependency inspection of all four repos — this was the single biggest assumption this plan could have gotten wrong, and it did not need correcting: OSINT uses Postgres `SELECT ... FOR UPDATE SKIP LOCKED` instead of a job queue; reddit_server and telegram_poc use purely **in-process, per-replica** memory caches (`FeedCache`, `TokenBucket`, `InFlightDeduplicator`) that are explicitly documented in their own code as not-distributed and not-meant-to-be-shared; Bluweb has only aspirational comments about a future Redis-backed event bus that was never implemented.

**Consequence for this plan**: there is no Redis instance to provision, size, or namespace at all. If a future service needs Redis, it can be added in complete isolation without touching any of these four.

---

## 11. External API Analysis

| Service | External dependency | Auth needed | Failure mode if unreachable |
|---|---|---|---|
| Bluweb | Arbitrary target websites (the crawl targets), Hugging Face Hub (one-time model pull), local Playwright/Chromium | `HF_TOKEN` optional | Per-page fetch failures are captured/classified, not fatal to the process; HF pull failure disables GLiNER extraction gracefully (logs a warning) |
| OSINT | Self-hosted SearxNG, Wikidata public API, HIBP Pwned-Passwords API, ~140 sites (holehe), hundreds of sites (Sherlock/Maigret), `nslookup`/`whois` OS binaries | None (all free/keyless) | `public_web`/`/api/v1/search` degrade to "unavailable" without SearxNG; individual adapter failures are captured per-source, don't crash the investigation |
| reddit_server | `oauth.reddit.com` (needs client id/secret) and `www.reddit.com/*.rss` (no credentials needed) | Reddit OAuth2 app credentials (OAuth path only) | Non-RSS routes return `503 REDDIT_NOT_CONFIGURED` if OAuth creds missing; RSS routes work with zero external credentials |
| telegram_poc | Telegram MTProto API via one authorized user-account session | Telegram API ID/hash + one-time interactive login (send-code/verify-code/verify-password) | `SESSION_INVALID_ERRORS` latch short-circuits calls once the session goes bad, requiring re-login through the same 3-step handshake |

No LLM APIs, no paid third-party data providers, and no webhooks/callbacks are used by any of the four services today.

---

## 12. Current Process / Startup Analysis

| Service | Startup command today | Where it's defined |
|---|---|---|
| Bluweb | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | Dockerfile CMD, docker-compose command, README manual step |
| OSINT (API) | `python -m app.main` (reads `OSINT_HOST`/`OSINT_PORT`) or `uvicorn app.main:app --host 0.0.0.0 --port 8000` (docker-compose) | `app/main.py:159-162`, docker-compose.yml |
| OSINT (worker) | `python -m app.worker_main` — Postgres mode only | docker-compose.yml `worker` service |
| reddit_server | `uvicorn app.main:app --host 0.0.0.0 --port 8000` (Docker; ignores `PORT` env) or `python run.py` (local dev; honors `PORT`/`--reload`/`--workers`) | Dockerfile CMD vs. `run.py` |
| telegram_poc | `uvicorn app.main:app --reload` (dev only — no production command documented anywhere) | README §19 |

---

## 13. Proposed Single-Server Architecture

```
                              INTERNET
                                 |
                                 v
                     +-----------------------+
                     |    NGINX (nginx)      |
                     |  reverse proxy + TLS   |
                     |  listens :80 / :443    |
                     +-----------------------+
                       |     |      |      |
           bluweb.<dom>|osint.<dom>|reddit.<dom>|telegram.<dom>
                       v     v      v      v
                 +--------+ +------+ +------+ +----------+
                 | bluweb | |osint | |reddit| | telegram |
                 | :8000  | |:8000 | |:8000 | |  :8000   |
                 +--------+ +------+ +------+ +----------+
                     |          |                    |
                     v          v                    v
              +-----------+ +-----------+     (own SQLite file,
              |  bluweb-  | |  osint-   |      own data/ volume)
              |  minio    | | postgres  |
              |:9000/9001 | |  :5432    |
              +-----------+ +-----------+
                             +-----------+
                             |  osint-   |
                             |  worker   |  (Postgres mode only, scalable)
                             +-----------+
                             +-----------+
                             |  osint-   |
                             | searxng   |
                             |  :8080    |
                             +-----------+
```

All backend containers sit on **one shared user-defined Docker bridge network** (`saga-net`), reachable from nginx by container name (`bluweb:8000`, `osint:8000`, `reddit:8000`, `telegram:8000`). **None of the four app containers publish a host port** — nginx is the only container with a `ports:` mapping to the host (`80:80`, `443:443`). This resolves the all-four-default-to-8000 conflict for free: each container's internal 8000 lives in its own network namespace and is never exposed to the host directly.

This is a **new, top-level `docker-compose.yml`** at `~/Saga-APIs/docker-compose.yml` (does not exist yet — not created as part of this planning pass, per instructions). It reuses each service's **existing Dockerfile as-is** via `build: { context: ./Bluweb }` etc. (Bluweb, OSINT, reddit_server already have one; telegram_poc needs a new Dockerfile written — packaging only, no application code change). Each of the four existing per-service `docker-compose.yml` files is left untouched and still usable by a developer working on just one service in isolation.

---

## 14. Reverse Proxy Strategy

**Recommendation: subdomains, not path prefixes.** Reasoning, grounded in the actual code:

1. **Bluweb and OSINT both use the identical base path `/api/v1/*`** and both expose unprefixed `/health` and `/metrics`. Under one shared origin with path-based routing (`/bluweb/api/v1/...` vs `/osint/api/v1/...`), nginx would need to rewrite/strip a prefix on every request — technically possible (`location /bluweb/ { proxy_pass http://bluweb:8000/; }`) but fragile: Bluweb's `GET /` → `/docs` redirect, and every service's own Swagger UI (`/docs`, `/openapi.json`) generates internal links assuming it owns the root of its origin. Subdomains sidestep this entirely — each service keeps believing it owns `/`.
2. **None of the four services currently share a public domain.** Per Bluweb's own `integration.md`, its existing deployed caller reaches it directly via `http://<ec2-ip>:8000` — there is no existing client hardcoding a shared-domain path prefix to preserve. Moving to a single server changes the host either way (new IP, or a new shared domain); subdomains are the smallest possible additional change on top of that unavoidable one, and they fully preserve every service's existing path structure (`/api/v1/...`, `/api/reddit/...`, `/api/telegram/...`) byte-for-byte.
3. If a wildcard/subdomain DNS setup is not available (single apex domain only), the fallback is path-based routing with `/bluweb/`, `/osint/`, `/reddit/`, `/telegram/` prefixes and nginx-level prefix stripping — acceptable, but expect to also patch `proxy_redirect` for Bluweb's root redirect and expect `/docs`/`/openapi.json` under each service to reference the wrong base path unless nginx also rewrites `Location` headers and the OpenAPI `servers` block. This is **strictly more fragile** than subdomains for zero benefit, so it is the fallback, not the default.

Recommended subdomains (placeholder domain — replace `<domain>` with whatever the operator owns):
```
bluweb.<domain>    -> bluweb:8000    (preserves /api/v1/*, /health*, /metrics unchanged)
osint.<domain>     -> osint:8000     (preserves /api/v1/*, /health, /ready, /metrics unchanged)
reddit.<domain>    -> reddit:8000    (preserves /api/reddit/* unchanged)
telegram.<domain>  -> telegram:8000  (preserves /api/telegram/*, /api/sources/*, /console unchanged)
```

**Reverse-proxy-specific configuration needed per service** (all infrastructure-level, zero app code changes):
- **Bluweb**: `POST /api/v1/search/instant` can block up to 30s server-side — set `proxy_read_timeout` ≥ 35s for this path (or the whole `bluweb` upstream, simplest). No streaming/websocket handling needed.
- **OSINT**: `POST /api/v1/username/lookup` is synchronous and documented as able to take "minutes" — set a generous `proxy_read_timeout` (e.g. 300s) for this upstream, or all of it.
- **reddit_server**: DEPLOYMENT.md itself flags that `proxy_read_timeout` should exceed `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS` (default 55s) or callers get a proxy-level 504 instead of the service's own structured timeout error — set ≥60s. Also note it does **not** parse `X-Forwarded-For`, so its own per-caller RSS rate limiter will bucket every caller behind the proxy as one client unless nginx is also configured to pass real client IPs and the app is *not* modified to read them — this is a known, accepted limitation to document, not a blocker (flagged as a PROBLEM below, §26 "Open Questions" / risk list).
- **telegram_poc**: media-download endpoints return raw binary (photos, files) — no special buffering config needed beyond normal proxy defaults, but do raise `client_max_body_size` if inbound media *uploads* were ever added (not currently possible — this service only downloads, never accepts uploads, confirmed by discovery). `/console` must be proxied from the **same origin** as the API routes (already the plan, since it's all one subdomain) — its own JS defaults to `location.origin`, so this works with zero changes.

**Auth gap enforcement at the proxy layer** (see §16): since Bluweb, OSINT, and telegram_poc ship **no application-level authentication**, this is the layer where access control must live if these subdomains are ever reachable beyond a trusted network — e.g. nginx `allow`/`deny` IP lists, HTTP Basic Auth, mTLS, or a VPN/tunnel requirement in front of the whole reverse proxy. This is a **required infrastructure control**, not optional hardening, given the current code.

---

## 15. Process / Container Strategy

| Service | Strategy | Reasoning |
|---|---|---|
| Bluweb | Docker container, reuse existing `Dockerfile`, 1 replica | Already containerized; single-process design is intentional (README documents why no separate worker exists yet) |
| OSINT | Docker container(s): `osint` (API) + `osint-worker` (Postgres mode, scalable) + `osint-postgres` + `osint-searxng` | Already containerized for API/Postgres/worker; SearxNG needs adding as its own container since OSINT's own compose file doesn't run it (it's expected to be started separately today) |
| reddit_server | Docker container, reuse existing `Dockerfile`, **exactly 1 replica** | Already containerized; more than 1 replica silently multiplies its in-process RSS cache/rate-limiter budget — a real correctness issue, not just a preference |
| telegram_poc | Docker container, **new Dockerfile needed**, **exactly 1 replica, ever** | No existing containerization; single-replica is a hard protocol-level constraint (one Telegram session = one process, full stop) |
| Nginx | Docker container, new minimal config | Common reverse-proxy/TLS layer for all four |

Each service is **independently restartable** by construction of using separate containers on separate compose service entries — `docker compose restart bluweb` never touches `osint`/`reddit`/`telegram`, satisfying Rule 4 directly.

### 15.1 telegram_poc's new Dockerfile — what it must contain (design only, not written yet)
- Base image matching what's already proven to work for the other three: `python:3.12-slim` (matches Bluweb/OSINT/reddit_server's own Dockerfiles, keeping runtime versions consistent across the fleet even though nothing in telegram_poc's code strictly requires 3.12 over 3.10/3.11).
- `pip install -r requirements.txt` (no OS-level packages beyond Python needed — confirmed no Tesseract/ffmpeg/etc. dependency exists).
- `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]` — **must be explicit**, since the in-app `API_HOST`/`API_PORT` settings are dead and do nothing on their own.
- A volume mount for `./telegram_poc/data:/app/data` (or equivalent) so the session file, SQLite DB, and media caches survive container restarts/recreation — this is the single most important volume in the entire plan, since the session file cannot simply be regenerated without another full login handshake.
- `deploy.replicas: 1` (or simply never scale it) with no `restart` policy that could race two instances during a brief overlap — recommend `restart: unless-stopped` (safe, since Docker Compose never runs two copies of the same service unless explicitly scaled) rather than anything HA/failover-oriented, given the single-writer constraint.

---

## 16. Network / Security Strategy

```
Internet
   |
   +--> 80/443 --> Nginx (TLS termination, the ONLY publicly reachable ports)
                  |
                  +--> bluweb:8000    (internal only, saga-net)
                  +--> osint:8000     (internal only, saga-net)
                  +--> reddit:8000    (internal only, saga-net)
                  +--> telegram:8000  (internal only, saga-net)
```

- **Inbound**: only 80/443 from the internet, to the nginx container. Everything else stays on the internal Docker network (`saga-net`), unreachable from outside the host even if a firewall rule is misconfigured, since Docker's own network isolation is the first layer of defense.
- **Outbound requirements** (per service, all normal egress, nothing unusual): Bluweb → arbitrary websites + Hugging Face Hub; OSINT → its own SearxNG container + Wikidata + HIBP + the sites Sherlock/Maigret/holehe probe; reddit_server → `oauth.reddit.com`/`www.reddit.com`; telegram_poc → Telegram's MTProto endpoints + (its console's static assets) Google Fonts CDN.
- **TLS**: terminated once, at nginx, for all four subdomains (a single wildcard cert, e.g. via Let's Encrypt, covers all of them if using the subdomain scheme). None of the four backend services implement their own TLS — this is expected and fine, since they're never directly reachable from outside the Docker network.
- **Trusted proxy headers**: nginx should set `X-Forwarded-For`/`X-Forwarded-Proto` on every upstream request as good practice, but note reddit_server's own rate limiter does **not** read `X-Forwarded-For` today (uses the raw TCP peer) — this is a known, accepted limitation of that service as shipped, not something to patch here.
- **CORS**: none of the four services configure CORS middleware today. Only relevant if a browser-based frontend on a *different* origin needs to call one of these APIs directly — not currently the case for any of them (telegram_poc's `/console` is same-origin by design specifically to avoid needing CORS).
- **Request size limits**: none of the four accept file uploads from callers (confirmed — telegram_poc only downloads media, never accepts uploads despite `python-multipart` being present as a transitive FastAPI dependency). Default nginx `client_max_body_size` is fine; no special tuning needed.
- **Timeouts**: see the per-service notes in §14 (Bluweb 35s, OSINT 300s for username lookups, reddit_server ≥60s). Recommend setting these per-`location` or per-`upstream` block in nginx rather than one global value, since the services' own tolerance for slow requests varies by an order of magnitude.
- **Auth gap**: reiterating from §14 — Bluweb, OSINT, and telegram_poc have **zero** application-level authentication. If this server is ever reachable beyond a fully trusted private network, nginx-level access control (Basic Auth, IP allowlist, mTLS, or a VPN requirement) is **mandatory**, not optional, for those three subdomains. reddit_server's `X-API-Key` should also be **set** (currently optional/disabled-by-default) before going live.

---

## 17. Data Isolation Strategy

Answered per the Phase 8 checklist, per service:

| Question | Bluweb | OSINT | reddit_server | telegram_poc |
|---|---|---|---|---|
| Is storage shared? | No — own Postgres (external) + own MinIO bucket | No — own SQLite file or own Postgres container | N/A (stateless) | No — own SQLite file |
| Service-specific? | Yes | Yes | N/A | Yes |
| Tenant-specific? | No (single-operator, no multi-tenancy) | No | N/A | No |
| Owner identifier | N/A — no tenant model in any of the four | | | |
| Could one service read another's data? | **No** — no shared connection strings, no shared schema/table names, no shared filesystem mount found in any of the four repos | | | |
| Credentials shared? | No — each has its own `.env` | | | |
| Schemas separated? | Yes (Bluweb's single unified table is its own; OSINT's tables are its own) | | N/A | Yes |
| Redis keys namespaced? | N/A — no Redis anywhere | | | |
| Filesystem paths isolated? | Yes, by container/volume separation in the proposed architecture | | | Yes — `data/` volume is exclusively telegram_poc's |

**Priority followed**: existing data model → preserved → isolated at the deployment level (separate containers/volumes/credentials), exactly as instructed. No application-level data changes are proposed anywhere in this plan.

---

## 18. Logging / Monitoring Strategy

| Service | Current logging | Notes |
|---|---|---|
| Bluweb | Structured logs to **stdout only** (`app/core/logging.py`, `logging.StreamHandler()`) — no log files written | Clean fit for `docker logs`/log-shipping agents |
| OSINT | Structured JSON logs to stdout (`app/logging_config.py`) — the stray root-level `*.log` files (`api_local.log`, `worker_a.log`, etc.) are **dev artifacts from manual shell redirection during local testing**, not something the app itself produces; they should not be copied into the deployment | Same — stdout-native |
| reddit_server | Structured logs to stdout, `LOG_JSON` env flag for JSON formatting | Same |
| telegram_poc | Plain `logging.basicConfig` to stdout/stderr — **no log file path configured anywhere in the app** | Same — must capture via `docker logs`/journald, nothing else exists |

**Recommendation**: all four are already stdout-native — no in-app logging changes needed. Use `docker compose logs -f <service>` or point a log-shipping agent (e.g. `docker logs` driver → Loki/CloudWatch/whatever the operator already runs) at each container; tag each stream by its compose service name (`bluweb`, `osint`, `osint-worker`, `reddit`, `telegram`, `nginx`) so origin is always traceable without any code change. Configure Docker's `json-file` log driver with `max-size`/`max-file` rotation limits (or switch to a `local` driver) so container logs don't grow unbounded on disk — none of the four apps rotate their own stdout, so the container runtime must.

**Monitoring recommendations** (not implemented as part of this plan, per instructions): container-level health/restart-count monitoring (`docker inspect` health status, restart count deltas), disk usage alerts on the MinIO volume (Bluweb) and the telegram_poc `data/` volume (both grow unboundedly with no retention policy today), RAM/CPU alerts scoped per-container so a Bluweb crawl spike is visible independently of the other three.

---

## 19. Health-Check Strategy

| Service | Liveness | Readiness | Notes |
|---|---|---|---|
| Bluweb | `GET /health` or `/health/live` (no dependency check) | `GET /health/ready` (checks Postgres `SELECT 1`) | Use `/health/ready` for the Docker `HEALTHCHECK`/orchestrator readiness gate |
| OSINT | `GET /health` (no dependency check) | `GET /ready` (checks DB `SELECT 1`) | `GET /api/v1/sources/health` is a richer per-adapter status page, useful for on-demand diagnostics, not a probe |
| reddit_server | `GET /health` (no dependency check, never fails) | `GET /ready` (config-presence check only, **no network call**) | **Never** wire `GET /api/reddit/status` into a liveness/readiness probe — it makes a live Reddit call and would flap on any transient Reddit outage (explicitly warned against in the service's own DEPLOYMENT.md) |
| telegram_poc | `GET /health` (no dependency check) | `GET /ready` (checks whether Telegram credentials are *configured*, **not** whether the session is actually connected/authorized) | For a stricter readiness signal, an operator could additionally poll `GET /api/telegram/status`, but that makes a real Telegram call — use sparingly, e.g. only on a slow interval, not as a fast liveness probe |

**Failure isolation, as instructed:**
- If reddit_server fails → OSINT, Bluweb, telegram_poc are unaffected (separate containers, no shared dependency).
- If telegram_poc fails → Bluweb is unaffected (no shared dependency); confirmed no code path in either service references the other.
- If one container crashes → only that container restarts (`restart: unless-stopped` per-service in Compose; no shared process to bring down siblings).
- If nginx restarts → all four app containers keep running underneath it (Compose containers are independent of the proxy's lifecycle); requests just queue/fail briefly at the edge until nginx comes back.
- If OSINT's SearxNG dependency goes down → OSINT's `public_web` adapter and `/api/v1/search` degrade gracefully to "unavailable" per-source, the rest of OSINT keeps working (confirmed graceful-degradation behavior in the code, not assumed).
- If Bluweb's MinIO goes down → new crawls would fail to persist artifacts (a real dependency, not gracefully degraded per the discovery) — treat MinIO's own container health as load-bearing for Bluweb specifically.

---

## 20. Migration Steps (zero/minimal downtime)

1. **Prepare the server**: install Docker + Docker Compose plugin; open only 80/443 in the host firewall/security group; provision DNS (subdomains or apex, per §14's choice) pointing at the new host.
2. **Install prerequisites**: nothing beyond Docker itself — no Python/Node needs installing on the bare host, since everything runs in containers.
3. **Copy/clone repositories**: `git clone` (or copy) all four existing repos into the new host under `~/Saga-APIs/{Bluweb,OSINT,reddit_server,telegram_poc}`, unchanged.
4. **Configure environments**: create `.env.bluweb`, `.env.osint`, `.env.reddit`, `.env.telegram` from each service's own `.env.example`, filling in real values (Postgres creds, MinIO creds, Reddit OAuth creds, Telegram API id/hash) — never merge these into one file (§8).
5. **Build services**: `docker compose build` from the new top-level compose file (once it exists) — reuses each service's existing Dockerfile; write the one new Dockerfile for telegram_poc first (§15.1).
6. **Start services individually**, one at a time, verifying each in isolation before moving to the next: `docker compose up -d bluweb-minio bluweb`, confirm `GET http://localhost:<temp-port>/health` and `/health/ready` (temporarily publish a host port for this verification step only, then remove it once nginx is wired up) — repeat for `osint-postgres`+`osint-searxng`+`osint`+`osint-worker`, then `reddit`, then `telegram`.
7. **Test each service directly** (still via a temporary host-port publish or `docker compose exec <svc> curl localhost:8000/...`): hit each service's health endpoint, then a representative real endpoint (e.g. Bluweb `POST /api/v1/preflight` against a known-safe test URL; OSINT `POST /api/v1/phone/lookup`; reddit_server `GET /health`+one RSS endpoint; telegram_poc `GET /health` and, once the operator completes the interactive login handshake once, `GET /api/telegram/status`).
8. **Configure the reverse proxy**: write the nginx config (per §14), remove the temporary host-port publishes from step 6/7, restart the stack so only nginx is host-exposed.
9. **Test through nginx**: repeat step 7's checks through the public subdomains/paths instead of temporary ports, confirming TLS, correct upstream routing, and the per-service timeout overrides (§14) actually apply (e.g. deliberately trigger OSINT's slow username-lookup path and confirm it doesn't 504 early).
10. **Verify existing API behavior** against each service's own existing integration docs (Bluweb's `integration.md`, OSINT's `docs/ENDPOINTS.md`, reddit_server's `INTEGRATION.md`) — confirm every documented endpoint still returns the same shape it did on the old standalone deployment.
11. **Verify external integrations**: confirm Bluweb can reach Hugging Face Hub and arbitrary test URLs; OSINT can reach its own SearxNG container, Wikidata, HIBP; reddit_server can reach `oauth.reddit.com`/`www.reddit.com`; telegram_poc can reach Telegram's MTProto endpoints and complete/resume its session.
12. **Verify authentication**: confirm reddit_server's `X-API-Key` is enforced (test both a valid and an invalid key); confirm nginx-level access control is actually in front of Bluweb/OSINT/telegram_poc if this host is internet-reachable (§16).
13. **Verify databases**: confirm Bluweb reaches its external Postgres and the `webintel_unified` table exists (schema was applied manually beforehand — this plan does not run migrations for Bluweb, per its own documented policy); confirm OSINT's `alembic upgrade head` ran successfully against its own Postgres (or that SQLite mode initializes cleanly); confirm telegram_poc's SQLite file and additive-column patcher ran without error.
14. **Verify background jobs**: confirm Bluweb's in-process monitoring scheduler is polling (check logs for scheduler activity); confirm OSINT's worker process(es) are claiming queued investigations (`SELECT ... FOR UPDATE SKIP LOCKED` activity in Postgres mode); confirm telegram_poc's 4 APScheduler jobs are running on their configured cadence (check logs).
15. **Verify logs**: confirm `docker compose logs <service>` shows activity for all six containers (four apps + nginx + whichever DB/worker containers apply), correctly attributable to each service name.
16. **Verify restart behavior**: `docker compose restart bluweb` and confirm the other three containers are untouched and continue serving requests throughout.
17. **Verify failure isolation**: deliberately stop one container (e.g. `docker compose stop reddit`) and confirm the other three keep responding normally through nginx.
18. **Only then switch production traffic** — repoint DNS (or remove any temporary redirect) from the old standalone deployments to the new shared host, one service at a time if the DNS/subdomain scheme allows it, watching each service's logs/health during the cutover window.

---

## 21. Testing Plan

- **Per-service unit/integration test suites already exist** for all four (`tests/` directories with substantial coverage per the discovery reports) — run `pytest` inside each service's own container/venv as a pre-deployment gate; these are unaffected by this infrastructure work since no application code changes.
- **Smoke test matrix**: one golden-path request per service through the final nginx-fronted URL (Bluweb preflight → crawl → document fetch; OSINT phone/email/username lookup + one full investigation; reddit_server one OAuth-path and one RSS-path request; telegram_poc `/health`+`/ready` and, post-login, one `/api/telegram/status` call).
- **Negative/edge tests specific to this migration**: confirm a request to `bluweb.<domain>/health` does **not** accidentally reach OSINT (path-collision regression check, given §4's finding that both use `/api/v1` and `/health`) — this is exactly what the subdomain-based routing in §14 is designed to prevent; test it explicitly rather than assuming the config is correct.
- **Timeout tests**: confirm OSINT's slow username-lookup path and Bluweb's `search/instant` don't 504 early through nginx (§14/§16 timeout overrides).
- **Failure-isolation tests**: per §20 steps 16-17 — restart one container, stop one container, confirm the other three are unaffected each time.
- **Auth tests**: confirm reddit_server rejects a missing/invalid `X-API-Key` once enabled; confirm whatever nginx-level access control is chosen for Bluweb/OSINT/telegram_poc actually blocks an unauthenticated request from outside the trusted network.

---

## 22. Rollback Plan

**Before migration, back up:**
- Bluweb's external Postgres database (a standard `pg_dump` of whatever pre-existing instance it points to) — this plan does not touch that database's provisioning, but back it up anyway before any cutover.
- OSINT's `osint.db` (if migrating a dev/SQLite instance) or its Postgres data volume, plus its `.env` (non-secret values only need documenting; secret values should already be in a password manager, not this report).
- telegram_poc's entire `data/` directory — **this is the most critical backup in the entire plan**, since the Telegram session file cannot be regenerated without a full interactive re-login (send-code/verify-code/verify-password), and re-logging in from a fresh session while an old copy might still exist elsewhere risks Telegram treating it as a second device and revoking one of them.
- Each service's `.env` file (or the secrets-manager entries backing it).

**If the consolidated deployment fails:**
- The four original standalone deployments (wherever they currently run — e.g. Bluweb's existing EC2 instance per `integration.md`) are **left running and untouched** throughout migration steps 1-17 of §20 — traffic is only cut over in step 18, and only after every verification step passes. This means rollback is simply: **don't complete step 18** (or revert the DNS/routing change back to the old hosts if it was already flipped).
- No database is migrated or altered in place by this plan for any of the four services — each keeps using its own existing database (Bluweb's external Postgres unchanged; OSINT's SQLite/Postgres unchanged; telegram_poc's SQLite unchanged) — so there is no in-place schema change to roll back.
- Credentials are never overwritten — each service gets its own new `.env.*` file on the new host, copied from (not replacing) whatever configuration the old standalone deployment used.
- Individual services are independently recoverable: if only one of the four fails during cutover, the other three can proceed while the failing one's traffic stays pointed at (or reverts to) its old standalone location.

---

## 23. Risks

1. **No application-level auth on 3 of 4 services** (Bluweb, OSINT, telegram_poc) — if the nginx-level access control in §16 is misconfigured or skipped, all three become fully open to anyone who can reach the host. *Mitigation*: treat the nginx auth/ACL layer as a hard launch blocker, test it explicitly (§21).
2. **telegram_poc's Telegram session is a single, non-shardable credential** — any accidental double-run (e.g. a botched blue/green deploy that briefly runs two copies) risks Telegram revoking the session or flagging the account. *Mitigation*: enforce `replicas: 1` at the compose level and never attempt rolling/parallel deploys for this specific service.
3. **reddit_server's in-process RSS cache/rate-limiter silently degrades if run as >1 replica** — no error is thrown, it just quietly stops sharing state correctly. *Mitigation*: same as above, pin to 1 replica, document why in the compose file itself.
4. **Bluweb is CPU/RAM-heavy and could starve the other three under load** if no container resource limits are set. *Mitigation*: set explicit `mem_limit`/CPU shares on the Bluweb container (§7).
5. **Bluweb and OSINT share an identical `/api/v1` path shape** — a future path-based-routing change (departing from this plan's subdomain recommendation) would silently misroute requests between them if not carefully rewritten. *Mitigation*: this plan's explicit recommendation against path-based routing (§14).
6. **telegram_poc and Bluweb's data volumes have no retention/cleanup policy today** (documented gaps in each service's own code/audit) — disk usage will grow unbounded over time. *Mitigation*: monitor disk usage (§18); not fixed here since it would require an application-level change, out of scope for this plan.
7. **Two known silent-failure bugs already exist in telegram_poc** (`PATCH /api/sources/{id}` and `POST /api/notifications/{id}/read` return 200 but don't commit) — unrelated to this migration, but worth flagging so the deployment isn't blamed for pre-existing app behavior during post-migration verification.
8. **OSINT's SearxNG dependency has no compose service defined in OSINT's own repo** — it must be added fresh in the unified compose file; if misconfigured, OSINT's search-related features silently degrade (not fail loudly) making the misconfiguration easy to miss during testing.

---

## 24. Open Questions

1. Does the operator have a real domain with subdomain capability (for §14's recommended routing), or only a single apex domain/IP (forcing the path-prefix fallback)?
2. What access-control mechanism is preferred for the three unauthenticated services at the nginx layer — IP allowlist (simplest, if all legitimate callers have static IPs), HTTP Basic Auth, mTLS, or a VPN/tunnel requirement in front of the whole host?
3. Should OSINT run in SQLite mode (simpler, single-process, matches its current likely dev usage) or Postgres mode (horizontally scalable workers) on the shared server? This plan supports either with no code changes, but the compose file's shape differs slightly.
4. Is there a retention/rotation policy the operator wants for Bluweb's MinIO artifacts and telegram_poc's collected media/messages, given neither has one today? (Out of scope to *implement* here, but worth deciding before disk fills up.)
5. Should Bluweb's and OSINT's Postgres usage be consolidated onto one shared Postgres **server** (separate databases/credentials) to reduce operational overhead, or kept as two fully separate Postgres instances (simpler mental model, zero blast-radius sharing)? §5.1 recommends the latter as the default; revisit only if resource constraints demand otherwise.
6. Has the telegram_poc operator already completed the interactive Telegram login once (producing a session file) that can be copied to the new host, or will a fresh login need to be performed on the new host as part of migration step 6/7?

---

## 25. Files That Would Need Modification

**None of the four existing application codebases need any modification for this plan.** The only new files this plan calls for creating (not created during this discovery/planning pass, per instructions) are:

- `~/Saga-APIs/docker-compose.yml` — new, top-level, unifies all four services + nginx on one network.
- `~/Saga-APIs/telegram_poc/Dockerfile` — new, packaging-only (§15.1), no changes to any `.py` file inside `telegram_poc`.
- `~/Saga-APIs/nginx/nginx.conf` (or equivalent, path/name at the operator's discretion) — new, reverse-proxy + TLS + per-service timeout config (§14/§16).
- `~/Saga-APIs/.env.bluweb`, `.env.osint`, `.env.reddit`, `.env.telegram` — new, populated from each service's own existing `.env.example`, never merged (§8).

## 26. Files That Must NOT Be Modified

- Every `.py` file in all four services.
- Every existing per-service `Dockerfile` (Bluweb, OSINT, reddit_server) — reused as-is via `build:` in the new top-level compose file.
- Every existing per-service `docker-compose.yml` — left in place for standalone single-service development, not deleted or edited.
- Every existing `alembic`/migration file in Bluweb and OSINT — in particular, never run `alembic upgrade head` for Bluweb (explicitly deprecated per its own docs).
- Every existing `.env.example` in all four services (used only as a template to generate the new isolated `.env.*` files, not edited itself).
- `docs/unified_schema.sql` (Bluweb) — the schema is applied once, out of band, exactly as the existing process already does; this plan doesn't change that process.

## 27. Exact Implementation Sequence

1. Decide the two open questions that block config (§24 items 1-2: domain/subdomain availability, and the access-control mechanism for the three unauthenticated services).
2. Write `telegram_poc/Dockerfile` per §15.1.
3. Write the four isolated `.env.*` files from each service's `.env.example`, filled with real (previously-provisioned or newly-created) credentials.
4. Write the new top-level `~/Saga-APIs/docker-compose.yml` per §13/§15, including the new `osint-searxng` service (not present in OSINT's own compose file) and explicit `mem_limit` on `bluweb` (§7/§23 item 4).
5. Write the nginx config per §14/§16 (subdomain vhosts, per-service timeout overrides, TLS, the chosen access-control mechanism for Bluweb/OSINT/telegram_poc).
6. Execute Migration Steps §20 items 1-9 (server prep through "test through nginx"), stopping to fix and re-verify at any failed check before proceeding.
7. Execute the verification steps §20 items 10-17.
8. Execute the Testing Plan §21 in full against the nginx-fronted URLs.
9. Cut over production traffic (§20 item 18), keeping the old standalone deployments live and unmodified until cutover is confirmed stable.
10. Decommission the old standalone deployments only after an agreed observation window with no incidents, per the operator's own change-management policy (not specified in this plan).

---

## RECOMMENDED FINAL ARCHITECTURE

One server. One Docker daemon. One shared internal network (`saga-net`). One public entry point.

- **Nginx** is the only container publishing ports to the internet (80/443, TLS-terminated), routing by subdomain to four backend containers it reaches purely by Docker service name.
- **Bluweb**, **OSINT**, **reddit_server**, **telegram_poc** each run in their own container, built from their own (mostly already-existing) Dockerfile, with their own `.env` file, their own data volume, and their own restart lifecycle — **none of them are merged, none of them share a database, none of them share Redis (none use it), and none of them are reachable from the internet except through nginx.**
- OSINT additionally gets its own Postgres + SearxNG + (optionally scalable) worker containers; Bluweb additionally gets its own MinIO container; reddit_server and telegram_poc need nothing beyond their own single container. telegram_poc is constrained to exactly one replica by Telegram's own protocol, and reddit_server is constrained to exactly one replica by its own in-process rate-limiter/cache design — both documented, both enforced simply by never scaling those two compose services.
- Every one of the four services' existing endpoints, response shapes, database schemas, and startup commands are **preserved exactly as they exist today** — the only things introduced are the reverse proxy in front of them, the shared network connecting them, and (for telegram_poc only) a new Dockerfile that runs its existing, unmodified `app/main.py` the same way `uvicorn app.main:app --reload` already does today, just with an explicit host/port and inside a container instead of a bare terminal.
