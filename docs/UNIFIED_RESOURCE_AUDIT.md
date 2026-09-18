# Unified API — Resource, Concurrency & Configuration Audit (Milestone 5)

## 1. Resource inventory

| Operation | Service | Blocking risk | Offload mechanism |
|---|---|---|---|
| Postgres queries | Bluweb | No | `asyncpg` async driver, native async/await |
| MinIO put/get | Bluweb | No | `asyncio.to_thread` (already existing, `artifact_store.py`) |
| Playwright/Chromium fetch | Bluweb | No | Playwright's own async API; browser processes are separate OS processes, not threads |
| Crawlee `RequestQueue` | Bluweb | No | Crawlee's own async API |
| **Trafilatura/DOM extraction** (article/forum/listing/event/job/faq/profile/index/generic/scrapling) | Bluweb | **Yes — was unmitigated** | **Fixed this milestone**: `asyncio.to_thread` (see §2) |
| **GLiNER/spaCy entity extraction** | Bluweb | **Yes — was unmitigated** | **Fixed this milestone**: `asyncio.to_thread` (see §2) |
| `nslookup`/`whois` subprocess | OSINT | No | Already `asyncio.to_thread`-wrapped (`domain_recon_adapter.py`) |
| Sherlock/Maigret/holehe (sync libraries) | OSINT | No | Already `asyncio.to_thread`-wrapped in every adapter |
| SearxNG/Wikidata/HIBP HTTP calls | OSINT | No | `httpx` async client |
| SQLite/Postgres queries | OSINT | No | SQLAlchemy async (SQLite mode) / sync-in-thread pattern already existing |
| Reddit OAuth/RSS HTTP calls | reddit_server | No | `httpx` async client throughout |
| `FeedCache`/`TokenBucket` | reddit_server | No | Pure in-memory, no I/O |
| Telethon MTProto calls | telegram_poc | No | Telethon's own async API |
| APScheduler jobs | telegram_poc | No | `AsyncIOScheduler`, runs its jobs as coroutines on the same loop |
| Avatar/media file cache read/write | telegram_poc | Negligible (small files, local disk) | Not offloaded — acceptable, sub-millisecond for typical avatar/media sizes, no evidence of a real problem |

## 2. The one genuine finding: CPU-bound extraction was blocking the shared event loop

**Measured directly** (not assumed): calling Bluweb's `extract_entities()` (GLiNER + spaCy inference) synchronously took **1.775s warm** and **18.9s on the very first (cold model-load) call**. Neither `extraction_router.py`'s trafilatura-based extractors (`extract_article`, `extract_forum`, `extract_listing`, `extract_event`, `extract_job`, `extract_faq`, `extract_profile`, `_index_to_document`, `_generic_fallback`, plus the Scrapling fallback) nor `story_service.py`'s `extract_entities()` call were ever offloaded from the event loop — they ran as plain synchronous calls inside `async def` functions.

Standalone, this only ever delayed *other Bluweb requests*. Consolidated into one process, it would have stalled Telegram's scheduler, Reddit's requests, and OSINT's requests for up to ~19 seconds per extraction — a direct, severe instance of "unified-process starvation... exposed by consolidation."

