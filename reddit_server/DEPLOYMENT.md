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
| Shared gateway | `REDDIT_RSS_CACHE_TTL_SECONDS`, `REDDIT_RSS_CACHE_MAX_ENTRIES`, `REDDIT_RSS_GATE_SIZE` |

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

The feed cache is **process-local**. Outbound quota is whatever Reddit returns
in `X-Ratelimit-*` for this host (and for `REDDIT_RSS_USER` / `REDDIT_RSS_FEED`
when those are set). This service does not add a tighter cap.

- One worker process is enough for the cache to coalesce identical feeds.
- Extra workers each keep their own cache and each count against Reddit's quota
  for the shared public IP.

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
