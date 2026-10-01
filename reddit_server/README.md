# Reddit RSS Provider Service

Standalone FastAPI service that monitors Reddit through **public, unauthenticated
RSS/Atom feeds** only (`/api/reddit/rss/*`). No Reddit OAuth client, no client
id/secret, and no official Reddit API calls anywhere in this codebase.

It works from a bare checkout with no `.env`. Optional settings (API keys,
RSS feed token, cache TTL) are in
[`.env.example`](.env.example).

Consumer-facing contract: [INTEGRATION.md](INTEGRATION.md).  
Deploy notes: [DEPLOYMENT.md](DEPLOYMENT.md).

---

## What it owns

- Fetching and parsing Reddit `.rss` / `.atom` feeds
- Keyword / event / profile monitoring over those feeds
- Feed-cache coalescing, and waiting when Reddit's own RSS quota headers say so
- Stable JSON shapes for callers (SOC Eye or anything else)

## What it does not own

- Polling schedule, storage, deduplication across requests, alerts, UI
- Reddit account login or OAuth

---

## Quick start

```bash
cd reddit_server
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn reddit_app.main:app --host 0.0.0.0 --port 8003
```

```bash
curl -s http://127.0.0.1:8003/health
curl -s -X POST http://127.0.0.1:8003/api/reddit/rss/monitor \
  -H 'Content-Type: application/json' \
  -d '{"keywords": ["protest"], "subreddits": ["worldnews"], "sort": "new", "time_range": "day", "limit": 25}'
```

If `API_KEYS` is set, send `X-API-Key: <key>` on every `/api/reddit/rss/*` call.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness |
| POST | `/api/reddit/rss/monitor` | Keyword monitoring |
| GET | `/api/reddit/rss/search` | Same as `/monitor`, query-string form |
| POST | `/api/reddit/rss/user` | Profile activity via user RSS |
| POST | `/api/reddit/rss/event` | Default event strategy + `event_signal` |

OpenAPI: `http://127.0.0.1:8003/docs` or `/openapi.json`.

---

## Tests

```bash
.venv/bin/pytest
```

Live Reddit smoke tests are skipped unless explicitly enabled (see
`tests/test_reddit_rss_live_smoke.py`).

---

## Architecture

```
HTTP  →  reddit_app/api/routes_reddit_rss.py
         reddit_app/services/reddit_rss_service.py
         reddit_app/reddit/rss_client.py   (+ feed_cache)
         www.reddit.com/*.rss
```

Stateless: no database, no background poll loop.
