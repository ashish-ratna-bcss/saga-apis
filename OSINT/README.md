# India OSINT — Phase 1

Free-only, API-only, public-source intelligence platform:

```
identifier -> automated investigation -> discovery -> automatic pivots ->
entity resolution -> relationships -> evidence -> confidence -> JSON API
```

No UI. Swagger/OpenAPI (`/docs`) is the documentation; other applications
consume this over REST/JSON without knowing which underlying tool (holehe,
sherlock, SearxNG...) produced any given fact.

Zero paid API dependency. Every source is a local library, an open-source
tool, a free keyless API, or a self-hosted open-source service.

**Not a subscriber/identity lookup.** A phone number resolving to "Rahul Kumar 82%"
means Rahul Kumar's name co-occurs with that number on public sources with that
confidence — not that Rahul Kumar is the registered owner. See [Claim types](#claim-types).

## Architecture

```
FastAPI (app/main.py)
  |
  +-- routes/investigations.py   create/read investigations, report, graph, timeline
  +-- routes/lookups.py          direct single-source /phone,/email,/username,/person,/domain lookups
  +-- routes/search.py           GET/POST /api/v1/search (SearxNG passthrough)
  +-- routes/sources.py          source health
  +-- routes/utils.py            standalone HIBP Pwned Passwords check
  |
  +-- normalization.py           identifier detection + normalization
  +-- orchestrator.py            in-process asyncio queue + worker pool (no Redis/Celery);
  |                              recursively investigates root + approved pivots
  +-- pivot_engine.py            decides what's worth investigating next, records every
  |                              decision (including skips) for auditability
  +-- extraction.py              regex/phonenumbers/URL-pattern candidate extraction
  |                              from evidence text -- feeds the pivot engine
  +-- document_fetch.py          SSRF-safe fetch for discovered document URLs
  +-- document_text.py           PDF (pypdf) / HTML (bs4) / TXT -> plain text
  +-- document_mining.py         picks which discovered URLs to fetch+mine, budget-capped
  +-- adapters/                  one adapter per source, normalized AdapterResult shape
  |     base.py                  SourceAdapter ABC, AdapterResult, SourceUnavailable
  |     registry.py               identifier_type -> adapters[] dispatch table
  |     phone_adapter.py          phonenumbers (technical data, offline)
  |     email_adapter.py          holehe (public account-existence probes)
  |     sherlock_adapter.py       sherlock-project (username presence)
  |     maigret_adapter.py        maigret (username presence, native async)
  |     domain_recon_adapter.py   nslookup NS/MX/SPF/DMARC + optional whois (DOMAIN)
  |     public_web_adapter.py     self-hosted SearxNG (public-web discovery)
  |     document_adapter.py       PDF/HTML/TXT mining on discovered URLs (deep mode only)
  |     hibp_adapter.py           HIBP Pwned Passwords (standalone utility, NOT wired to investigations)
  |     wikidata_adapter.py        Wikidata (structured person/org lookup, PERSON_NAME)
  |
  +-- entity_resolution.py       AdapterResult -> Entity/Relationship/Evidence, idempotent
  +-- confidence.py              explainable, additive confidence scoring
  +-- report.py                  investigation report assembly
  +-- graph.py / timeline.py     JSON nodes/edges and chronological event feed
  +-- models.py / db.py          SQLAlchemy models; SQLite (dev) or PostgreSQL (prod)
  +-- job_queue.py               Postgres-backed multi-process job queue (FOR UPDATE SKIP
  |                              LOCKED) + lease/heartbeat crash recovery -- Postgres only
  +-- worker_main.py             standalone worker entrypoint (`python -m app.worker_main`),
  |                              no HTTP server -- the docker-compose "worker" service
  +-- metrics.py                 in-process Prometheus counters, exposed at GET /metrics
  +-- logging_config.py          structured JSON logging
  +-- retention.py               configurable data retention / cleanup (opt-in, off by default)
  +-- alembic/                   schema migrations (the only schema-creation path on Postgres)
```

### SQLite (dev) vs PostgreSQL (production)

`OSINT_DATABASE_URL` selects the mode (`app.db.is_sqlite` / `is_postgres`):

- **SQLite** (default, `sqlite:///./osint.db`): single process, in-process
  `asyncio.Queue` + worker pool (unchanged from Phase 1). Nothing to install
  beyond `pip install -r requirements.txt`.
- **PostgreSQL**: the API process no longer runs adapters itself -- it only
  writes `investigations` rows with `status=queued`; any number of
  `app.worker_main` processes claim them via `SELECT ... FOR UPDATE SKIP
  LOCKED` (no separate broker -- Postgres is already the required source of
  truth, so adding Redis/Celery/RQ for this workload isn't justified). A
  worker renews its lease (`OSINT_INVESTIGATION_LEASE_SECONDS`, default 120s)
  while processing; if it dies, another worker's idle-poll cycle reclaims the
  stale lease and requeues the investigation for anyone to pick up -- see
  `app/job_queue.py`.

### Job orchestration + automatic pivoting

`POST /api/v1/investigations` returns immediately (`status: queued`); a
background worker pool (`app/orchestrator.py`) then investigates the root
identifier: every adapter for its type, plus `public_web`. The evidence
collected is handed to the pivot engine (`app/pivot_engine.py`), which
extracts candidate identifiers (email/phone/username/domain -- see
[Extraction](#extraction--pivoting)) and, for each one that clears a
confidence threshold and isn't a duplicate, spawns a *pivot*: the same
investigation loop runs again for that new identifier, anchored to the
entity that triggered it, recursively, up to `OSINT_MAX_PIVOT_DEPTH`. No
adapter call ever runs inside the HTTP request.

Job/investigation status: `queued -> running -> completed | partial | failed | unavailable`.
`partial` means some sources completed and others didn't (unavailable/failed);
`unavailable` means every source was unreachable/not installed -- neither
ever fabricates a result.

### Extraction + pivoting

`app/extraction.py` pulls candidate identifiers out of evidence text/URLs:

- **Email** — regex, confidence 0.75.
- **Phone** — `phonenumbers.PhoneNumberMatcher` (validated, not just digit-shaped), confidence 0.80.
- **Username** — only from a short, explicit list of known social-profile URL
  patterns (`github.com/<user>`, `twitter.com/<user>`, ...), confidence 0.60.
  Reserved path segments (`/login`, `/search`, ...) are excluded.
- **Domain** — derived from discovered URLs, *excluding* a blocklist of
  generic platforms (github.com, whatsapp.com, amazon.com, ...). A domain
  mentioned once is confidence 0.35 (below the default threshold, so it
  won't pivot on its own); mentioned twice or more across the same evidence
  batch (independent corroboration) it's boosted to 0.60.
- **Person name / company** — deliberately **not** extracted from free text.
  Reliably telling "Rahul Kumar" apart from "Reset Password" or "Contact Us"
  in arbitrary scraped text needs real NER, not regex; a naive version would
  produce exactly the over-generated noise the spec warns against.

Every pivot decision is recorded in the `pivots` table, including ones that
were *skipped* (`skipped_duplicate`, `skipped_low_confidence`,
`skipped_self_reference`, `skipped_limit_reached`) — so the pivot engine's
behavior is fully auditable via `GET /api/v1/investigations/{id}` (`pivots`
field) or `.../timeline`, not just its successes.

**Safety controls** (`.env`, all overridable):

| Setting | Default | Purpose |
|---|---|---|
| `OSINT_MAX_PIVOT_DEPTH` | 2 | root=depth 0; stop after this many pivot levels |
| `OSINT_MAX_TOTAL_PIVOTS_PER_INVESTIGATION` | 15 | hard cap per investigation |
| `OSINT_MAX_PIVOTS_PER_ENTITY` | 5 | hard cap per parent entity |
| `OSINT_PIVOT_CONFIDENCE_THRESHOLD` | 0.5 | below this, recorded but not executed |
| `OSINT_MAX_INVESTIGATION_RUNTIME_SECONDS` | 600 | wall-clock budget across root + all pivots |

### Document mining (PDF/HTML/TXT)

`app/document_adapter.py` fetches a discovered URL, extracts its text
(pypdf for PDF, BeautifulSoup for HTML, plain decode for TXT), and stores
that text as `Evidence.raw_metadata['snippet']` -- the *same* field the
pivot engine already reads for search snippets. This means candidate
identifiers embedded in a mined PDF/HTML/TXT document go through the exact
same confidence/dedup/limit gating as anything found in a search result,
with no separate code path (see IMPORTANT IMPLEMENTATION PRINCIPLE) and
zero changes to `pivot_engine.py`. Live-verified against a real government
PDF (IRS Form W-9): 38K characters of real text extracted, embedded
identifiers correctly mined.

**SSRF protection** (`app/document_fetch.py`): only `http`/`https`; hostname
is DNS-resolved and checked against private/loopback/link-local/reserved IP
ranges *before* connecting *and* before following every redirect hop (the
realistic attack for a service fetching already-discovered URLs: an
initially-public URL redirecting to an internal address) -- both paths are
live-tested against real targets (`127.0.0.1`, `169.254.169.254`,
`localhost`, IPv6 `::1`, and a real redirect chain). Response size is capped
via streaming (not trusted from `Content-Length` alone), request time is
capped, redirects are capped at 5 hops. Documented residual risk: DNS
rebinding isn't fully closed (see the module's docstring) -- closing it
completely needs socket-level IP pinning that httpx's public API doesn't
expose cleanly; what's implemented blocks the realistic threat for a
service that mines already-vetted public URLs, not raw attacker-supplied ones.

Document mining only runs in **`deep` mode** (see below) -- it's the most
expensive step (a real fetch per document), so `quick`/`standard` skip it
by default. Bounded by `OSINT_MAX_DOCUMENTS_PER_INVESTIGATION` (default 10),
`OSINT_DOCUMENT_FETCH_TIMEOUT_SECONDS` (15), `OSINT_MAX_DOCUMENT_BYTES` (10MB).

### Investigation modes

`POST /api/v1/investigations` accepts `"mode": "quick" | "standard" | "deep"` (default `standard`):

| Mode | Adapters | `public_web` | Pivoting | Document mining |
|---|---|---|---|---|
| `quick` | first/primary adapter only | no | no | no |
| `standard` | every per-type adapter | yes | 1 level | no |
| `deep` | every per-type adapter | yes | up to `OSINT_MAX_PIVOT_DEPTH` | yes |

### Idempotency

Every phone/email/username/domain/URL is normalized before storage. Entities
are deduplicated per-investigation on `(entity_type, normalized_value)`; a
second source (or a second pivot path) confirming the same fact adds another
Evidence row (and a `DISCOVERED_FROM` corroboration edge) and recomputes that
entity's confidence -- it never creates a duplicate entity or re-runs the
same adapters twice for the same identifier.

That's data-level dedup, always on. Separately, `POST /api/v1/investigations`
accepts an optional `Idempotency-Key` header (request-level, opt-in) -- a
retried request with the same key and the same body returns the original
investigation instead of creating a second one; the same key with a
*different* body is a client bug and gets `409`. Scoped per API key (or
`anonymous` when auth is off), enforced by a DB unique constraint so two
concurrent retries can race safely.

## Local Development

No Docker, no Postgres, no Redis needed for this path -- SQLite +
local Python processes only.

```bash
# 1. app venv
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt   # add -r requirements-dev.txt for ruff
cp .env.example .env                           # already defaults to SQLite + local SearxNG

# 2. SearxNG, native install (no Docker) -- one-time
./scripts/setup_searxng.sh

# 3. bring up both (SearxNG backgrounded, API in the foreground)
./scripts/start_local.sh
```

`start_local.sh` is the one-command path. Equivalent manually, in separate
terminals:

```bash
# terminal 1
./scripts/start_searxng.sh          # listens on 127.0.0.1:8890

# terminal 2
source .venv/bin/activate
python -m app.main                  # or: uvicorn app.main:app --reload
```

**No separate worker terminal needed here.** SQLite mode runs the worker
in-process inside the API (`app/main.py`'s lifespan starts it automatically
-- see "SQLite (dev) vs PostgreSQL (production)" above). `python -m
app.worker_main` is the Postgres-mode worker entrypoint; it refuses to start
against a SQLite database (SQLite has no `SELECT ... FOR UPDATE SKIP
LOCKED`, which that queue depends on) -- running it here would be a second,
non-functional consumer, not added scale.

Then verify it's alive:

```bash
curl http://127.0.0.1:8000/ready
curl "http://127.0.0.1:8000/api/v1/search?q=python%20programming"
curl -X POST http://127.0.0.1:8000/api/v1/investigations \
  -H "Content-Type: application/json" -d '{"input_identifier": "9505408820"}'
```

### SearxNG without Docker

`scripts/setup_searxng.sh` clones [searxng/searxng](https://github.com/searxng/searxng)
into `searxng-src/`, installs it into its own venv (`pip install -e .`, pure
wheels, no C build needed as of the pinned commit), and writes a local
`settings.yml`: JSON output enabled, `limiter: false` (SearxNG's optional
Valkey/Redis-backed bot-protection limiter -- not needed for a single local
caller, so this stays a one-process, zero-extra-service install), listening
on `127.0.0.1:8890`. `scripts/start_searxng.sh` runs it via SearxNG's own
Flask dev server (`python -m searx.webapp`) -- no uWSGI/nginx, that's only
documented for production multi-user deployments. Requires Python >=3.10
(SearxNG's own requirement); verified working on 3.12.

Configured engines (`searxng-src/settings.yml`) beyond SearxNG's own
defaults (duckduckgo, startpage, brave, wikipedia already enabled upstream):
bing, mojeek, google explicitly enabled to evaluate. Run
`python3 scripts/verify_searxng_engines.py` (real queries against your
running instance) to see which currently return real results vs.
CAPTCHA-blocked/parsing-broken -- see "Search engine verification" below for
the last recorded result. `OSINT_SEARXNG_ENGINES` restricts every query this
app makes to a curated working subset (default `google,bing,brave,wikipedia`)
so it never wastes a request on an engine known to be dead.

## Running in production (PostgreSQL + Docker)

Entirely optional -- "Local Development" above (SQLite + local processes,
no Docker) is the default and is enough for single-instance use. This is
only for horizontal scaling across multiple worker processes/machines.

```bash
docker compose up -d --build   # postgres + migrate (one-shot) + api + worker
docker compose up -d --scale worker=3   # add more worker processes, no code change
```

Three services, no Kubernetes, no Redis (see "SQLite vs PostgreSQL" above for
why Postgres alone is enough):

- **postgres** -- source of truth + job queue.
- **migrate** -- runs `alembic upgrade head` once, then exits; `api`/`worker`
  wait for it (`depends_on: condition: service_completed_successfully`).
- **api** -- FastAPI, no in-process workers in this mode.
- **worker** -- `app.worker_main`, no HTTP server; scale horizontally by
  running more of these (containers or bare processes), or increase
  `OSINT_WORKER_CONCURRENCY` to run more polling loops inside one process.

SearxNG isn't in the compose file (optional external infra, see above) --
point both `api` and `worker` at it with `OSINT_SEARXNG_URL`.

Running the migration manually (e.g. against a managed Postgres, no compose):

```bash
export OSINT_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/osint
./.venv/bin/python -m alembic upgrade head
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000   # api
./.venv/bin/python -m app.worker_main                          # worker, run N of these
```

`GET /metrics` (Prometheus text format) and `GET /ready` (checks DB
connectivity) are meant for a load balancer / scraper in front of each `api`
replica -- counters are per-process, the standard Prometheus model
(aggregation happens at the scraping server).

## Deploying to a fresh server (Ubuntu/Debian)

Same code either way -- pick SQLite (single process, "Local Development"
above) or PostgreSQL + Docker ("Running in production" above) once the box
itself is ready. This section is just getting a bare Ubuntu/Debian server to
that starting point.

```bash
# 1. OS packages
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git dnsutils

# dnsutils provides `nslookup` -- domain_recon_adapter.py's hard dependency
# (DOMAIN lookups report unavailable without it, see Sources table below).

# 2. Optional: WHOIS support for domain_recon
# Purely additive -- domain_recon already runs DNS/MX/SPF/DMARC without it
# (shutil.which("whois") is checked at call time, not at startup), so a
# failed/skipped install here must never block the rest of setup.
sudo apt-get install -y whois || true

# 3. clone + venv + deps (same as Local Development)
git clone <this-repo-url> osint && cd osint
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit OSINT_DATABASE_URL / OSINT_SEARXNG_URL as needed

# 4. SearxNG (optional but recommended -- powers public_web + /api/v1/search)
./scripts/setup_searxng.sh

# 5. bring it up -- SQLite/single-process:
./scripts/start_local.sh
# ...or PostgreSQL/Docker, see "Running in production" above.
```

For a long-running server rather than a one-off session, put step 5 behind
whatever process supervisor you already use (systemd, tmux, pm2) -- nothing
here requires a specific one, matching the "no unrequested infrastructure"
principle this project follows throughout (no Redis/Celery/Kubernetes
either, see "Job orchestration" above).

`whois` is genuinely optional at every layer -- the apt install above can
fail or be skipped, `domain_recon`'s own runtime check
(`shutil.which("whois")`) degrades to DNS/MX/SPF/DMARC-only with
`whois_available: false` in the response, and no lookup ever fails because
the binary is missing. Install it any time later and it takes effect on the
next domain lookup with no restart or code change needed.

## Running tests / lint

```bash
./.venv/bin/python -m pytest tests/ -q
./.venv/bin/ruff check app/ tests/
```

100+ tests, all offline/mocked at the unit level (network-dependent behavior
is verified against real adapters/services during development, not on every
CI run -- see each adapter's docstring for what was manually verified live).

## API

All endpoints below are under `/api/v1` except health/readiness, which are
unversioned by convention.

The `*/lookup` endpoints and `POST /api/v1/investigations` share the exact
same adapter registry, entity/evidence/confidence pipeline, and (for
`person`/`domain`) the same public_web/SearxNG client (see
`orchestrator.run_direct_lookup`, which both `_investigate_identifier` and
the direct routes call through) -- the difference is scope, not
implementation. A `*/lookup` call is synchronous (blocks until that one
identifier's own sources finish) and single-level: no automatic pivoting,
no recursion, no document mining. `POST /api/v1/investigations` is async
(returns immediately, poll `.../status`) and runs the full pipeline --
pivots into anything discovered, mines documents in `deep` mode, etc. Use
`*/lookup` for "just this one source, right now"; use `/investigations` for
a full automated investigation.

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/api/v1/search` | Public web search via SearxNG -- `?q=` or `{"query", "page", "language", "safesearch"}`; normalized `{query, results[], meta}`. Same SearxNG client the investigation pipeline's `public_web` source uses -- see below |
| POST | `/api/v1/phone/lookup` | `{"phone"}` -- phonenumbers only, synchronous, no queue |
| POST | `/api/v1/email/lookup` | `{"email"}` -- holehe only, synchronous |
| POST | `/api/v1/username/lookup` | `{"username"}` -- sherlock+maigret, synchronous (live-verified: several minutes, longer under concurrent load -- both run in full, sequentially, no partial/streaming response). Hits both tools confirm on the same site are merged into one response row (keyed by domain, not exact URL -- the two tools format the same profile's URL differently) with confidence re-scored through the real confidence engine, e.g. `0.55 -> 0.65` on independent corroboration -- see `routes/lookups.py::_merge_username_evidence` |
| POST | `/api/v1/person/lookup` | `{"person_name"}` -- Wikidata + public_web, synchronous |
| POST | `/api/v1/domain/lookup` | `{"domain"}` -- DNS/WHOIS recon (`domain_recon`, see below) + public_web, synchronous |
| POST | `/api/v1/investigations` | Create an investigation (`input_identifier`, optional `identifier_type`, `mode`); auto-detects type if omitted; enqueues background processing, auto-pivots, poll for results |
| GET | `/api/v1/investigations` | List investigations |
| GET | `/api/v1/investigations/{id}` | Full detail: jobs, entities, relationships, evidence, pivots, technical metadata |
| GET | `/api/v1/investigations/{id}/status` | Lightweight progress (job/pivot counts, elapsed time) -- cheap to poll |
| POST | `/api/v1/investigations/{id}/cancel` | Cooperative cancellation (409 if already terminal) |
| GET | `/api/v1/investigations/{id}/report` | Structured report (case info, sources queried/unavailable, discovered entities, relationships, evidence, limitations) |
| GET | `/api/v1/investigations/{id}/graph` | `{nodes, edges}` -- no rendering, JSON only |
| GET | `/api/v1/investigations/{id}/timeline` | Chronological machine-readable event feed |
| PUT | `/api/v1/investigations/{id}/notes` | Set analyst notes |
| GET | `/api/v1/sources/health` | Per-source install/enabled/success-rate/avg-duration status |
| POST | `/api/v1/utils/pwned-password` | Standalone HIBP Pwned Passwords check (password exposure, not an email-breach lookup) |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (checks DB connectivity) |
| GET | `/metrics` | Prometheus text-exposition metrics (per-process counters) |

`POST /api/v1/investigations` also accepts an optional `Idempotency-Key`
header -- see [Idempotency](#idempotency) above.

Every response carries an `X-Request-ID` header (echoed back if the caller
supplied one, generated otherwise). Errors come back as
`{"error": {"code", "message", "request_id", "details"}}`. Every request also
writes one `AuditLog` row (method/path/status/duration/investigation_id) --
separate from investigation Evidence, since it's service telemetry, not an
OSINT finding.

## Authentication + concurrency limits

No API key, ever -- this service is meant to be called directly by other
internal applications with no login step. Runs on localhost/an internal
network only; if it's ever reachable more broadly, put a reverse proxy or
network-level control in front of it rather than reintroducing app-level
auth. `OSINT_MAX_CONCURRENT_INVESTIGATIONS` caps how many investigations can be
`queued`/`running` at once across all clients (a DB `COUNT`, correct across
processes either way) -- `POST /api/v1/investigations` returns 429 above
that. `OSINT_SOURCE_MAX_CONCURRENCY` separately caps concurrent calls to the
*same* source adapter within one process (e.g. several investigations all
pivoting into `sherlock` at once) -- per-process only, not cluster-wide; a
true cluster-wide cap would need Postgres advisory locks or Redis, not
justified for this workload.

## Service reliability (long-running investigations)

- **Cancellation** -- `POST /api/v1/investigations/{id}/cancel` sets a
  cooperative `cancel_requested` flag (409 if the investigation is already
  terminal). The orchestrator checks it at the start of every identifier
  investigated and before every pivot recursion -- never mid-adapter-call,
  so a cancel never corrupts an in-flight write; work already running
  completes normally, then the investigation finalizes as `cancelled` with
  every entity/evidence/job collected so far intact. Live-verified: cancelled
  a real running deep-mode investigation mid-flight, confirmed `cancelled`
  status with all 24 entities / 23 evidence rows preserved.
- **Progress reporting** -- `GET /api/v1/investigations/{id}/status` is a
  lightweight polling endpoint (job counts by status, pivot counts by
  status, elapsed seconds, cancel_requested) that never loads the full
  entity/evidence graph, unlike `GET /{id}`.
- **Retry policy** -- transient adapter failures (`SourceUnavailable`,
  default `retryable=True`) get up to `OSINT_ADAPTER_MAX_RETRIES` bounded,
  exponentially-backed-off retries. A permanently-blocked source
  (`retryable=False`) and unexpected bugs (bare exceptions, `FAILED` not
  `UNAVAILABLE`) are never retried -- retrying a programming error or a
  permanent block can't ever help.
- **Source timeouts** -- every adapter already has its own configurable
  timeout (`OSINT_ADAPTER_TIMEOUT_SECONDS`, plus per-source ones like
  `OSINT_DOCUMENT_FETCH_TIMEOUT_SECONDS`); a slow source raises
  `SourceUnavailable`, never hangs the investigation.
- **Investigation wall-clock budget** -- `OSINT_MAX_INVESTIGATION_RUNTIME_SECONDS`
  stops new pivots/document mining once exceeded; already-collected evidence
  is preserved, never discarded.
- **Disabled sources** -- a `SourceHealth` row with `enabled=false` is
  skipped before even calling `is_available()` (toggled via direct DB access
  in Phase 1; no admin-toggle endpoint yet).
- **Worker isolation** -- each investigation is one recursive coroutine on
  the shared worker pool (`OSINT_WORKER_CONCURRENCY`); one slow investigation
  occupies one worker slot and never blocks the others.
- **SQLite concurrency** -- WAL journal mode + a 30s busy-timeout (`app/db.py`)
  are enabled at connection time, and the orchestrator commits (not just
  flushes) a job's `RUNNING` state *before* the long adapter `await`, so a
  multi-minute adapter call never holds a write lock that would block a
  concurrent request like `.../cancel`. Found live: the first cancellation
  test against a real running investigation returned `sqlite3.OperationalError:
  database is locked` -- fixed and re-verified live.
- **Job state machine** -- an investigation's status can only move along
  allowed edges (`queued -> running -> {completed, partial, failed,
  unavailable, cancelled}`, plus `running -> queued` for a lease reclaim);
  every write re-reads the current DB status first and refuses/no-ops on an
  invalid transition (`app.enums.can_transition`). Guards against the one
  real hazard multiple worker processes introduce: a worker whose lease got
  reclaimed as dead, but that was actually just slow, finishing its own
  (stale) write after a second worker already completed the same
  investigation -- that write is now dropped instead of clobbering the real
  result.
- **Cross-process worker crash recovery** (Postgres mode only) -- live-verified:
  started a worker with an 8s lease, let it claim and start a real
  investigation, `kill -9`'d it mid-job. The investigation sat `running` with
  no live worker for 10s (confirmed via the status endpoint), then a fresh
  worker process's idle-poll cycle logged `reclaiming investigation ...:
  lease held by worker-f5bb8378 expired`, requeued it, claimed it itself, and
  ran it to a normal terminal status -- no manual intervention.
- **Cross-process cancellation** -- live-verified against the same Postgres +
  separate-worker-process setup: `POST .../cancel` from the API process while
  a different worker process held the investigation; the worker noticed
  `cancel_requested` on its next check and finalized it `cancelled`.
- **API restart recovery** -- an investigation's state lives entirely in
  Postgres, not in the API process; restarting the API mid-investigation
  doesn't affect a worker still processing it, and a fresh API process can
  immediately serve `GET .../status` for it.

## Sources — what each can and cannot establish

| Source | Identifier | Free? | What it establishes | What it does NOT establish | Reliability |
|---|---|---|---|---|---|
| `phonenumbers` | Phone | Yes, offline | Carrier/region/line-type for the *number range* | Who currently holds the number | Deterministic |
| `holehe` | Email | Yes | Whether an account exists at ~140 sites' register/login endpoints (read-only probe, no password-recovery flows) | The account holder's real identity | High (structural check) |
| `sherlock` | Username | Yes | Whether the exact username string is claimed on each site it checks | That the same person controls every claimed profile | Medium -- worse for common handles |
| `maigret` | Username | Yes | Same as sherlock, broader/different site set, capped to top-300 by default | Same caveat | Medium |
| `domain_recon` | Domain | Yes, offline (`nslookup`, optional `whois`) | NS/MX records, SPF/DMARC presence, and (only if the `whois` binary is installed -- optional, never required) raw WHOIS text | Domain ownership -- WHOIS privacy services often mask this even when the lookup itself succeeds | Deterministic |
| `public_web` | Any | Yes, self-hosted | Public pages mentioning the identifier (via SearxNG); results are filtered to require the literal term in title/content/URL | That a mention implies ownership/association beyond co-occurrence | Medium -- a hit is real, silence isn't proof of absence |
| `searxng` (via `GET/POST /api/v1/search`) | *(free-text query, standalone utility)* | Yes, self-hosted, no Docker (see "SearxNG without Docker") | Aggregated results from multiple legitimate public search engines -- see "Search engine verification" below | Nothing about the identifier's ownership -- it's a general search endpoint, not evidence-gathering (no relevance filtering, unlike `public_web`) | Depends on the underlying engine, see matrix below |
| `document_extraction` | *(discovered URL, deep mode only)* | Yes | Full text of a fetched PDF/HTML/TXT document, mined for embedded identifiers | That the document is authoritative or current | High for the fetch/parse itself; extracted candidates carry their own per-type confidence |
| `wikidata` | Person name | Yes, keyless | Structured facts about a *notable* exact-name match: official website, social media handles, employer -- see below | That a same-name match is the investigation subject (never assumed) | Deliberately capped below the pivot threshold for the match itself; structured claims are higher |
| HIBP Pwned Passwords | *(password, standalone utility)* | Yes | Whether a password appears in known breach corpora | Nothing about a specific email/account being breached | High |

**No company-registry or court-record source.** mca.gov.in's Master Data
Services and services.ecourts.gov.in's Case Status search were both
live-verified (2026-09-09) to gate *every* search behind a CAPTCHA, with no
free API for either -- automating past that would violate the
no-CAPTCHA-bypass constraint. Placeholder adapters existed briefly to make
that gap visible in `/api/v1/sources/health`, then were removed entirely
(2026-09-10) since they never actually did anything -- a permanently-`unavailable`
row isn't worth the maintenance surface of dead adapter code. See "Source
feasibility matrix" below for the full research record.

**Free-only by decision, not by default.** A paid-provider layer (People Data
Labs, Hunter.io identity/enrichment; Shodan, urlscan.io, BehindTheEmail) was
built, live-qualified against real controlled identifiers, and then removed
entirely (code, tests, `.env` keys) on 2026-09-10 after none of it
demonstrated real added value over the free stack above for this project's
actual India phone/email targets. `gravatar_adapter.py`,
`disposable_email_adapter.py`, `whatsmyname_adapter.py`,
`user_scanner_adapter.py`, `mca_adapter.py`, `ecourts_adapter.py` (plus
`app/providers/interfaces.py`'s unused pluggable-provider ABCs) were removed
the same way -- never wired into the active registry or (for mca/ecourts)
never able to do anything but report `unavailable`. See git history around
2026-09-10 for what was tried and why.

### Search engine verification

`scripts/verify_searxng_engines.py` runs real queries against the local
SearxNG instance -- "configured" (present in `searxng-src/settings.yml`) is
not the same claim as "verified working" (actually returned results without
CAPTCHA/login/parsing failure). Last recorded run (2026-09-09, real network,
against SearxNG 2026.9.8 self-hosted on this machine):

| Engine | Configured | Reachable | Returned results | Status |
|---|---|---|---|---|
| `google` | Yes | Yes | Yes | **Verified working** -- disabled upstream by default (known CAPTCHA risk under heavier use), enabled here and currently returning real results |
| `bing` | Yes | Yes | Yes | **Verified working** |
| `brave` | Yes | Yes | Yes | **Verified working** |
| `wikipedia` | Yes | Yes | Yes, for entity-style queries (e.g. "India") -- correctly 0 results for a numeric query like a phone number, that's not a failure | **Verified working** |
| `duckduckgo` | Yes | Yes | No | **Blocked** -- SearxNG reports `unresponsive_engines: [["duckduckgo", "CAPTCHA"]]`, a well-known issue when self-hosting against DuckDuckGo's scrape-detection |
| `startpage` | Yes | Yes | No | **Failed** -- `unresponsive_engines: [["startpage", "parsing error"]]`, this SearxNG version's scraper no longer matches Startpage's current HTML |
| `mojeek` | Yes | Yes | No | **Failed** -- no error reported, but 0 results for every query tried including well-known terms; likely a stale/broken scraper (mojeek ships `disabled: true` upstream by default) |

`OSINT_SEARXNG_ENGINES` (default `google,bing,brave,wikipedia`) restricts
every query this app makes to the verified-working subset, so
duckduckgo/startpage/mojeek stay configured in SearxNG (queryable directly
against `127.0.0.1:8890` for debugging) without this app wasting a request
on them by default. Re-run the verification script periodically -- these are
scrapers against real third-party sites, and which ones work can change.

### Source feasibility matrix (Indian public/company/court/business sources researched)

Every candidate below was checked live (not assumed) against: is it genuinely
public, does it require CAPTCHA/auth, is there a documented free API, and is
automated access technically/legally appropriate.

| Source | Public? | CAPTCHA? | Free API? | Verdict | Evidence |
|---|---|---|---|---|---|
| MCA Master Data (mca.gov.in) | Yes | **Yes, every search** | No | `UNAVAILABLE` | Live fetch shows literal "Enter Captcha" / "Refresh Captcha" fields |
| eCourts Case Status (services.ecourts.gov.in) | Yes | **Yes, every search** | No | `UNAVAILABLE` | Live fetch shows "Enter the Captcha (the 5 alphanumeric characters shown on the screen)" on every search path (CNR, case number, party name, act type) |
| GST Search Taxpayer (services.gst.gov.in) | Yes | Unclear -- JS-rendered SPA, backend AJAX contract undocumented | No documented public API | `MANUAL_ONLY` | The public GST homepage itself serves a bot-check challenge; the taxpayer-search page is a JS shell with no published API contract -- treating an unversioned internal AJAX endpoint as stable would mean inventing an undocumented API, which the spec forbids |
| data.gov.in (Open Government Data Platform) | Yes, in principle | No | Yes, `api.data.gov.in` -- but requires manual registration for a free API key | `MANUAL_ONLY` | The main site returns HTTP 403 from Akamai's edge WAF on a plain fetch; no specific company/business-master dataset was confirmed to exist. Free-tier registration (not payment) would be required, and no single verified dataset justified building an adapter this iteration |
| Wikidata (wikidata.org) | Yes | No | **Yes, fully documented, keyless** | `WORKING` -- implemented | robots.txt explicitly permits API access; live-verified structured company/person data (see `wikidata_adapter.py`) |

**Net result:** every India-specific government portal checked (MCA, eCourts,
GST) is either CAPTCHA-gated or has no stable documented API -- a consistent,
deliberate pattern in how these systems are built, not a gap in this
research. Wikidata is the one source that was both genuinely free/public
*and* had a real, live-verified, documented, automation-friendly API, so it's
the one that got implemented this iteration.

Every result is a `PUBLIC_ASSOCIATION` or `TECHNICAL` claim, never an
`IDENTITY_CLAIM` -- see [Claim types](#claim-types).

## Claim types

- **TECHNICAL** -- deterministic data about the identifier itself (phonenumbers
  carrier/region). Stored on the investigation, not the entity graph.
- **PUBLIC_ASSOCIATION** -- an entity/relationship inferred from public-source
  correlation. Always carries an explainable confidence score below 1.0
  (except the investigator's own input, which starts at 1.0 as a given, not
  a discovery).
- **IDENTITY_CLAIM** -- an authoritative, verified identity match. Phase 1 has
  no source capable of producing this; reserved for Phase 2 paid providers.

## Confidence engine

Additive, capped at 1.0, every factor named (`app/confidence.py`):

| Factor | Weight |
|---|---|
| Direct public source | +0.30 |
| Independent second source | +0.20 |
| Third independent source | +0.15 |
| Exact phone/email match | +0.15 |
| Name + company agreement | +0.10 |
| Recent observation (<90d) | +0.05 |
| Source reliability (avg trust ≥ 0.70) | +0.05 |

A single username hit is never treated as a confirmed person, one public
page is never a confirmed owner, and matching names alone never silently
merge two entities -- entities merge only on exact normalized-value
fingerprint match (same email, same phone, same domain, same username
string), never on name similarity.

## Known limitations

- No functional company-registry or court-record source -- MCA and eCourts
  are both CAPTCHA-gated with no free API (see the feasibility matrix above);
  DOMAIN investigations still rely on `public_web` plus pivoting, not a
  structured business-registry lookup.
- Wikidata's PERSON_NAME coverage is necessarily limited to *notable* people/
  organizations with an encyclopedia entry -- most individuals an
  investigator searches for will have no match at all, which is correct
  behavior (see [Extraction](#extraction--pivoting)'s conservative-by-design
  principle), not a bug.
- Document mining only fetches direct HTTP(S) document URLs -- it doesn't
  follow links found inside a mined document to discover further documents
  (no recursive crawling of a site), and only runs in `deep` mode.
- Person name / company name are never extracted from free text (see
  [Extraction](#extraction--pivoting)) -- a PERSON_NAME or DOMAIN
  investigation only pivots into email/phone/username/domain candidates,
  never a inferred new person or company name.
- Rate limiting is in-process (module-level dict) -- correct for Phase 1's
  single-process deployment, would need a shared store (Redis) behind
  multiple processes/instances.
- SQLite by default: fine for single-instance local use; swap
  `OSINT_DATABASE_URL` for Postgres if you need concurrent multi-process access.
- `public_web` requires you to self-host SearxNG; Phase 1 does not scrape a
  third-party search engine directly.

## Phase 2 (not implemented)

A paid-provider layer was tried and removed -- see "Free-only by decision"
above. Any future provider adapter (Twilio, Hunter, PDL, Apollo, RocketReach,
DeHashed, IntelX, commercial Indian MNP/search APIs, or anything else) would
still just be one more `SourceAdapter` registered in
`app/adapters/registry.py`, without touching entity resolution, confidence
scoring, or the orchestrator -- that boundary is what made the previous
attempt cheap to fully remove when it didn't pan out, and is why there's no
separate pluggable-interfaces module to keep in sync in the meantime.
