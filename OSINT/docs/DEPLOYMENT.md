# OSINT API Deployment Guide

This guide is based on the repository source of truth: `app/main.py`, `app/config.py`, `app/db.py`, `app/orchestrator.py`, `app/job_queue.py`, `docker-compose.yml`, `Dockerfile`, `alembic/`, `scripts/`, `.env.example`, and `README.md`.

The service has no authentication by design. Only deploy it on localhost, behind a trusted reverse proxy, or on a private network segment.

## Supported runtimes

| Mode | Database | Worker model | When to use |
|---|---|---|---|
| SQLite local | `sqlite:///./osint.db` | In-process worker pool inside the API process | Single server, zero extra infrastructure |
| PostgreSQL production | `postgresql+psycopg://...` | Separate API process plus one or more `python -m osint_app.worker_main` processes | Multi-process or horizontally scaled deployments |
| Docker compose | PostgreSQL | API container plus worker container(s) plus Postgres | Containerized production or staging |

## Runtime requirements

| Requirement | Value |
|---|---|
| Python | Python 3.12 is the repo's container/runtime baseline (`Dockerfile` uses `python:3.12-slim`) |
| Python for SearxNG | Python 3.10+ (`scripts/setup_searxng.sh` was verified on 3.12) |
| Package manager | `pip` inside a virtual environment |
| Database | SQLite for local mode, PostgreSQL for production mode |
| Search backend | Optional but recommended self-hosted SearxNG |

## OS packages

Install these on a fresh Ubuntu/Debian host:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl dnsutils build-essential
sudo apt-get install -y whois || true
```

Notes:

- `dnsutils` is required because `domain_recon` uses `nslookup`.
- `whois` is optional. If it is missing, domain lookups still work with DNS/NS/MX/SPF/DMARC only.
- `curl` is used by the startup and verification commands in the repository scripts.
- `build-essential` matches the container image and gives source builds a fallback toolchain if a wheel is unavailable.

## Environment variables

All configuration uses the `OSINT_` prefix from `app/config.py` and `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `OSINT_DATABASE_URL` | `sqlite:///./osint.db` | Database URL; selects SQLite local mode or PostgreSQL production mode |
| `OSINT_SEARXNG_URL` | `http://127.0.0.1:8890` in `.env.example` | Base URL for the self-hosted SearxNG instance used by search and `public_web` |
| `OSINT_SEARXNG_ENGINES` | `google,bing,brave,wikipedia` | Engine subset passed to SearxNG; empty string lets SearxNG use its own configured set |
| `OSINT_HOST` | `127.0.0.1` | Host binding used by `python -m osint_app.main` |
| `OSINT_PORT` | `8000` | Port binding used by `python -m osint_app.main` |
| `OSINT_WORKER_CONCURRENCY` | `4` | Worker loops per process in SQLite mode, and per standalone worker process in PostgreSQL mode |
| `OSINT_ADAPTER_TIMEOUT_SECONDS` | `45` | Per-adapter HTTP timeout |
| `OSINT_MAIGRET_TOP_SITES` | `300` | Top-ranked site cap for Maigret |
| `OSINT_PIVOT_ENABLED` | `true` | Enables automatic pivots |
| `OSINT_MAX_PIVOT_DEPTH` | `2` | Maximum pivot depth in deep mode |
| `OSINT_MAX_TOTAL_PIVOTS_PER_INVESTIGATION` | `15` | Hard cap per investigation |
| `OSINT_MAX_PIVOTS_PER_ENTITY` | `5` | Hard cap per parent entity |
| `OSINT_PIVOT_CONFIDENCE_THRESHOLD` | `0.5` | Minimum pivot confidence |
| `OSINT_MAX_INVESTIGATION_RUNTIME_SECONDS` | `600` | Wall-clock budget for one investigation |
| `OSINT_MAX_CONCURRENT_INVESTIGATIONS` | `10` | Global cap on queued/running investigations |
| `OSINT_SOURCE_MAX_CONCURRENCY` | `3` | Per-process cap on concurrent calls to the same source adapter |
| `OSINT_DOCUMENT_MINING_ENABLED` | `true` | Enables deep-mode document mining |
| `OSINT_MAX_DOCUMENTS_PER_INVESTIGATION` | `10` | Cap on mined documents per investigation |
| `OSINT_DOCUMENT_FETCH_TIMEOUT_SECONDS` | `15` | Fetch timeout for document mining |
| `OSINT_MAX_DOCUMENT_BYTES` | `10485760` | Maximum fetched document size, 10 MB |
| `OSINT_ADAPTER_MAX_RETRIES` | `2` | Retry count for retryable `SourceUnavailable` errors |
| `OSINT_ADAPTER_RETRY_BACKOFF_SECONDS` | `0.5` | Initial exponential backoff between retries |
| `OSINT_INVESTIGATION_RETENTION_DAYS` | empty | Optional investigation retention window |
| `OSINT_AUDIT_LOG_RETENTION_DAYS` | empty | Optional audit-log retention window |
| `OSINT_DB_POOL_SIZE` | `5` | PostgreSQL connection pool size |
| `OSINT_DB_MAX_OVERFLOW` | `10` | PostgreSQL connection pool overflow |
| `OSINT_DB_POOL_TIMEOUT` | `30` | PostgreSQL pool checkout timeout |
| `OSINT_DB_POOL_RECYCLE_SECONDS` | `1800` | PostgreSQL connection recycle interval |
| `OSINT_INVESTIGATION_LEASE_SECONDS` | `120` | PostgreSQL worker lease duration before another worker can reclaim a job |

