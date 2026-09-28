# Saga APIs

One git repository for all four FastAPI services:
[ashish-ratna-bcss/saga-apis](https://github.com/ashish-ratna-bcss/saga-apis.git).
Clone it once, dump the whole tree on a host, then run each service as its
own process. There is no shared API gateway.

```bash
git clone https://github.com/ashish-ratna-bcss/saga-apis.git
cd saga-apis
```

Each service has its own package, `.env`, virtualenv, and PM2 process.

| Service | Directory | PM2 name | Port | Health |
|---|---|---|---|---|
| Bluweb | `Bluweb/` | `bluweb` | 8001 | `http://127.0.0.1:8001/health` |
| OSINT | `OSINT/` | `osint` | 8002 | `http://127.0.0.1:8002/health` |
| Reddit | `reddit_server/` | `reddit` | 8003 | `http://127.0.0.1:8003/health` |
| Telegram | `telegram_poc/` | `telegram` | 8004 | `http://127.0.0.1:8004/health` |

Telegram also exposes `/ready`. Bluweb also exposes `/health/ready`.

## One-time setup

Python 3.12+, and a `.venv` inside each service directory. Copy each
service's `.env.example` to `.env` and fill in real values where required.

```bash
# Bluweb
cd Bluweb
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
cd ..

# OSINT
cd OSINT
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..

# Reddit
cd reddit_server
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..

# Telegram
cd telegram_poc
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd ..
```

Docker Compose files inside a service directory are only for that service's
optional dependencies (Bluweb MinIO, OSINT Postgres/SearxNG). PM2 does not
start them.

## Run with PM2

From this directory:

```bash
pm2 start ecosystem.config.cjs
pm2 start ecosystem.config.cjs --only bluweb   # one service
pm2 status
pm2 logs
pm2 stop all
```

Each app is a single forked uvicorn process (required: Bluweb and Telegram
run in-process schedulers; Reddit's RSS cache and OSINT's SQLite workers
are process-local).

OSINT SQLite (the default) runs workers inside the API process. If
`OSINT_DATABASE_URL` is Postgres, start a separate worker from the commented
`osint-worker` block in `ecosystem.config.cjs`.
