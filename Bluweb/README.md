# Bluweb — Self-hosted Web Scrape & Crawl API

100% self-hosted: **httpx + Playwright + open-source extractors**.  
No paid scrape APIs, proxies, or CAPTCHA services. You host the process;
your app stores the JSON.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/ready` | Process ready |
| POST | `/api/v1/preflight` | Can we scrape this URL? (robots/sitemap/JS heuristic) |
| POST | `/api/v1/scrape` | One URL → extracted content |
| POST | `/api/v1/crawl` | Multi-page crawl (BFS, sitemap/RSS seeds) |
| GET | `/api/v1/crawl/{id}` | Poll crawl status + pages |
| POST | `/api/v1/crawl/{id}/cancel` | Stop a running crawl |

OpenAPI: `/docs`

## What you get (max open-source capability)

- SSRF-safe fetching  
- HTTP first, Playwright when JS heuristic says so (`use_browser` to force)  
- Page-type extraction (article, forum, listing, index, …)  
- Multi-page: `max_pages` (≤500), `max_depth` (≤5), same-domain scope  
- Sitemap + RSS seed discovery (free, self-hosted)  
- Soft-block / quality signals inside extractors  

**Not included (would need paid APIs):** residential proxies, CAPTCHA solvers,
Cloudflare bypass as a service. Hard-blocked sites return errors — by design.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m playwright install chromium
cp .env.example .env
.venv/bin/python -m uvicorn bluweb_app.main:app --host 0.0.0.0 --port 8000
```

PM2 (repo root, port **8001**): `pm2 start ecosystem.config.cjs --only bluweb`

## Examples

```bash
# Single page
curl -s -X POST http://127.0.0.1:8001/api/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.org"}'

# Multi-page crawl (async — poll GET)
curl -s -X POST http://127.0.0.1:8001/api/v1/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.org","max_pages":10,"max_depth":1}'

# Or wait up to 60s for the full result in one response
curl -s -X POST http://127.0.0.1:8001/api/v1/crawl \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.org","max_pages":5,"max_depth":1,"wait_seconds":60}'
```

Copy `pages[].content` / metadata into **your** database. Jobs are kept
in memory ~1 hour after completion, then dropped.