## API port selection

Do not assume port `8000` is free. Pick an available port before starting the API.

1. Choose a candidate port, for example `8080`, `8081`, or another site-specific value.
2. Check whether something is already listening on that port:

```bash
ss -ltn "sport = :8080"
```

If the command prints a listener line, the port is occupied.

3. If the port is occupied, try the next candidate until you find a free one.
4. Set `OSINT_PORT` to the chosen port in `.env` or export it in your service environment.

If you run behind a reverse proxy, bind the API to `127.0.0.1` and expose the proxy instead. If you run the API directly, bind to `0.0.0.0` only when network controls already restrict access.

## SearxNG requirements and setup

SearxNG is optional but recommended. Without it:

- `GET /api/v1/search`
- `POST /api/v1/search`
- the `public_web` adapter used by investigations

all report unavailable instead of fabricating results.

### Required SearxNG port

The repository's native SearxNG scripts use port `8890` and bind to `127.0.0.1`.

If you change the SearxNG port, update `OSINT_SEARXNG_URL` to the exact base URL, for example `http://127.0.0.1:8890`.

### Setup

```bash
./scripts/setup_searxng.sh
./scripts/start_searxng.sh
```

`setup_searxng.sh` clones SearxNG into `searxng-src/`, creates a dedicated virtual environment there, installs it, and writes `searxng-src/settings.yml` with JSON output enabled and the local limiter disabled. `start_searxng.sh` starts the service in the foreground.

### SearxNG engine configuration

The API client uses `OSINT_SEARXNG_URL` and, by default, sends `OSINT_SEARXNG_ENGINES=google,bing,brave,wikipedia` to keep requests on the subset verified by `scripts/verify_searxng_engines.py`.

If you want SearxNG to use its own engine configuration, set `OSINT_SEARXNG_ENGINES=` to an empty string.

### Verify engines

After SearxNG is running, check which engines actually return results:

```bash
./.venv/bin/python scripts/verify_searxng_engines.py
```

## SQLite local runtime

Use SQLite for the simplest single-server deployment.

### Steps

```bash
git clone <repo-url> osint
cd osint
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` if needed. At minimum, set:

- `OSINT_PORT` to a free API port
- `OSINT_HOST` to `127.0.0.1` if you will proxy it, or `0.0.0.0` if you are binding directly and have firewall controls in place
- `OSINT_SEARXNG_URL` if SearxNG is running on a different host or port

Then start the stack:

```bash
./scripts/start_searxng.sh
./scripts/start_local.sh
```

