# Unified API — Architecture

Supersedes the four-containers-behind-nginx design in `docs/SINGLE_SERVER_DEPLOYMENT_PLAN.md` for the parts that changed. That doc's discovery content (endpoints, env vars, resource profile) is still accurate per-service; this doc covers what's different now that all four run as one process.

## What changed

Bluweb, OSINT, reddit_server, and telegram_poc are no longer four separate FastAPI processes on four ports. They are one FastAPI process (`unified_api/app/main.py`) that **mounts** each service's own, unmodified `FastAPI` app object at a path prefix:

```
/scrape/*    -> Bluweb   (bluweb_app.main:app)
/osint/*     -> OSINT    (osint_app.main:app)
/reddit/*    -> reddit_server (reddit_app.main:app)
/telegram/*  -> telegram_poc  (telegram_app.main:app)
```

Every existing route, exception handler, middleware, and OpenAPI schema is untouched — mounting is Starlette's native sub-application composition (`parent_app.mount(prefix, sub_app)`), not route re-registration. Each service's own unprefixed `/health` becomes `/<namespace>/health` for free, with zero code change.

## The one real code change this required: package renaming

All four services' top-level Python package was literally named `app` (`Bluweb/app`, `OSINT/app`, `reddit_server/app`, `telegram_poc/app`). Python cannot import two different packages under the same name in one interpreter, so this was a hard blocker — renamed in place, mechanically, with no logic changes:

| Service | Old package | New package |
|---|---|---|
| Bluweb | `app` | `bluweb_app` |
| OSINT | `app` | `osint_app` |
| reddit_server | `app` | `reddit_app` |
| telegram_poc | `app` | `telegram_app` |

Every `from app...`/`import app...` (source, tests, Dockerfiles, alembic env.py, docker-compose commands, test monkeypatch strings) was updated within each repo. Each service's own test suite was run before and after and passed identically (Bluweb 331/2 pre-existing failures, OSINT 183/0, reddit_server 223/1 skipped, telegram_poc 226/3 pre-existing failures — same failures before and after, none introduced). Each service is still fully standalone-deployable exactly as before (its own Dockerfile/compose still builds and runs it alone under the new package name).

Two related fixes, both required for the merge and verified to not change standalone behavior:
- **`env_file` made absolute.** Bluweb and OSINT resolved `.env` relative to process CWD; in one merged process there is one CWD, so both would have read whichever `.env` happened to be there. Now anchored to `Path(__file__).resolve().parent.parent(.parent) / ".env"` inside each service's own repo (the pattern telegram_poc already used).
- **Bluweb's `GET /` → `/docs` redirect made relative** (`url="docs"` instead of `"/docs"`), so it still lands on `/scrape/docs` once mounted instead of the top-level `/docs`.

## Lifespan composition

Starlette does not auto-run a mounted sub-app's own lifespan. The parent app's lifespan enters all four sub-apps' lifespans via `contextlib.AsyncExitStack`:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for sub_app in SUB_APPS.values():
            await stack.enter_async_context(sub_app.router.lifespan_context(sub_app))
        yield
```

This is the standard, documented way to compose multiple FastAPI apps' startup/shutdown (`sub_app.router.lifespan_context(sub_app)` is exactly what Starlette itself calls internally). Verified live (not just imported): one process run connects a real Telegram MTProto session, starts exactly one APScheduler with exactly 4 jobs, starts exactly one Reddit `RedditClientManager`, and OSINT's SQLite-mode in-process worker pool — each exactly once, because each sub-app object is constructed exactly once and mounted exactly once. Restart-tested (ran the process twice): clean reconnect, no duplicate job registration, no errors.

## Two architectural questions from the original spec, resolved by direct code inspection (no code changes needed)

**Bluweb's MinIO — is it in the API response path?** No. Traced every `ArtifactStore` call site: `put_bytes` is called exactly once, in the crawl pipeline, to archive raw fetched bytes. `get_bytes` (the read method) is **never called anywhere in the codebase**. Every document/version/diff/changes endpoint (`bluweb_app/api/v1/documents.py`) reads the `content` column directly off the Postgres-backed ORM model. MinIO is a write-only, best-effort raw archival copy, invisible to API callers — the spec's Phase 5 concern ("the consuming application should receive extracted data in the response, not have it permanently stored merely for them") is already true today. No storage-abstraction redesign was needed; MinIO is left exactly as-is.

**OSINT's worker process — embed it or not?** OSINT's real `.env` runs SQLite mode (`OSINT_DATABASE_URL=sqlite:///./osint.db`), which already runs its worker pool in-process via its own lifespan — confirmed working inside the merged process (smoke test's `/osint/ready` returned `200 ready`). No change needed. If OSINT is ever switched to Postgres mode, its separate `osint_app.worker_main` process keeps running exactly as it does standalone today, alongside the unified API container — per the spec's own guidance not to force worker embedding just to satisfy "one process," since "one public API service" (already achieved) is the actual requirement, not "one OS process."

## Docker / deployment

New files only — no existing per-service Dockerfile/compose file was replaced, each still works standalone:

- `unified_api/Dockerfile` — one image, builds from the four existing renamed packages as siblings (build context is the repo root, not `unified_api/`).
- `unified_api/requirements.txt` — merged dependency set; core web-stack packages (fastapi/uvicorn/pydantic/pydantic-settings/sqlalchemy/httpx/alembic/pypdf) pinned to Bluweb's versions (verified newest, satisfies the other three's open ranges); everything else carried over from each service's own resolved pins.
- `docker-compose.yml` (repo root) — `unified-api` (the one app container, port 8000 bound to localhost only) + `nginx` (the only container publishing 80/443 to the world). Each service's `.env`/data directory is bind-mounted from its own existing repo path — nothing is copied into the image or merged into a shared file.
- `unified_api/nginx.conf` — single upstream (`unified-api:8000`) for every namespace, per the spec's explicit "nginx must not route by namespace to different containers" requirement. Generous timeouts set globally (300s) to cover OSINT's username-lookup and Bluweb's instant-search endpoints.
- **Not started by this compose file**: Bluweb's Postgres (external, pre-provisioned per its own docs — nothing changed there) and MinIO (this host already runs one persistently as the `ssor-minio` container; Bluweb's `.env` should point at it, not a new instance).

## What's still deferred

- Full characterization/regression test suite beyond each service's existing tests (spec Phase 1).
- Log-format unification across services (spec Phase 13) — each service's own `configure_logging()`/`basicConfig()` call still runs at import time; Python's `logging.basicConfig` is a no-op after the first call, so in the merged process only the first-imported service's format actually takes effect for anything using bare `logging.basicConfig`. This changes log line *formatting* only (not endpoint behavior/status codes/response bodies), observed but not fixed in this pass.
- `docs/UNIFIED_API_ENDPOINTS.md` / `UNIFIED_API_MIGRATION.md` (spec Phase 28/29) — the existing `docs/SINGLE_SERVER_ENDPOINT_MAP.md` has the full per-endpoint inventory already; only the namespace prefixes changed (`/scrape`, `/osint`, `/reddit`, `/telegram` replace the old per-subdomain scheme), everything else in that table is still accurate.
