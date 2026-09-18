# Unified API — Failure Matrix (Milestone 5)

| Component | Failure | Expected behavior | Actual behavior | Changed? |
|---|---|---|---|---|
| Bluweb Postgres | Unreachable | `/scrape/health/ready` → 503 with reason; crawl/document writes fail | Confirmed by code inspection (`try/except` around `SELECT 1`); not force-tested against the real external DB (shared production infra) | No |
| Bluweb MinIO | Unreachable, `ARCHIVAL_ENABLED=true` (default) | Crawl fails | Crawl background task raises unhandled `S3Error`, job status stuck at `running` forever (never `failed`) — **pre-existing bug, documented in Milestone 3, not fixed here** | No (bug pre-dates this milestone) |
| Bluweb MinIO | Unreachable, `ARCHIVAL_ENABLED=false` | Crawl succeeds, no archival | Confirmed live: crawl completed, document created/retrievable, zero MinIO calls | No (this is Milestone 4's existing toggle, re-verified) |
| Bluweb Crawlee (local `storage/`) | Not tested (local disk, no external dependency) | N/A | N/A | — |
| Bluweb Chromium/Playwright | Not force-triggered (safe test targets don't need it) | Per-page/browser cleanup via `async with`; global cap of 8 concurrent | Verified by code inspection only, not a live browser-fallback crawl | No |
| OSINT SearxNG | Unreachable (true throughout this environment) | `public_web`/`/search` report `unavailable` per-source, rest of investigation proceeds | Confirmed repeatedly, live, across Milestones 3-5 | No |
| OSINT external lookup timeout | `OSINT_ADAPTER_TIMEOUT_SECONDS` (45s) | Adapter marked failed/retried per `OSINT_ADAPTER_MAX_RETRIES`, investigation continues | Existing behavior, not exercised live this milestone (would require an artificially slow external target) | No |
| Reddit OAuth upstream | Not configured (true throughout this environment) | `503 REDDIT_NOT_CONFIGURED` on OAuth routes | Confirmed repeatedly, live | No |
| Reddit RSS upstream | Reachable, tested live | 200, real data, cached on repeat | Confirmed live, cache hit 0.006s vs. cold 1.24s | No |
| Reddit rate limiter | N/A (no failure induced) | Single shared `TokenBucket`/cache per process | Confirmed structurally (one `RedditClientManager` instance) and empirically (cache-hit timing) | No |
| Telegram MTProto connection | Restarted 3x | Reconnects, session reused, `authenticated` reflects real account state | Confirmed live each cycle; session file mtime matches restart time (real file reused, not recreated) | No |
| Telegram SQLite | Not force-corrupted (would risk real data) | N/A | N/A | — |
| Telegram session file | Not force-deleted (would require a full interactive re-login on the real account) | N/A | N/A | — |
| Telegram scheduler during restart | Container restarted 3x | Exactly 4 jobs registered each time | Confirmed live, all 3 cycles | No |
| Filesystem (Bluweb `storage/`, OSINT `osint.db`, Telegram `data/`) | N/A (no failure induced) | Isolated per service, no cross-writes | Confirmed via before/after mtime snapshot across a full live-test + restart cycle: zero new files/directories | No |
| External HTTP dependencies generally | Various (SearxNG, Reddit OAuth) unreachable throughout testing | Graceful per-source degradation, never a 500 | Confirmed | No |
| Container restart | 3 consecutive cycles | Exactly one process, one client, one scheduler, 4 jobs, stable memory | Confirmed: memory 283.9→293.1→284.4 MiB, no growth trend; `docker top` shows exactly one `uvicorn` process | No |
| Nginx upstream failure | Not force-tested (would require stopping `unified-api` while nginx runs) | Nginx returns 502/504 to clients, container itself keeps running underneath | Not exercised live this milestone; nginx's own default error handling is unmodified, standard behavior | No |

## Expected existing behavior (unchanged by this milestone)
Every row above marked "Confirmed" and "No" under Changed reflects behavior that already existed in each standalone service and is preserved unmodified in the unified process.

## Behavior changed by this milestone
**One change**: Bluweb's content/entity extraction now runs via `asyncio.to_thread` instead of blocking the event loop directly (see `docs/UNIFIED_RESOURCE_AUDIT.md` §2). This does not change *what* happens on failure or success — extraction still produces the same `ArticleDocument`/entity results, the same exceptions still propagate to the same call sites — it only changes *which thread* runs the CPU-bound work, so concurrent requests to other namespaces are no longer blocked while it runs.

## Known limitations (pre-existing, documented, not fixed)
- Bluweb: MinIO failure with archival enabled leaves a crawl job stuck at `running` forever (missing try/except in `crawl_engine.py`'s background task) — Milestone 3 finding, still present.
- Bluweb, OSINT, telegram_poc: no application-level authentication — documented deployment requirement, not addressed by this milestone per explicit instruction.
- telegram_poc: `HEAD /console` → 405 (no explicit HEAD handler), `PATCH /api/sources/{id}` / `POST /api/notifications/{id}/read` don't commit their writes — Milestone 3 findings, unrelated to this milestone, still present.
- telegram_poc test suite: 3 pre-existing failures from an unpinned `telethon` version drift, unrelated to consolidation.
- Bluweb test suite: 2 pre-existing failures in `test_domains_api.py`, unrelated to consolidation.
- Bluweb's own standalone `docker-compose.yml` (not the unified deployment) hardcodes MinIO credentials in plaintext.