Or start the API directly:

```bash
./.venv/bin/python -m osint_app.main
```

SQLite mode uses the in-process worker pool started by `osint_app.main`'s lifespan hook. No separate worker process is needed.

### SQLite permissions

Make sure the API user can write to:

- the repository directory
- `.venv/`
- `osint.db`
- `searxng-src/`
- `searxng.log` if you use the local SearxNG helper script

## PostgreSQL production runtime

Use PostgreSQL when you want multiple API or worker processes.

### Database URL

Set `OSINT_DATABASE_URL` to a PostgreSQL URL:

```bash
export OSINT_DATABASE_URL='postgresql+psycopg://osint:osint_dev_pw@localhost:5432/osint'
```

The repository's Docker compose file uses the same shape internally, with the host name `postgres` inside the compose network.

### Migrations

The app does not auto-run migrations on startup. Apply them before starting the API:

```bash
./.venv/bin/alembic upgrade head
```

`alembic/env.py` reads `OSINT_DATABASE_URL` from the application settings, so the migration target always matches the API configuration.

### API and worker processes

Start the API:

```bash
./.venv/bin/uvicorn osint_app.main:app --host 0.0.0.0 --port "$OSINT_PORT"
```

Start the worker process in a separate terminal, systemd unit, or container:

```bash
./.venv/bin/python -m osint_app.worker_main
```

`osint_app.worker_main` only works against PostgreSQL. It exits immediately if `OSINT_DATABASE_URL` still points at SQLite.

`OSINT_WORKER_CONCURRENCY` controls how many worker loops each worker process starts. Increase it if you want more polling loops inside one process, or run more worker processes if you want horizontal scale.

### Docker compose path

The repository also supports:

```bash
docker compose up -d --build
```

That stack starts PostgreSQL, runs migrations, then starts the API and worker containers. It maps the API container's port `8000` to host port `8000` by default, so change the host-side mapping if that port is already taken.

## Domain lookup behavior and WHOIS fallback

`domain_recon` always performs DNS/NS, MX, SPF, and DMARC checks when `dnsutils` is installed.

WHOIS is optional and detected at runtime with `shutil.which("whois")`.

| Situation | Behavior |
|---|---|
| `whois` installed | DNS + MX + SPF + DMARC + WHOIS |
| `whois` unavailable | DNS + MX + SPF + DMARC |

Missing `whois` must never block deployment.

## Filesystem permissions and service ownership

Use a dedicated service account if possible. That account needs write access to the application checkout, the virtual environment, SQLite database files if you use them, and any logs you expect the process to create.

For PostgreSQL deployments, the database server owns its own data directory or managed service account. The application only needs network access to the database and read/write permission to its own working directory.

## Systemd and process management

The repository does not ship systemd units, but systemd is an appropriate production supervisor on Ubuntu/Debian.

Recommended shape:

- one `osint-api.service` running `./.venv/bin/python -m osint_app.main` for SQLite mode, or `./.venv/bin/uvicorn osint_app.main:app ...` for PostgreSQL mode
- one `osint-worker.service` running `./.venv/bin/python -m osint_app.worker_main` for PostgreSQL mode only
- one `osint-searxng.service` if you do not use the helper scripts interactively

Keep the API and worker in separate units when using PostgreSQL so restarts and log handling stay isolated.

## Start order

The deployment flow should be:

1. Detect/configure an available API port.
2. Configure the API environment.
3. Start SearxNG if you want search/public-web coverage.
4. Start the API.
5. Verify `/ready`.
6. Verify `/api/v1/sources/health`.
7. Run endpoint smoke tests.

## Verification

### Health and readiness

```bash
curl -i "http://127.0.0.1:${OSINT_PORT}/health"
curl -i "http://127.0.0.1:${OSINT_PORT}/ready"
```

Expected:

- `/health` returns `200` with `{"status":"ok"}`
- `/ready` returns `200` with `{"status":"ready"}` when the database is reachable
- `/ready` returns `503` with `{"status":"not_ready", "reason": ...}` when the database cannot be reached

### Source health

