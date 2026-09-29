# Reddit RSS — Integration Guide

Consumer-facing contract for the **RSS-only** Reddit provider. There is no
OAuth transport and no `/api/reddit/*` routes outside `/api/reddit/rss/*`.

Base URL examples assume local PM2 port `8003`. Adjust for your deployment.

---

## 1. Auth (this service, not Reddit)

Reddit credentials are never required.

Optional service auth: set `API_KEYS` (comma-separated). Every
`/api/reddit/rss/*` request then needs:

```http
X-API-Key: <one of the configured keys>
```

Empty `API_KEYS` disables auth (local only — do not expose that beyond localhost).

---

## 2. Error envelope

All failures use:

```json
{ "error": { "code": "SOME_CODE", "message": "...", "details": {} } }
```

Treat `error.code` as the stable contract; `message` is human-readable.

| HTTP | `code` | Meaning |
|---|---|---|
| 401 | `UNAUTHORIZED` | Missing/invalid `X-API-Key` when auth is enabled |
| 422 | `REDDIT_RSS_INVALID_QUERY` | Bad query/keywords/dates/sort/limit |
| 422 | `REDDIT_RSS_INVALID_SUBREDDIT` | Bad or too many subreddit names |
| 403 | `REDDIT_RSS_FORBIDDEN` | Reddit blocked the request (IP/reputation) |
| 429 | `REDDIT_RSS_RATE_LIMITED` | Reddit RSS rate limit exhausted |
| 429 | `REDDIT_RSS_CLIENT_RATE_LIMITED` | This caller exceeded its budget on this service |
| 502 | `REDDIT_RSS_UNAVAILABLE` | Upstream failure / oversized body |
| 502 | `REDDIT_RSS_PARSE_ERROR` | Non-XML / unparseable feed body |
| 504 | `REDDIT_RSS_TIMEOUT` | Timed out talking to Reddit |
| 504 | `REDDIT_RSS_QUEUE_TIMEOUT` | Shared outbound budget wait exceeded |

Empty success is still `200` with `"count": 0, "posts": []`.

---

## 3. Shared-gateway behaviour

Many callers → one process → one Reddit-facing IP (~1 req/min unauthenticated).

1. **Global token bucket** — paces every outbound Reddit fetch (`REDDIT_RSS_GLOBAL_RATE`).
2. **Feed cache + coalescing** — identical feed URLs within TTL share one fetch.
3. **Per-caller limiter** — `REDDIT_RSS_CLIENT_LIMIT` / window on this router.

All three are process-local. Filtering (`keywords`, `exclude`, …) always runs
fresh per request after the shared cache.

Optional higher Reddit quota: set `REDDIT_RSS_USER` + `REDDIT_RSS_FEED` from
reddit.com → Preferences → RSS Feeds. These are not OAuth; the feed token is
still treated as a secret (never logged or returned).

---

## 4. Endpoints

### `POST /api/reddit/rss/monitor`

```bash
curl -X POST http://127.0.0.1:8003/api/reddit/rss/monitor \
  -H 'Content-Type: application/json' \
  -d '{
        "keywords": ["protest", "strike"],
        "subreddits": ["India", "worldnews"],
        "sort": "new",
        "time_range": "day",
        "limit": 50
      }'
```

Require `query` or `keywords`. Optional: `strong_keywords`, `exclude`,
`match_field` (`title`|`full`), `min_matches`, `from_date`/`to_date` (ISO 8601,
client-side), `limit` (1–100).

Every response includes `"transport": "rss"` and `"authenticated": false`.

### `GET /api/reddit/rss/search`

Same use-case as `/monitor` (one code path):

```bash
curl 'http://127.0.0.1:8003/api/reddit/rss/search?keywords=protest&keywords=strike&subreddit=India+worldnews&sort=new&time=day'
```

### `POST /api/reddit/rss/user`

Profile activity for a username via public user RSS.

### `POST /api/reddit/rss/event`

Default subreddit/keyword strategy plus `event_signal`:

```json
{
  "event_signal": {
    "detected": true,
    "post_count": 24,
    "unique_subreddits": 5,
    "matched_keywords": ["protest"],
    "threshold": 10
  }
}
```

`detected` is `post_count >= REDDIT_RSS_EVENT_THRESHOLD` for **this response only**
— not a confirmed real-world event and not a cross-request trend.

---

## 5. Deduplication

Within one response, posts are deduped by id/guid/URL. Cross-request
deduplication is the caller's job; this service is stateless.

---

## 6. Client sketch (Python)

```python
import httpx

async def monitor(keywords: list[str], subreddits: list[str] | None = None) -> dict:
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8003", timeout=30.0) as client:
        r = await client.post(
            "/api/reddit/rss/monitor",
            json={
                "keywords": keywords,
                "subreddits": subreddits or [],
                "sort": "new",
                "time_range": "day",
            },
            # headers={"X-API-Key": "..."},
        )
        r.raise_for_status()
        return r.json()
```

Typed clients: `curl http://127.0.0.1:8003/openapi.json`.

---

## 7. Compatibility

Additive API: new optional request/response fields should not break existing
callers. Pin `/openapi.json` if you regenerate clients deliberately.
