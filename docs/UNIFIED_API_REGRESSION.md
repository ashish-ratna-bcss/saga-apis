# Unified API — Milestone 3 Regression Report

Scope: prove that mounting Bluweb/OSINT/reddit_server/telegram_poc into one FastAPI process changed **only** the URL namespace, nothing else. One real regression was found and fixed (below) — everything else confirmed PASS.

## 1. Baseline test counts (unchanged from Milestone 1/2, re-verified now)

| Service | Result |
|---|---|
| Bluweb | 331 passed, 2 pre-existing failures |
| OSINT | 183 passed |
| reddit_server | 223 passed, 1 skipped |
| telegram_poc | 226 passed, 3 pre-existing failures |

## 2. Unified test counts

Same four suites, run again after the fix in §16 below, against the renamed packages the unified app actually imports — identical results to §1. No test was added, removed, or changed.

## 3. Endpoint coverage

`unified_api/gen_endpoint_table.py` walks the **live registered routes** of the actual running unified app (not documentation) and writes `docs/UNIFIED_API_ENDPOINTS.md`. **145 routes** found across all four namespaces (Bluweb 46, OSINT 25, reddit_server 23, telegram_poc 51 — method-level rows, includes `/docs`, `/openapi.json`, `/console`, health/ready). Every route's original path is preserved verbatim under its new namespace prefix; no route renamed, removed, or added.

## 4. OpenAPI comparison — PASS

Compared each sub-app's `paths`/`components`/`info` (`.openapi()` called directly) against what's served live through the mount (`GET /<ns>/openapi.json`). **Identical** for all four services. The only difference is a `servers` key present in the HTTP-served version (`[{"url": "/scrape"}]` etc.) — added by FastAPI itself from the ASGI `root_path` at request time, exactly the "expected difference" the milestone spec called out in advance. No unexpected schema/method/parameter/response drift.

## 5. Health checks — PASS

All eight endpoints return correct status/body through nginx: `/scrape/health`, `/scrape/health/ready` (checks Postgres), `/osint/health`, `/osint/ready` (checks DB), `/reddit/health`, `/reddit/ready` (config-presence, no network call), `/telegram/health`, `/telegram/ready` (config-presence). Semantics unchanged — none converted into an expensive dependency check.

## 6. Bluweb verification — PASS

Real calls through `/scrape/...` against the unified process:
- `POST /api/v1/preflight` on `https://example.com` → 201, real DNS/HTTP/robots/feed probe, real capability score.
- `POST /api/v1/crawls` → 202, real crawl_id, `queued`→`running` transition observed live.
- `GET /api/v1/crawls/{id}` polling, `POST .../cancel` → 200.
- `GET /api/v1/documents`, `/documents/{id}`, `/documents/{id}/versions`, `/documents/{id}/versions/{n}`, `/documents/{id}/changes`, `/documents/{id}/diff` — all tested against **real pre-existing production documents already in Bluweb's Postgres** (collected 2026-09-08, before this work started). `content` field returns full real text; `diff` returns correct `changed:false` shape for identical versions.
- `POST /api/v1/search` — real Postgres FTS query, correct response shape.
- One real (pre-existing, unrelated) issue surfaced and **not fixed** — see §16.

## 7. OSINT verification — PASS

- `POST /api/v1/phone/lookup` on a real number → completed synchronously, real `phonenumbers` (offline) result.
- `POST /api/v1/domain/lookup` on `example.com` → `domain_recon` (real `nslookup`/`whois` subprocess) completed; `public_web` correctly reported `unavailable` (SearxNG not running in this test environment) — graceful degradation, matches documented standalone behavior, not a regression.
- `POST /api/v1/investigations` (mode=quick) → `queued` → **in-process worker picked it up and completed it within ~1 second**, full result retrievable via `GET .../{id}` and `.../status`. Proves the SQLite-mode worker pool runs correctly once, inside the merged lifespan, with no duplication.
- A wrong-schema request correctly returned `422` with a precise field-level Pydantic error (bonus proof for §13/error-parity).

## 8. Reddit verification — PASS