```bash
curl -i "http://127.0.0.1:${OSINT_PORT}/api/v1/sources/health"
```

Confirm that each active adapter is present and that `searxng` is `working` if you configured `OSINT_SEARXNG_URL`.

### Smoke tests

Run a small, representative set:

```bash
curl -i "http://127.0.0.1:${OSINT_PORT}/api/v1/search?q=open%20source%20intelligence&page=1"

curl -i -X POST "http://127.0.0.1:${OSINT_PORT}/api/v1/phone/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+14155552671"}'

curl -i -X POST "http://127.0.0.1:${OSINT_PORT}/api/v1/email/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"email":"example@example.com"}'

curl -i -X POST "http://127.0.0.1:${OSINT_PORT}/api/v1/username/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"username":"example_username"}'

curl -i -X POST "http://127.0.0.1:${OSINT_PORT}/api/v1/person/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"person_name":"Linus Torvalds"}'

curl -i -X POST "http://127.0.0.1:${OSINT_PORT}/api/v1/domain/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"domain":"anthropic.com"}'

curl -i -X POST "http://127.0.0.1:${OSINT_PORT}/api/v1/investigations" \
  -H 'Content-Type: application/json' \
  -d '{"input_identifier":"example@example.com","mode":"standard"}'
```

For investigations, poll the returned investigation ID until the status becomes terminal:

```bash
curl -i "http://127.0.0.1:${OSINT_PORT}/api/v1/investigations/<id>/status"
```

### Metrics

```bash
curl -i "http://127.0.0.1:${OSINT_PORT}/metrics"
```

The metrics endpoint is Prometheus text exposition, per-process.

## Logs

| Component | Where to look |
|---|---|
| API started by `python -m osint_app.main` | Process stdout/stderr or systemd journal |
| API started by Docker compose | `docker compose logs -f api` |
| Worker started by `python -m osint_app.worker_main` | Process stdout/stderr or systemd journal |
| Worker started by Docker compose | `docker compose logs -f worker` |
| PostgreSQL container | `docker compose logs -f postgres` |
| Local SearxNG helper | `searxng.log` |

The API also writes structured request rows to the `audit_log` table.

## Restart and recovery

- SQLite mode: restart the API process. The in-process worker pool comes back with the API.
- PostgreSQL mode: restart the API and worker processes separately. Investigations that were in flight are stored in the database.
- If a PostgreSQL worker dies mid-job, its lease expires after `OSINT_INVESTIGATION_LEASE_SECONDS`, and another worker can reclaim the investigation automatically.
- If you change `OSINT_DATABASE_URL`, `OSINT_SEARXNG_URL`, or any schema-affecting settings, rerun migrations if needed before restarting the API.

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `/ready` returns `503` | Database unreachable | Database URL, credentials, running DB service |
| `/api/v1/search` returns `503` | SearxNG missing or unreachable | `OSINT_SEARXNG_URL`, SearxNG process, network reachability |
| Domain lookups return unavailable | `dnsutils` missing | `nslookup` installed and on `PATH` |
| WHOIS data missing | `whois` not installed or timed out | Optional by design |
| Investigations stay queued | No worker running or worker cannot reach DB | API vs worker process separation, PostgreSQL lease/reclaim behavior |
| Port already in use | Another service is listening | `ss -ltn "sport = :PORT"` |

## Deployment verification checklist

- [ ] Chosen API port is free and recorded in `OSINT_PORT`
- [ ] `.env` created from `.env.example`
- [ ] `requirements.txt` installed in a virtual environment
- [ ] SearxNG running, if required, on the documented port
- [ ] `OSINT_SEARXNG_URL` points to the actual SearxNG base URL
- [ ] Database URL matches the chosen runtime mode
- [ ] Alembic migrations applied for PostgreSQL deployments
- [ ] API starts successfully
- [ ] `/ready` returns `200`
- [ ] `/api/v1/sources/health` returns the expected adapter rows
- [ ] Smoke tests for search, direct lookups, and investigations pass
- [ ] Firewall or reverse-proxy rules prevent raw public exposure
