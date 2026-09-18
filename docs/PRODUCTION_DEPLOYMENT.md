# Production Deployment — Unified API on the real server (Milestone 6)

Server: `1930` (SSH alias) → `100.49.109.96` / `ip-172-31-81-195`. **This is a shared, multi-tenant production box** — it also hosts an unrelated vLLM/Qwen inference gateway and three real client-facing PM2/nginx-served sites (`odisha.blurasaga.com`, `delhipolice.blurasaga.com`, `uttarakhandpolice.blurasaga.com`). Never touch those containers, PM2 entries, or the auto-generated `/etc/nginx/sites-available/blurasaga` file (managed by a separate `deploy/sites.json` → `render_nginx.js` pipeline — do not hand-edit).

## 1. Server prerequisites
Ubuntu 26.04, Docker 29.7.2 + Compose v5.5.0, 8 vCPU / 30GB RAM, 27GB free disk at deployment time. Passwordless sudo available for the `ubuntu` user (used only for `systemctl stop bluweb.service`).

## 2. Deployment directory
`/home/ubuntu/unified-api/` (not `/data/...` — that top-level directory is root-owned; per Milestone 3's own rule, the pre-existing `/data/bluweb` and `/data/telegram_poc` directories are left completely untouched, not overwritten).

Layout:
```
/home/ubuntu/unified-api/
  Bluweb/{bluweb_app,migrations,alembic.ini,pyproject.toml,.env}
  OSINT/{osint_app,alembic,alembic.ini,requirements.txt}
  reddit_server/{reddit_app,pyproject.toml,requirements.txt}
  telegram_poc/{telegram_app,requirements.txt,.env}
  unified_api/{app,Dockerfile,requirements.txt,nginx.conf}
```

## 3. Code transfer procedure
From the dev machine:
```
rsync -az --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
  --exclude='.pytest_cache' --exclude='.mypy_cache' --exclude='.ruff_cache' \
  --exclude='*.egg-info' --exclude='data' --exclude='.benchmarks' --exclude='.claude' \
  --exclude='storage' --exclude='osint.db*' --exclude='searxng-src' --exclude='.env' \
  Bluweb OSINT reddit_server telegram_poc unified_api docs \
  1930:/home/ubuntu/unified-api/
```
**Known gap, already hit once**: a bare `--exclude='storage'` also matches `Bluweb/bluweb_app/services/storage/` (a real package, not just the `Bluweb/storage/` Crawlee runtime dir it was meant to exclude). If re-running this sync, verify `Bluweb/bluweb_app/services/storage/artifact_store.py` actually landed, or use an anchored pattern (`--exclude='/Bluweb/storage/'`) instead.

## 4. Environment configuration
Each service's real `.env` must live at exactly the path its own `BASE_DIR`-anchored config expects — **not** a generic top-level file, or two services' same-named variables (`DATABASE_URL` exists in both Bluweb's and Telegram's env) will collide via the OS environment:
- `Bluweb/.env` — copied server-side from the pre-existing `/data/bluweb/.env` (real remote Postgres at `103.211.36.242:5432`, real MinIO creds), plus `ARCHIVAL_ENABLED=true` appended.
- `telegram_poc/.env` — copied server-side from `/data/telegram_poc/.env`, then the path-valued variables rewritten to absolute paths pointing at the **existing real data**, not a fresh copy: `DATABASE_URL=sqlite+aiosqlite:////data/telegram_poc/data/telegram_service.db`, `TELEGRAM_SESSION_PATH=/data/telegram_poc/data/telegram_service.session`, `MEDIA_STORAGE_PATH`, `RAW_EVIDENCE_STORAGE_PATH`, `AVATAR_CACHE_PATH`, `MESSAGE_MEDIA_CACHE_PATH` all likewise pointed at `/data/telegram_poc/data/...`. `RAW_EVIDENCE_ENABLED=true` appended.
- **Known gap, already hit once**: appending a line with `echo ... >> .env` when the file doesn't already end in a newline concatenates onto the last line instead of adding a new one (corrupted `API_PORT=8001AVATAR_CACHE_PATH=...` on one line). Always verify with `tail -c1 file | xxd` shows `0a` (newline) before appending, or use `printf '\n%s\n' "..." >> file`.
- OSINT / reddit_server: no real `.env` existed on the server (these are new services here) — both run on their code defaults (OSINT: fresh SQLite inside the container; reddit_server: OAuth unconfigured, `503` on those routes, RSS routes fully usable).
- **Never `cat`/`tail` a `.env` file that might contain secrets to inspect it** — use `grep -oE '^[A-Z_]+=' file` (names only) or `sed`/targeted `grep '^KEY='` with the value piped straight into another command, never displayed. This was violated once for `Bluweb/.env` (an `HF_TOKEN` value was displayed via `cat -A`) — rotate that token if this matters for your threat model.

## 5. Docker commands
Build (from `/home/ubuntu/unified-api/`, ~10-12 min, layer-cached on rebuild):
```
docker build -f unified_api/Dockerfile -t unified-saga-api:latest .
```
Run (production — `--network host` so the container reaches the real remote Postgres and the host-published MinIO port without extra networking config; `--restart unless-stopped` for resilience without a new systemd unit):
```
docker run -d --name unified-saga-api --network host --restart unless-stopped \
  -v /home/ubuntu/unified-api/Bluweb/.env:/srv/Bluweb/.env:ro \
  -v /home/ubuntu/unified-api/telegram_poc/.env:/srv/telegram_poc/.env:ro \
  -v /data/telegram_poc/data:/data/telegram_poc/data \
  unified-saga-api:latest \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```
MinIO (`bluweb-minio-1`) must be running independently of this container — it's a separate pre-existing container, started with `docker start bluweb-minio-1` if ever stopped.

## 6. Nginx configuration
**Not touched.** Per explicit instruction, the unified API is exposed the same way the old Bluweb was — directly on port 8000, no reverse proxy, no nginx involvement, no changes to the shared/auto-generated nginx config. If a reverse proxy is wanted later, add a **new, separate** `sites-available` file (never hand-edit `blurasaga`) proxying to `127.0.0.1:8000`.

## 7. Health checks
```
GET /scrape/health        GET /scrape/health/ready
GET /osint/health         GET /osint/ready
GET /reddit/health        (no /ready — reddit_server's is config-only, same as /health effectively)
GET /telegram/health      GET /telegram/ready
```

## 8. Four-namespace verification
Confirmed live: `/scrape/*` (37 paths), `/osint/*` (19), `/reddit/*` (19), `/telegram/*` (42) — matches Milestone 3's OpenAPI-derived counts exactly (117 unique paths / 145 method-level rows).

## 9. Telegram singleton verification
`GET /telegram/api/telegram/status` → `{"connected":true,"authenticated":true,"account":{"id":"8937684807",...}}` — the **real, already-authenticated production account**, reused via the mounted `/data/telegram_poc/data` volume, with zero new logins performed. `docker top unified-saga-api` shows exactly one process.

## 10. Scheduler verification
Every startup (initial run + 4 restart cycles total across the test and production containers) logged exactly 4 `Added job` lines and one `Scheduler started` — never duplicated.

## 11. Restart procedure
```
docker restart unified-saga-api
# then re-check: docker logs unified-saga-api | grep -c "Added job"   (expect 4)
#               curl -s localhost:8000/scrape/health
```

## 12. Rollback procedure
1. `docker stop unified-saga-api` (or `docker rm -f unified-saga-api` — the image stays, nothing is lost).
2. `sudo systemctl start bluweb.service` — brings back `bluweb-api-1` + `bluweb-minio-1` on port 8000 exactly as before (systemd unit was only *stopped*, never disabled or removed).
3. Verify: `curl localhost:8000/health` (old Bluweb's own unprefixed health route) and `curl localhost:8000/api/v1/documents` (or equivalent) return as before.
4. Telegram was never running as an old service in this cutover (found already-stopped at discovery time) — nothing to roll back there beyond stopping the unified container, which releases the session file lock.
5. No nginx change was made, so there is nothing to revert there.

## 13. Old-service cleanup procedure (NOT performed — explicit approval required first)
`bluweb-api-1` is stopped (`docker ps -a` shows `Exited (0)`) but **not removed**; `/data/bluweb` and `/data/telegram_poc` are fully intact, including all persistent data. Deletion of the old container/image or either directory was not requested and was not performed. If/when approved: remove the stopped container (`docker rm bluweb-api-1`) and optionally the image — **never** delete `/data/bluweb` or `/data/telegram_poc` (real Postgres connection info, MinIO creds, and the only copy of the Telegram session/SQLite data respectively) without a separate, explicit, data-specific approval.

## 14. Persistent-data protection
- Telegram session + SQLite: still physically at `/data/telegram_poc/data/` (bind-mounted into the unified container, never copied/duplicated).
- Bluweb Postgres: remote, at `103.211.36.242:5432`, entirely unaffected by anything in this deployment — the unified container only connects to it, same as the old one did.
- Bluweb MinIO: same pre-existing `bluweb-minio-1` container/volume, untouched (just needed a manual restart after being stopped as a side effect of `bluweb.service stop`, since MinIO was part of the *same* docker-compose project).

## 15. Troubleshooting
- **Container won't start, `ModuleNotFoundError`**: almost certainly the rsync `storage` over-exclusion (§3) — check the file landed, rebuild.
- **Pydantic `ValidationError` on a numeric field with a garbled string value**: a `.env` concatenation bug from a no-trailing-newline append (§4) — inspect with `cat -A` (names/structure only, be mindful of secrets) and re-split the line.
- **MinIO errors / crawl stuck at `status: running` forever**: pre-existing gap in Bluweb's own `crawl_engine.py` (no try/except around the MinIO call) — confirmed in Milestones 3 and 5, not fixed (unrelated to this deployment). If MinIO is down and archival isn't needed, set `ARCHIVAL_ENABLED=false` in `Bluweb/.env` and restart the container.
- **Telegram `authenticated: false` unexpectedly**: check the bind-mount for `/data/telegram_poc/data` is present and `TELEGRAM_SESSION_PATH` in `telegram_poc/.env` is the correct absolute path — a relative path or a missing mount would silently start a *fresh* anonymous session instead of erroring.

## 16. Final production architecture (current state)
```
Internet
   │
   ▼
100.49.109.96:8000  ──────────────►  unified-saga-api (Docker, --network host, --restart unless-stopped)
                                          ├── /scrape/*    → Bluweb    (real remote Postgres + real MinIO)
                                          ├── /osint/*     → OSINT     (fresh local SQLite)
                                          ├── /reddit/*    → reddit_server (stateless, OAuth unconfigured, RSS live)
                                          └── /telegram/*  → telegram_poc (real, already-authenticated session, reused in place)

Old bluweb-api-1 + bluweb-minio-1 (compose stack): bluweb-api-1 stopped, not removed; bluweb-minio-1 restarted standalone (still needed by the unified container).
Old Telegram deployment: was never running at discovery time; code + real session/DB left exactly where they were, now in active use via bind mount.
Nginx: untouched, still serves the three unrelated blurasaga sites plus the default vLLM route; port 8000 was never behind it (matches pre-existing Bluweb setup).
```