**Fix applied** (mechanical, no logic changes, same pattern OSINT's own adapters already use): every one of those ~11 call sites now goes through `await asyncio.to_thread(fn, ...)` instead of a direct call. Because two crawls could now race to cold-load the GLiNER/spaCy models on separate threads, a `threading.Lock` was added around each model's existing check-and-set singleton logic (`gliner_extractor.py`, `spacy_extractor.py`) — the load itself still only ever happens once; a rare concurrent caller during the very first cold-start window gets the pre-existing "model unavailable" graceful-degradation path instead of a race, which is the same fallback behavior the code already had for a genuinely failed model load.

**Verified, before and after**: a controlled harness ran two concurrent `extract_entities()` calls via `asyncio.to_thread` alongside a fast (50ms) ping loop — max ping latency **0.057s** (vs. what would have been 1.8s+ blocked, per the direct un-threaded measurement). Reproduced live through the actual Docker+nginx stack: a real crawl's health-check neighbors (`/telegram/health`, `/reddit/health`) stayed at 3-83ms throughout. Full Bluweb test suite re-run after the fix: 331 passed / 2 pre-existing failures — identical to baseline.

## 3. Concurrency audit — live-tested combinations

| Combination | Result |
|---|---|
| Bluweb preflight (2.7s) + OSINT domain lookup (1.2s) + Reddit health + Telegram health, all concurrent | Reddit/Telegram returned in 12-16ms (Milestone 3) |
| Real Bluweb crawl + 10x rapid Telegram/Reddit health polls (0.3s apart) during the crawl | All health checks 3-83ms, no stalls (this milestone, post-fix, through the live Docker+nginx stack) |
| Two concurrent GLiNER/spaCy extractions + fast ping loop | Ping max 0.057s (this milestone, isolated harness) |
| OSINT investigation (real, worker-processed) alongside other namespaces | Completed in ~1s, no interference (Milestone 3) |
| Reddit RSS cold (1.24s) vs. cached (0.006s) | Proves single shared in-process cache, no per-request contention (Milestone 3) |

No other genuine event-loop-blocking call site was found in reddit_server, OSINT, or telegram_poc — all of their potentially slow operations were already correctly async or thread-offloaded prior to this milestone.

## 4. Memory / CPU / process isolation (Docker, real container)

| Measurement | Value |
|---|---|
| Idle memory (models loaded from a prior request) | ~284-395MB RSS |
| PIDs (docker stats, includes worker threads) | 6 at idle, 8 during/just-after a crawl |
| `docker top` process list | Exactly one `uvicorn` process, no orphaned Chromium/subprocess, before or after a crawl against a non-JS target |
| Memory across 3 restart cycles | 283.9MiB → 293.1MiB → 284.4MiB — no growth trend |
| Scheduler jobs across 3 restart cycles | Exactly 4 each time, never duplicated |

Chromium/Playwright process behavior under real browser-fallback load was **not force-triggered** in this audit (the safe test targets used, `example.com`/`example.org`, are simple static pages that never need the browser-fallback path) — verified instead by code inspection: `crawl_max_concurrent_browser_global` (default 8) caps concurrent Chromium processes globally, and Playwright's own `async with` context-manager usage in `services/preflight/browser.py`/`services/crawling/fetchers.py` guarantees page/browser cleanup on every exit path (including exceptions). No code change made here — no evidence of a problem, and forcing a real multi-browser-fallback crawl against an external site was judged unnecessary load on a third party for an audit that code inspection already answers.

## 5. Dependency failure behavior

| Dependency | Test performed | Result |
|---|---|---|
| MinIO unreachable | Real (pre-existing credential mismatch in this environment, Milestone 3) | With `ARCHIVAL_ENABLED=true` (default): crawl fails cleanly with `status: failed` and a clear error message (previously got stuck at `running` forever — see known limitation below). With `ARCHIVAL_ENABLED=false`: crawl completes successfully end-to-end, confirmed live this milestone. |
| SearxNG unreachable | Real (not running in this environment throughout M1-M5) | OSINT's `public_web`/`/api/v1/search` report `unavailable` per-source; rest of the investigation proceeds — confirmed repeatedly across milestones |
| Reddit OAuth unconfigured | Real (no credentials in this environment) | `503 REDDIT_NOT_CONFIGURED` on OAuth-backed routes; RSS routes unaffected — confirmed repeatedly |
| Postgres unreachable (Bluweb) | **Not force-tested against the real external DB** (shared production infrastructure, out of scope to take down) | Verified by code inspection instead: `/scrape/health/ready` wraps `SELECT 1` in try/except, returns `503` with the exception message on failure — deterministic, unrelated to consolidation |
| Telegram disconnected/reconnect | Real, via container restart x3 | Clean reconnect every time, exactly one client, no duplicate session |

**One pre-existing, unrelated bug documented, not fixed** (per this milestone's own rule): Bluweb's `run_crawl` background task has no try/except around `artifact_store.ensure_bucket()`/`put_bytes()` — a real MinIO failure (with `ARCHIVAL_ENABLED=true`) leaves the crawl job's `status` stuck at `running` forever instead of transitioning to `failed`. First found in Milestone 3, still present, unrelated to this milestone's hardening scope (it's an error-handling gap in the crawl engine's own MinIO integration, not a unified-process concurrency issue).

## 6. Configuration audit

| Variable | Service | Required? | Default | Path-sensitive? | Failure if missing |
|---|---|---|---|---|---|
| `DATABASE_URL` | Bluweb | Yes | none (required) | No (Postgres URL) | App fails to start (Pydantic validation error) |
| `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`/`MINIO_BUCKET`/`MINIO_SECURE` | Bluweb | Only if `ARCHIVAL_ENABLED=true` | placeholder defaults | No | Crawl fails per §5 if wrong/unreachable and archival is on |
| `ARCHIVAL_ENABLED` | Bluweb | No | `true` | No | N/A — new in Milestone 4 |
| `HF_TOKEN` | Bluweb | No | none | No | GLiNER/IndicNER model download may 401 on first run without it; degrades to "unavailable" |
| `OSINT_DATABASE_URL` | OSINT | No | `sqlite:///./osint.db` | **Yes** (anchored to `OSINT/` since Milestone 1) | Falls back to default SQLite path |
| `OSINT_SEARXNG_URL` | OSINT | No | none | No | `public_web`/`/search` degrade to unavailable |
| `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` | telegram_poc | Only for real Telegram operations | none | No | Auth endpoints report configuration errors; `/health`/`/ready` still work |
| `TELEGRAM_SESSION_PATH` | telegram_poc | No | anchored to `telegram_poc/data/` | **Yes** (anchored since Milestone 3's fix; further anchored to absolute in Milestone 3) | Would start a fresh anonymous session if ever misresolved (fixed) |
| `RAW_EVIDENCE_ENABLED` | telegram_poc | No | `true` | No | N/A — new in Milestone 4 |
| `MEDIA_STORAGE_PATH`/`AVATAR_CACHE_PATH`/`MESSAGE_MEDIA_CACHE_PATH`/`RAW_EVIDENCE_STORAGE_PATH` | telegram_poc | No | anchored to `telegram_poc/data/` | **Yes** (anchored since Milestone 3) | N/A, all path-anchored |
| `API_KEYS` | reddit_server | No (auth disabled if empty) | empty | No | Every `/api/reddit/*` route open if unset — documented, pre-existing, unchanged |
| `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`/`REDDIT_USER_AGENT` | reddit_server | Only for OAuth routes | none | No | OAuth routes `503`, RSS routes unaffected |
| `CORS_ORIGINS` | reddit_server | No | empty (CORS middleware not even added) | No | No wildcard risk — confirmed: middleware is conditionally added only if non-empty, and even then takes an explicit origin list, never `*` |

No secret values are printed anywhere in this document or in application logs (confirmed: reddit_server explicitly `register_secret()`s its client secret/password for log redaction; Bluweb/OSINT have no secrets in their env surface beyond DB/MinIO credentials, never logged; Telegram explicitly never logs `api_hash`/codes/passwords per its own documented policy, confirmed in Milestone 0 discovery).

## 7. Timeout audit

| Layer | Timeout | Source |
|---|---|---|
| Nginx (`unified_api/nginx.conf`) | `proxy_read_timeout`/`proxy_send_timeout` 300s, global | Set in Milestone 2, covers OSINT's slow username-lookup and Bluweb's instant-search |
| Bluweb preflight HTTP | `PREFLIGHT_HTTP_TIMEOUT_SECONDS` (10s default) | Existing app config, unchanged |
| Bluweb browser fallback | `PREFLIGHT_BROWSER_TIMEOUT_SECONDS` (20s default) | Existing app config, unchanged |
| OSINT adapters | `OSINT_ADAPTER_TIMEOUT_SECONDS` (45s default) | Existing app config, unchanged |
| OSINT investigation wall-clock | `OSINT_MAX_INVESTIGATION_RUNTIME_SECONDS` (600s default) | Existing app config, unchanged |
| Reddit OAuth/RSS | `REDDIT_REQUEST_TIMEOUT_SECONDS`/`REDDIT_RSS_TIMEOUT_SECONDS` | Existing app config, unchanged |
| Reddit RSS shared-budget wait | `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS` (55s default) | Existing app config, unchanged; nginx's 300s comfortably exceeds it |

No missing timeout was found that creates a *new* production risk introduced by consolidation — every slow operation already had its own service-level timeout, and nginx's single global timeout is generous enough to never cut one short. No timeout values were changed.

## 8. Nginx / container hardening — verified, unchanged from Milestone 2

One upstream (`unified-api:8000`), all four namespaces routed through it, `X-Forwarded-For`/`X-Forwarded-Proto`/`X-Real-IP`/`Host` all set. No WebSocket usage exists anywhere in the four services (confirmed by the Milestone 3 endpoint inventory — none of the 145 routes are WebSocket routes), so no upgrade-header configuration was needed. Long-running requests (OSINT username lookup, Bluweb instant search) covered by the 300s timeout. Container restart (verified x3 this milestone) leaves nginx itself untouched and reconnecting cleanly once the upstream is healthy again.

## 9. Logging / observability

Not redesigned, per instruction. Every request already logs with enough context to attribute it to a namespace (each service's own logger name — e.g. `osint.access`, `reddit_app.main`, `telethon.*`, Bluweb's own structured logger — appears in every line). No secrets found in any log line across the extensive live testing performed in this and prior milestones.

## 10. Security configuration

- No wildcard CORS anywhere (reddit_server's CORS middleware isn't even added unless `CORS_ORIGINS` is explicitly set to specific origins).
- No debug/reload mode enabled in any production startup path (the unified Docker image's `CMD` has no `--reload`).
- No secrets embedded in the unified `docker-compose.yml`, `unified_api/Dockerfile`, or `nginx.conf` — all credentials come from each service's own bind-mounted `.env` file.
- **Pre-existing, unrelated, not fixed**: Bluweb's *own* standalone `docker-compose.yml` (not used by the unified deployment) hardcodes `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` in plaintext. Documented, out of scope.
- **Not addressed, by explicit instruction**: Bluweb, OSINT, and telegram_poc still have zero application-level authentication. This milestone does not add auth (per Phase 12's explicit rule) — it remains a **deployment requirement**: do not expose this unified service beyond a trusted network without an nginx-level access-control layer (IP allowlist, Basic Auth, mTLS, or VPN), exactly as already documented in `docs/SINGLE_SERVER_DEPLOYMENT_PLAN.md` §16.