- `GET /api/reddit/rss/search?q=python` → real live Reddit RSS data, 200, first call 1.24s (real network fetch).
- Identical second call → 200 in 0.006s — **shared feed cache confirmed live** (not per-request/per-router).
- `POST /api/reddit/subreddit` (OAuth-backed) → `503 REDDIT_NOT_CONFIGURED`, matching standalone default (no OAuth creds in this environment) — correct parity, not a regression.

## 9. Reddit rate limiting — PASS

Exactly one `RedditClientManager` is constructed (module-level `app = create_app()`, mounted once). The cache-hit timing in §8 is only explainable by one shared in-process cache instance — if the unified app had created one limiter/cache per request or per router, the second call would show the same ~1.2s cold-fetch time, not 0.006s. No second rate limiter was added.

## 10. Telegram verification — PASS

- `GET /api/telegram/status` → real MTProto connection (`connected: true`), `authenticated: false` — confirmed this is the account's **actual pre-existing state**, not a bug: reproduced identically running telegram_poc fully standalone (see §16 for the one place standalone vs. unified genuinely differed, which was a different bug, now fixed).
- `GET /api/sources` → 200, real DB-backed empty list.
- `GET /console` → 200, real HTML content.
- `HEAD /console` → 405 — reproduced identically standalone; pre-existing (no explicit HEAD handler on that route), not caused by mounting.

## 11. Telegram singleton verification — PASS

Every startup (local run, Docker run, and the restart in §14) logs **exactly 4** `Added job` lines and **exactly one** `Scheduler started`, and exactly one `telethon.network.mtprotosender` connect. Mounting the existing `app` object once, and entering its lifespan once via `AsyncExitStack`, structurally cannot create a second client or scheduler — verified, not just asserted.

## 12. Telegram scheduler behavior — PASS

Same 4 jobs (`reconcile_pending_access`, `run_collection_cycle`, `connection_health_check`, `process_notifications`) registered every time, same intervals (unchanged code, unchanged `setup_scheduler` call). No duplicate registration observed across 3 separate startups (local, Docker, Docker-restart).

## 13. Database isolation — PASS

Bluweb → its own external Postgres (real host confirmed via `DATABASE_URL`, unaffected by CWD). OSINT → its own SQLite (`OSINT/osint.db`, real file, real historical data). Telegram → its own SQLite (`telegram_poc/data/telegram_service.db`). reddit_server → no database. No schema/table was merged or renamed. One real isolation **bug found and fixed** — see §16.

## 14. Filesystem isolation — PASS (after fix)

Verified inside the actual Docker container: `/srv/telegram_poc/data/` and `/srv/OSINT/osint.db` are the real bind-mounted host files (matching mtimes to the exact test run), not copies. No service wrote into another service's directory. See §16 for the one real violation found before the fix.

## 15. Configuration isolation — PASS (after fix)

Ran the unified app from `/tmp` (a CWD unrelated to any of the four repos) and confirmed, via direct `Settings()` instantiation: Bluweb loaded its real external-Postgres `DATABASE_URL`; Telegram loaded its real `TELEGRAM_API_ID`; OSINT and Telegram's SQLite/session/media paths all resolved to their own repo's absolute location — not `/tmp`, not `unified_api/`. No service read another's `.env`.

## 16. Restart test — PASS; Docker test — PASS; nginx test — PASS

All three folded into one pass (§ "Restart the container" in the transcript): stopped and restarted the actual `saga-unified-api` Docker container fronted by the actual `saga-nginx` container. Post-restart: all 7 health/ready endpoints 200 through nginx, exactly 4 scheduler jobs, real Telegram reconnect (session file mtime matches the restart timestamp — proves it re-used the real persistent session, not a fresh one).

### The one real consolidation regression found, and the fix

**Found**: running the unified process with a CWD other than the individual service's own repo directory (e.g. `unified_api/`, which is the natural CWD for the unified deployment) caused **OSINT and telegram_poc to silently create brand-new, disconnected copies of their SQLite database and, for Telegram, its session file** — inside `unified_api/` instead of using the real ones in `OSINT/` and `telegram_poc/`. Concretely: `unified_api/osint.db` and `unified_api/data/telegram_service.{db,session}` appeared after a single test run.

