# Reddit RSS — Deployment

RSS-only FastAPI service. No Reddit OAuth credentials. No database.

Default local / PM2 port in the monorepo: **8003**.

---

## Requirements

- Python 3.11+ (monorepo setup uses 3.12)
- Outbound HTTPS to `www.reddit.com` for `.rss` / `.atom` feeds
- Optional: `API_KEYS`, optional Reddit Preferences RSS `user`/`feed` token

---

## Install

```bash
cd reddit_server
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # optional; defaults work
```

From the monorepo root with PM2:

```bash
pm2 start ecosystem.config.cjs --only reddit
```

Or directly:

```bash
.venv/bin/uvicorn reddit_app.main:app --host 0.0.0.0 --port 8003
```

---

## Configuration

All settings are optional. See `.env.example`.

| Area | Variables |
|---|---|
| App | `HOST`, `PORT`, `LOG_LEVEL`, `LOG_JSON`, `CORS_ORIGINS`, `API_KEYS` |
| RSS client | `REDDIT_RSS_USER_AGENT`, `REDDIT_RSS_USER`, `REDDIT_RSS_FEED`, timeouts/retries, `REDDIT_RSS_EVENT_THRESHOLD` |
| Shared gateway | `REDDIT_RSS_GLOBAL_RATE`, `REDDIT_RSS_GLOBAL_BURST`, `REDDIT_RSS_MAX_QUEUE_WAIT_SECONDS`, cache TTL/size, client limit/window |

**Do not** set Reddit OAuth client id/secret — those variables are not used and
do not exist in this service.

`REDDIT_RSS_FEED` is treated as a secret (never logged, never returned).

---

## Health

| Probe | Path | Notes |
|---|---|---|
| Liveness | `GET /health` | Process up |
| Readiness | `GET /ready` | Service ready to accept RSS work |

There is no OAuth token probe — nothing to authenticate with Reddit.

---

## Scaling

Feed cache, global token bucket, and per-caller limiter are **process-local**.

- Prefer **one worker process** per public IP hitting Reddit, or accept that
  N workers multiply outbound RSS pressure.
- Horizontal scale for CPU is fine if each instance has its own Reddit-facing
  IP or you accept aggregate rate-limit risk.
- Do not run many replicas behind one NAT IP expecting each to get a full
  ~1 req/min Reddit budget independently without coordination.

---

## Security checklist

- [ ] Set `API_KEYS` before exposing beyond localhost
- [ ] Store `REDDIT_RSS_FEED` (if used) in a secret store, not in git
- [ ] Restrict network egress to Reddit HTTPS if your policy requires it
- [ ] Prefer reverse proxy TLS termination in front of uvicorn

---

## Verify

```bash
curl -i http://127.0.0.1:8003/health
curl -i http://127.0.0.1:8003/ready
curl -i -X POST http://127.0.0.1:8003/api/reddit/rss/monitor \
  -H 'Content-Type: application/json' \
  -d '{"keywords":["news"],"limit":5}'
```