**Root cause**: Milestone 1 anchored each service's `env_file` location to an absolute, package-relative path (`BASE_DIR / ".env"`), which is CWD-independent and correct. But the **values inside those `.env` files** — `OSINT_DATABASE_URL=sqlite:///./osint.db` and `TELEGRAM_SESSION_PATH=./data/telegram_service.session` (plus the SQLite `database_url` and the media/avatar/raw-evidence cache paths) — are themselves relative paths, and SQLAlchemy/Telethon resolve a relative path against the **process's actual CWD at open time**, not against the package's own directory. Standalone, this never surfaces because the service is always launched from its own repo directory. Mounted, the unified process's CWD can legitimately be anywhere.

**Fix** (consolidation-specific, no business logic touched): added a pydantic `field_validator` to `osint_app/config.py` (`database_url`) and `telegram_app/config.py` (`telegram_session_path`, `database_url`, `media_storage_path`, `raw_evidence_storage_path`, `avatar_cache_path`, `message_media_cache_path`) that anchors a relative path/URL to each package's own `BASE_DIR` — the exact same pattern already used for `env_file` in Milestone 1. An absolute value (or a Postgres URL) passes through completely unchanged, so standalone behavior with an absolute-path `.env` is byte-identical to before.

**Verified**: re-ran OSINT's (183 passed) and telegram_poc's (226 passed / 3 pre-existing) full test suites — identical to baseline, zero regression from the fix itself. Re-ran the CWD-independence check from `/tmp` and from inside the actual Docker container: both now resolve to the real, single, correct file for every path, confirmed by mtime and by content (real historical Postgres/SQLite data visible through the API).

## 17. Known pre-existing failures (untouched, confirmed identical to Milestone 1/2 baseline)

- Bluweb: `test_healthy_domain_with_discovery_and_extraction_data`, `test_page_type_with_no_quality_observations_reports_none_not_zero` — 2 failures, pre-dates this work.
- telegram_poc: `test_private_channel_with_no_username_is_access_denied`, `test_start_bot_extracts_url_buttons_from_reply`, `test_start_bot_discovers_invite_link_from_button` — 3 failures, caused by an unpinned `telethon` version drift (`KeyboardButtonUrl` removed upstream), unrelated to consolidation.
- Bluweb crawl pipeline: `artifact_store.put_bytes()` (MinIO write) has no try/except around it in `crawl_engine.py`; a real MinIO credential mismatch in this environment's `.env` vs. the standing `ssor-minio` container caused a background crawl task to die silently, leaving the crawl job stuck at `status: running` forever (never transitions to `failed`). Reproduced with the same `.env` and same MinIO container — **not a consolidation regression** (would happen identically standalone), **not fixed** (pre-existing gap in error handling + an environment credential mismatch, both out of scope per "no unrelated bug fixes"). Documented here so it isn't mistaken for something this milestone broke.
- telegram_poc: `HEAD /console` → 405 (no explicit HEAD handler) — confirmed identical standalone, pre-existing, not fixed.
- telegram_poc: `PATCH /api/sources/{id}` and `POST /api/notifications/{id}/read` don't commit their DB writes (documented in the service's own prior technical audit) — untouched, unrelated to consolidation.

## 18. Known deferred items (unchanged from Milestone 2)

- Log-format unification across services (cosmetic only — first-imported service's `logging.basicConfig` wins; endpoint behavior unaffected).
- Bluweb MinIO storage redesign — confirmed again in this milestone (documents/versions/diff/changes all read from Postgres, `get_bytes` still never called) — **not redesigned, not removed**, per instruction.
- OSINT Postgres-mode worker embedding — untouched; SQLite-mode in-process worker (the current real config) verified working correctly inside the unified process.
- Full synthetic characterization-test authoring beyond each service's own existing suite (Milestone 3 used real, representative, minimal-footprint requests instead, per the "do not hammer external providers" instruction).

## Final gate

Every checklist item in the milestone spec is satisfied except where explicitly noted above as a found-and-fixed consolidation regression (§16) or a documented pre-existing/deferred item (§17/§18). No endpoint was removed, renamed, or had its request/response contract changed. No MinIO or OSINT-worker redesign was performed. The two config files touched (`OSINT/osint_app/config.py`, `telegram_poc/telegram_app/config.py`) received only the path-anchoring validators described in §16 — no other line changed.
