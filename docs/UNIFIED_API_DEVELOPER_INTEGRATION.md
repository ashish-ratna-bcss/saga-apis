# Unified Saga API — Developer Integration Guide

This guide documents every client-facing endpoint of the deployed Unified Saga API, generated from the live OpenAPI schemas and verified implementation behavior. Use it to integrate directly — no source code access required.

## 1. Base URL

All endpoints are reached through one base URL:

```
{BASE_URL}
```

Configure `{BASE_URL}` once in your application (an environment variable or config value) and build every request as `{BASE_URL}` + the endpoint path shown below. The same base URL serves all four modules, distinguished by path prefix:

```
{BASE_URL}/telegram/...
{BASE_URL}/reddit/...
{BASE_URL}/osint/...
{BASE_URL}/scrape/...
```

Example: if `BASE_URL=https://api.example.com`, then `GET /scrape/api/v1/documents` is called at `https://api.example.com/scrape/api/v1/documents`.

## 2. Quick Start

```
Your Application
      │  HTTP request ({BASE_URL}/<module>/<path>)
      ▼
 Unified Saga API
      │  JSON response
      ▼
Your Application
```

Minimal working example (Python):

```python
import requests

BASE_URL = "https://api.example.com"  # configure this once

resp = requests.get(f"{BASE_URL}/reddit/health", timeout=10)
resp.raise_for_status()
print(resp.json())  # {"status": "ok"}
```

How to consume the API:
1. **Set `BASE_URL`** to wherever the Unified API is reachable for your environment.
2. **Send requests** with the standard `requests` (Python) / `fetch` (JS) / `curl` — every endpoint is plain HTTP + JSON, no special client library needed.
3. **Parse responses** as JSON (`response.json()` / `await response.json()`). Every success response is `application/json`.
4. **Handle errors** by checking the HTTP status code first (see §11 and §12), then reading the JSON error body for detail.
5. **Use IDs returned by APIs**: several endpoints return an identifier (`investigation_id`, `crawl_id`, `preflight_id`, `source_id`, `document_id`, `job_id`) that you pass to a *different* endpoint later to check status or fetch results — this is how the asynchronous workflows (§4, §7, §9) work. Store these IDs in your own application if you need to look them up again.

---

# 3. Telegram API

The Telegram module lets your application look up Telegram channels/groups/users, search messages (live or already-collected), manage monitored "sources," retrieve stored messages and notifications, and run historical backfills — all through one already-authenticated Telegram account managed by the Unified API. Your application never handles Telegram credentials or sessions itself.

**Authentication:** none at the HTTP level — every endpoint below is open to any caller that can reach the base URL. (Separately, the API's own backing Telegram account is already logged in; the three `auth/*` endpoints below exist to set that account up and are not something your application calls on every request.)

### Connection Status

--------------------------------------------------
### Get Telegram Connection Status

**Method:** `GET`

**Endpoint:**

    /telegram/api/telegram/status

**Full URL:**

    {BASE_URL}/telegram/api/telegram/status

**Purpose:** Reports whether the backing Telegram account is connected to Telegram's servers and authenticated.

**When to use it:** Before relying on any other Telegram endpoint, or to show connection health in your own UI/monitoring.

**Authentication:** None.

**Request headers:** None required.

**Path parameters:** None

**Query parameters:** None

**Request body:** None (GET request).

**Success response:**

```json
{
  "connected": true,
  "authenticated": true,
  "account": {"id": "8937684807", "username": null, "phone": "91********71"},
  "error": null,
  "session_invalid": false
}
```

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `connected` | boolean | MTProto transport connection is up |
| `authenticated` | boolean | The account is logged in and usable |
| `account` | object \| null | `id`, `username`, masked `phone` when authenticated |
| `error` | string \| null | Last connection error, if any |
| `session_invalid` | boolean | `true` if the session was revoked/expired and needs re-authentication (see auth endpoints below) |

**HTTP status codes:** `200` always.

**Error response:** Not applicable — this endpoint does not fail.

**cURL:**
```bash
curl "{BASE_URL}/telegram/api/telegram/status"
```

**Python:**
```python
r = requests.get(f"{BASE_URL}/telegram/api/telegram/status", timeout=10)
status = r.json()
if not status["authenticated"]:
    print("Telegram account needs re-authentication")
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/telegram/api/telegram/status`);
const status = await r.json();
```

**Integration notes:** If `authenticated` is `false` and `session_invalid` is `true`, someone with server access needs to run the auth flow below — your application cannot fix this itself.

---

### Account Authentication (setup-time only — not a per-request call)

--------------------------------------------------
### Send Login Code

**Method:** `POST`

**Endpoint:** `/telegram/api/telegram/auth/send-code`

**Full URL:** `{BASE_URL}/telegram/api/telegram/auth/send-code`

**Purpose:** Starts the Telegram login handshake for the backing account — triggers a real SMS/in-app code from Telegram.

**When to use it:** Only during initial account setup or after `session_invalid: true`. Not part of normal application traffic.

**Authentication:** None.

**Request headers:** `Content-Type: application/json`

**Path parameters:** None

**Query parameters:** None

**Request body:**
```json
{ "phone": "+1XXXXXXXXXX" }
```
`phone` (string, required): the account's phone number in international format.

**Success response:**
```json
{ "ok": true, "requires_password": false, "error": null }
```

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `ok` | boolean | Code sent successfully |
| `requires_password` | boolean | Reserved; not meaningful at this step |
| `error` | string \| null | Failure reason if `ok` is false |

**HTTP status codes:** `200` (see `ok` field for actual result), `422` validation error.

**Error response:** `422` → `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` (standard FastAPI validation error — no custom wrapper on this route).

**cURL:**
```bash
curl -X POST "{BASE_URL}/telegram/api/telegram/auth/send-code" \
  -H "Content-Type: application/json" \
  -d '{"phone": "+1XXXXXXXXXX"}'
```

**Python:**
```python
requests.post(f"{BASE_URL}/telegram/api/telegram/auth/send-code",
              json={"phone": "+1XXXXXXXXXX"}, timeout=10)
```

**JavaScript/TypeScript:**
```javascript
await fetch(`${BASE_URL}/telegram/api/telegram/auth/send-code`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({phone: "+1XXXXXXXXXX"})
});
```

**Integration notes:** Real Telegram SMS is sent — do not call this repeatedly or automatically; it can trigger Telegram-side rate limiting on the account. This is an operational/setup action, not a feature for end users of your application.

---

--------------------------------------------------
### Verify Login Code

**Method:** `POST`

**Endpoint:** `/telegram/api/telegram/auth/verify-code`

**Full URL:** `{BASE_URL}/telegram/api/telegram/auth/verify-code`

**Purpose:** Completes login with the code Telegram just sent.

**When to use it:** Immediately after `send-code`, as part of the same setup flow.

**Authentication:** None.

**Request headers:** `Content-Type: application/json`

**Path/Query parameters:** None

**Request body:**
```json
{ "phone": "+1XXXXXXXXXX", "code": "12345" }
```

**Success response:**
```json
{ "ok": true, "requires_password": false, "error": null }
```
If the account has 2FA enabled, `requires_password` is `true` and you must call the next endpoint.

**Response fields:** same as Send Login Code above.

**HTTP status codes:** `200`, `422`.

**Error response:** same FastAPI-default `422` shape as above.

**cURL:**
```bash
curl -X POST "{BASE_URL}/telegram/api/telegram/auth/verify-code" \
  -H "Content-Type: application/json" \
  -d '{"phone": "+1XXXXXXXXXX", "code": "12345"}'
```

**Python:**
```python
requests.post(f"{BASE_URL}/telegram/api/telegram/auth/verify-code",
              json={"phone": "+1XXXXXXXXXX", "code": "12345"}, timeout=10)
```

**JavaScript/TypeScript:**
```javascript
await fetch(`${BASE_URL}/telegram/api/telegram/auth/verify-code`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({phone: "+1XXXXXXXXXX", code: "12345"})
});
```

**Integration notes:** Setup-time only, same caution as Send Login Code.

---

--------------------------------------------------
### Verify 2FA Password

**Method:** `POST`

**Endpoint:** `/telegram/api/telegram/auth/verify-password`

**Full URL:** `{BASE_URL}/telegram/api/telegram/auth/verify-password`

**Purpose:** Completes login when the account has two-factor authentication enabled.

**When to use it:** Only if the previous step returned `requires_password: true`.

**Authentication:** None.

**Request headers:** `Content-Type: application/json`

**Request body:**
```json
{ "password": "..." }
```

**Success response:** `{ "ok": true, "requires_password": false, "error": null }`

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/telegram/api/telegram/auth/verify-password" \
  -H "Content-Type: application/json" -d '{"password": "..."}'
```

**Python:**
```python
requests.post(f"{BASE_URL}/telegram/api/telegram/auth/verify-password",
              json={"password": "..."}, timeout=10)
```

**Integration notes:** The 2FA password crosses the network as plain JSON — only ever call this over HTTPS. Setup-time only.

---

### Sources (monitored channels/groups/users)

--------------------------------------------------
### Create Source

**Method:** `POST`

**Endpoint:** `/telegram/api/sources`

**Full URL:** `{BASE_URL}/telegram/api/sources`

**Purpose:** Registers a Telegram channel, group, bot, or user as a "source" the API tracks. Resolves the identifier against real Telegram and probes accessibility.

**When to use it:** Before you can monitor or collect messages from a channel/group, or before calling other `/sources/{id}/...` endpoints for it.

**Authentication:** None.

**Request headers:** `Content-Type: application/json`

**Path parameters:** None

**Query parameters:** None

**Request body:**
```json
{
  "identifier": "somechannel",
  "source_type": null,
  "monitoring_enabled": false
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `identifier` | string | yes | Username, t.me link, or numeric channel ID |
| `source_type` | string \| null | no | Override the auto-detected type |
| `monitoring_enabled` | boolean | no, default `false` | Start monitoring immediately |

**Success response:**
```json
{
  "id": 1,
  "identifier": "somechannel",
  "telegram_entity_id": "1234567890",
  "username": "somechannel",
  "title": "Some Channel",
  "source_type": "channel",
  "access_status": "public",
  "status_reason": null,
  "monitoring_enabled": false,
  "monitoring_started_at": null,
  "last_status_check_at": "2026-09-13T12:00:00Z",
  "next_status_check_at": null,
  "last_message_id": null,
  "last_collected_at": null,
  "last_probe_status": "ok",
  "last_probe_reason": null,
  "last_probe_at": "2026-09-13T12:00:00Z",
  "created_at": "2026-09-13T12:00:00Z",
  "updated_at": "2026-09-13T12:00:00Z"
}
```

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `id` | integer | Source ID — use this in every other `/sources/{id}/...` call |
| `identifier` | string | What you registered |
| `telegram_entity_id` | string \| null | Real Telegram entity ID once resolved |
| `username` / `title` | string \| null | Resolved metadata |
| `source_type` | string | `channel` / `group` / `user` / `bot` |
| `access_status` | string | e.g. `public`, `private`, `pending`, `denied` |
| `monitoring_enabled` | boolean | Whether background collection is active |
| `last_message_id` / `last_collected_at` | — | Collection watermark |
| `created_at` / `updated_at` | string (ISO datetime) | — |

**HTTP status codes:** `201` created, `422` validation error.

**Error response:** `422` → standard FastAPI shape (see above).

**cURL:**
```bash
curl -X POST "{BASE_URL}/telegram/api/sources" \
  -H "Content-Type: application/json" \
  -d '{"identifier": "somechannel"}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/telegram/api/sources",
                   json={"identifier": "somechannel"}, timeout=30)
source = r.json()
source_id = source["id"]
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/telegram/api/sources`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({identifier: "somechannel"})
});
const source = await r.json();
```

**Integration notes:** This makes a real Telegram RPC call (resolving the identifier) — expect it to take longer than a typical API call (up to a few seconds). Store the returned `id` — it is required for every subsequent source-scoped call.

---

--------------------------------------------------
### List Sources

**Method:** `GET` · **Endpoint:** `/telegram/api/sources` · **Full URL:** `{BASE_URL}/telegram/api/sources`

**Purpose:** Lists all registered sources.

**When to use it:** To display/manage the set of monitored channels.

**Authentication:** None. **Request headers:** None required.

**Path parameters:** None

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | integer | no | 100 | Max rows |
| `offset` | integer | no | 0 | Skip this many (see §15 Pagination) |

**Request body:** None

**Success response:** Array of the same object shape as Create Source's response.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources?limit=50&offset=0"`

**Python:**
```python
r = requests.get(f"{BASE_URL}/telegram/api/sources", params={"limit": 50, "offset": 0}, timeout=10)
sources = r.json()
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/telegram/api/sources?limit=50&offset=0`);
const sources = await r.json();
```

**Integration notes:** Pure database read — fast, no live Telegram call.

---

--------------------------------------------------
### Get Source

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}`

**Purpose:** Fetch one source's current stored state.

**When to use it:** After Create Source, to re-check status without a new Telegram probe.

**Authentication:** None.

**Path parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source_id` | integer | yes | ID from Create Source |

**Query parameters:** None · **Request body:** None

**Success response:** Same shape as Create Source.

**HTTP status codes:** `200`, `422` (invalid ID type).

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1"`

**Python:** `requests.get(f"{BASE_URL}/telegram/api/sources/{source_id}", timeout=10)`

**JavaScript/TypeScript:** `await fetch(\`${BASE_URL}/telegram/api/sources/${sourceId}\`)`

**Integration notes:** Database-only read.

---

--------------------------------------------------
### Update Source

**Method:** `PATCH` · **Endpoint:** `/telegram/api/sources/{source_id}` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}`

**Purpose:** Change a source's title or monitoring flag.

**When to use it:** To toggle monitoring or rename locally without re-probing Telegram.

**Authentication:** None.

**Path parameters:** `source_id` (integer, required)

**Request body:**
```json
{ "monitoring_enabled": true, "title": "Custom label" }
```
Both fields optional/nullable — send only what you want changed.

**Success response:** Updated source object (same shape as Create Source).

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X PATCH "{BASE_URL}/telegram/api/sources/1" \
  -H "Content-Type: application/json" -d '{"monitoring_enabled": true}'
```

**Python:**
```python
requests.patch(f"{BASE_URL}/telegram/api/sources/{source_id}",
               json={"monitoring_enabled": True}, timeout=10)
```

**Integration notes:** **Known deployment issue:** as of the current deployment, this endpoint returns `200` but the change is not reliably persisted (a documented pre-existing defect, unrelated to this guide) — re-fetch with Get Source to confirm before relying on it.

---

--------------------------------------------------
### Delete Source

**Method:** `DELETE` · **Endpoint:** `/telegram/api/sources/{source_id}` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}`

**Purpose:** Removes a source and its associated messages/media/checkpoints/access-requests (cascading delete).

**When to use it:** To stop tracking a channel and discard its local data.

**Authentication:** None. **Path parameters:** `source_id` (integer, required). **Request body:** None.

**Success response:** No body.

**HTTP status codes:** `204` No Content on success, `422` invalid ID.

**cURL:** `curl -X DELETE "{BASE_URL}/telegram/api/sources/1"`

**Python:** `requests.delete(f"{BASE_URL}/telegram/api/sources/{source_id}", timeout=10)`

**Integration notes:** **Irreversible** — no confirmation step, no soft-delete. Double-check the ID before calling.

---

--------------------------------------------------
### Request Access

**Method:** `POST` · **Endpoint:** `/telegram/api/sources/{source_id}/request-access` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/request-access`

**Purpose:** Submits a join request for a private source (idempotent — safe to call again, won't double-submit).

**When to use it:** When `access_status` on a source indicates it's private/inaccessible and you want the backing account to request access.

**Authentication:** None. **Path parameters:** `source_id` (integer, required). **Request body:** None.

**Success response:** `200`, no fixed schema (server returns whatever the join-request result carries).

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/sources/1/request-access"`

**Python:** `requests.post(f"{BASE_URL}/telegram/api/sources/{source_id}/request-access", timeout=30)`

**Integration notes:** Real Telegram action — the account actually sends a join request. Don't call repeatedly.

---

--------------------------------------------------
### Get Access Status

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}/access-status` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/access-status`

**Purpose:** Reads the stored access status without a new Telegram call.

**Authentication:** None. **Path parameters:** `source_id` (integer, required).

**Success response:**
```json
{
  "source_id": 1, "access_status": "pending", "status_reason": null,
  "last_status_check_at": "2026-09-13T12:00:00Z", "next_status_check_at": null,
  "last_probe_status": "ok", "last_probe_reason": null, "last_probe_at": "2026-09-13T12:00:00Z"
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1/access-status"`

**Python:** `requests.get(f"{BASE_URL}/telegram/api/sources/{source_id}/access-status", timeout=10)`

**Integration notes:** Database-only read; use Check Access below to force a fresh Telegram probe.

---

--------------------------------------------------
### Check Access

**Method:** `POST` · **Endpoint:** `/telegram/api/sources/{source_id}/check-access` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/check-access`

**Purpose:** Re-probes Telegram right now for this source's accessibility and updates stored status.

**Authentication:** None. **Path parameters:** `source_id` (integer, required).

**Success response:** Full source object (same shape as Create Source), with refreshed `access_status`/`last_probe_*` fields.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/sources/1/check-access"`

**Integration notes:** Real Telegram RPC — expect it to be slower than the pure-DB status endpoint above.

---

--------------------------------------------------
### Start Monitoring

**Method:** `POST` · **Endpoint:** `/telegram/api/sources/{source_id}/monitoring/start` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/monitoring/start`

**Purpose:** Enables background collection for this source and runs one immediate collection pass.

**Authentication:** None. **Path parameters:** `source_id` (integer, required).

**Success response:**
```json
{ "source_id": 1, "monitoring_enabled": true, "access_status": "public", "monitoring_started_at": "2026-09-13T12:00:00Z", "last_collected_at": "2026-09-13T12:00:05Z", "last_message_id": 4821 }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/sources/1/monitoring/start"`

**Integration notes:** After this, new messages arrive on the API's own schedule (every `COLLECTION_INTERVAL_MINUTES`, a server-side setting) and become visible via **Locally stored messages** below — your application does not need to poll Telegram itself.

---

--------------------------------------------------
### Stop Monitoring

**Method:** `POST` · **Endpoint:** `/telegram/api/sources/{source_id}/monitoring/stop` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/monitoring/stop`

**Purpose:** Disables background collection for this source.

**Authentication:** None. **Path parameters:** `source_id` (integer, required).

**Success response:** Same shape as Start Monitoring, `monitoring_enabled: false`.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/sources/1/monitoring/stop"`

---

--------------------------------------------------
### Get Monitoring Status

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}/monitoring-status` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/monitoring-status`

**Purpose:** Reads current monitoring state without changing anything.

**Authentication:** None. **Path parameters:** `source_id` (integer, required).

**Success response:** Same shape as Start Monitoring.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1/monitoring-status"`

---

### Stored Messages (database-only — never calls Telegram)

--------------------------------------------------
### List Messages for One Source

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}/messages` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/messages`

**Purpose:** Returns already-collected messages for one source from local storage.

**When to use it:** Reading collected data — never triggers a Telegram call, always fast.

**Authentication:** None.

**Path parameters:** `source_id` (integer, required)

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | integer | no | 50 | Max rows |
| `offset` | integer | no | 0 | Skip this many |
| `keyword` | string | no | — | Text filter |
| `sender_username` | string | no | — | Filter by sender |
| `media_type` | string | no | — | Filter by media type |
| `date_from` / `date_to` | string | no | — | ISO datetime bounds |

**Request body:** None

**Success response:** Array of:
```json
{
  "id": 42, "source_id": 1, "telegram_message_id": 4821,
  "sender_id": "111", "sender_username": "alice", "sender_display_name": "Alice",
  "message_date": "2026-09-10T10:00:00Z", "edit_date": null,
  "text": "hello world", "views": 100, "forwards": 2, "reply_count": 0,
  "media_type": null, "source_url": "https://t.me/somechannel/4821",
  "collected_at": "2026-09-10T10:05:00Z", "raw_data_hash": "abc123...",
  "processing_status": "raw"
}
```

**Response fields:** as shown — `id` is the local row ID, `telegram_message_id` is Telegram's own message number.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1/messages?limit=20"`

**Python:**
```python
r = requests.get(f"{BASE_URL}/telegram/api/sources/{source_id}/messages",
                  params={"limit": 20}, timeout=10)
messages = r.json()
```

**Integration notes:** No `include_raw` here — the raw evidence file is not exposed via this API.

---

--------------------------------------------------
### Search All Stored Messages

**Method:** `GET` · **Endpoint:** `/telegram/api/messages` · **Full URL:** `{BASE_URL}/telegram/api/messages`

**Purpose:** Same as above but across every source at once.

**Authentication:** None.

**Query parameters:** same filter set as above plus `source_id` (optional, to scope to one source without using the path-based endpoint).

**Success response:** Same array shape as List Messages for One Source.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/messages?keyword=hello&limit=20"`

**Integration notes:** `limit` has no server-enforced maximum on this route — pass a sane value yourself.

---

--------------------------------------------------
### Get One Message

**Method:** `GET` · **Endpoint:** `/telegram/api/messages/{message_id}` · **Full URL:** `{BASE_URL}/telegram/api/messages/{message_id}`

**Purpose:** Fetch a single locally-stored message by its local row ID.

**Authentication:** None. **Path parameters:** `message_id` (integer, the local `id`, not `telegram_message_id`).

**Success response:** Same object shape as one row from List Messages.

**HTTP status codes:** `200`, `404` if not found, `422` invalid ID.

**cURL:** `curl "{BASE_URL}/telegram/api/messages/42"`

---

### Notifications

--------------------------------------------------
### List Notifications

**Method:** `GET` · **Endpoint:** `/telegram/api/notifications` · **Full URL:** `{BASE_URL}/telegram/api/notifications`

**Purpose:** Lists system-generated notifications (e.g. access-status changes).

**Authentication:** None.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | integer | no | 50 | Max rows |
| `offset` | integer | no | 0 | Skip |
| `unread_only` | boolean | no | false | Only unread |

**Success response:** Array of:
```json
{
  "id": 3, "event_type": "access_status_changed", "source_id": 1,
  "telegram_entity_id": "1234567890", "source_name": "Some Channel",
  "previous_status": "pending", "new_status": "public",
  "payload": {}, "is_read": false, "created_at": "2026-09-13T12:00:00Z"
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/notifications?unread_only=true"`

---

--------------------------------------------------
### Mark Notification Read

**Method:** `POST` · **Endpoint:** `/telegram/api/notifications/{notification_id}/read` · **Full URL:** `{BASE_URL}/telegram/api/notifications/{notification_id}/read`

**Purpose:** Marks one notification as read.

**Authentication:** None. **Path parameters:** `notification_id` (integer, required).

**Success response:** The updated notification object, `is_read: true`.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/notifications/3/read"`

**Integration notes:** **Known deployment issue** (same as Update Source): returns `200` but the read-state may not persist reliably in the current deployment — a documented pre-existing defect.

---

### Backfill (historical collection)

--------------------------------------------------
### Start Backfill

**Method:** `POST` · **Endpoint:** `/telegram/api/sources/{source_id}/backfill` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/backfill`

**Purpose:** Queues a background job to walk backward through a source's history and collect older messages.

**When to use it:** To fetch messages older than what monitoring has collected so far. **This is asynchronous** — see §4 workflow below.

**Authentication:** None. **Path parameters:** `source_id` (integer, required). **Request body:** none required (empty JSON object accepted).

**Success response:**
```json
{
  "id": 7, "source_id": 1, "status": "queued",
  "from_date": null, "to_date": null, "requested_limit": 0,
  "messages_found": 0, "messages_inserted": 0, "messages_skipped": 0,
  "checkpoint_message_id": null, "error_count": 0, "error_message": null,
  "started_at": null, "completed_at": null, "created_at": "2026-09-13T12:00:00Z"
}
```

**Response fields:** `id` is the backfill job ID — use it to poll below.

**HTTP status codes:** `202` Accepted, `422` validation error.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/sources/1/backfill" -H "Content-Type: application/json" -d '{}'`

**Python:**
```python
r = requests.post(f"{BASE_URL}/telegram/api/sources/{source_id}/backfill", json={}, timeout=15)
job_id = r.json()["id"]
```

**Integration notes:** **Not restart-safe** in the current deployment — a job stuck `RUNNING` when the server restarts stays `RUNNING` forever with no automatic recovery (a documented pre-existing defect). Always check `status` before assuming a job is progressing.

---

--------------------------------------------------
### List Backfill Jobs

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}/backfill` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/backfill`

**Purpose:** Lists all backfill jobs for one source.

**Authentication:** None. **Path parameters:** `source_id` (integer, required).

**Success response:** Array of the same object shape as Start Backfill's response.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1/backfill"`

---

--------------------------------------------------
### Poll One Backfill Job

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}/backfill/{job_id}` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/backfill/{job_id}`

**Purpose:** Check progress/completion of a backfill job.

**Authentication:** None.

**Path parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `source_id` | integer | yes | — |
| `job_id` | integer | yes | From Start Backfill's response |

**Success response:** Same shape as Start Backfill, with `status` progressing `queued` → `running` → `completed` / `failed`, and `messages_inserted` etc. populated.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1/backfill/7"`

**Python:**
```python
import time
while True:
    job = requests.get(f"{BASE_URL}/telegram/api/sources/{source_id}/backfill/{job_id}", timeout=10).json()
    if job["status"] in ("completed", "failed"):
        break
    time.sleep(5)
```

---

### Bots

--------------------------------------------------
### Start a Registered Bot

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/bots/{source_id}/start` · **Full URL:** `{BASE_URL}/telegram/api/telegram/bots/{source_id}/start`

**Purpose:** Sends `/start` to a bot (registered as a source) using the backing account, waits for its reply, and returns the reply text/buttons/any discovered links.

**When to use it:** To interact with a Telegram bot programmatically (e.g. discover what a bot offers).

**Authentication:** None. **Path parameters:** `source_id` (integer, required — must already be registered as a bot-type source).

**Success response:**
```json
{
  "source": {}, "status": "replied",
  "response_text": "Welcome! Use /help for commands.",
  "response_message_id": 55, "response_date": "2026-09-13T12:00:01Z",
  "buttons": [["Help", "https://t.me/somebot?start=help"]],
  "discovered": [], "error": null
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/bots/1/start"`

**Integration notes:** Uses a fixed timeout server-side (`BOT_START_TIMEOUT_SECONDS`, default 15s) waiting for the bot's reply — if the bot doesn't answer in time, `status` reflects a timeout rather than the call hanging indefinitely. This is a real message sent from the account — avoid calling repeatedly against the same bot.

---

### Discovery & Search (live Telegram queries)

--------------------------------------------------
### Extract Telegram Links from Text

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/discovery/extract-links` · **Full URL:** `{BASE_URL}/telegram/api/telegram/discovery/extract-links`

**Purpose:** Parses arbitrary text and classifies any `t.me/...` links found — read-only, no network calls.

**Authentication:** None. **Request body:** `{"text": "check out https://t.me/somechannel"}`.

**Success response:** `{"candidates": [{"...": "..."}]}` — one entry per link found, classified by kind (channel/group/invite/message).

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/discovery/extract-links" -H "Content-Type: application/json" -d '{"text": "https://t.me/somechannel"}'`

**Integration notes:** Never fetches non-Telegram URLs, purely text parsing — safe to call freely.

---

--------------------------------------------------
### Discover Channels/Groups by Keyword

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/channels/search` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channels/search`

**Purpose:** Live Telegram search for channels/groups matching a keyword (not message content search).

**Authentication:** None.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | yes | — | Keyword |
| `limit` | integer | no | 20 | Max results |

**Success response:**
```json
{ "results": [{}], "pagination": {}, "warnings": [] }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/channels/search?q=news&limit=10"`

**Integration notes:** No auto-persistence — see **Save Search Result** below if you want to keep a hit.

---

--------------------------------------------------
### Register a Discovered Channel as a Source

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/channels/{telegram_id}/register` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channels/{telegram_id}/register`

**Purpose:** Converts a channel found via discovery/search directly into a registered source (equivalent to Create Source, but starting from a Telegram entity ID rather than a username/link).

**Authentication:** None.

**Path parameters:** `telegram_id` (string, required — from a search result).

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `monitoring_enabled` | boolean | no | true | Start monitoring immediately |

**Success response:** Source object, same shape as Create Source. **HTTP status codes:** `201`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/channels/1234567890/register?monitoring_enabled=true"`

---

--------------------------------------------------
### Search Within One Arbitrary Channel

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/channels/{channel_id}/search` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channels/{channel_id}/search`

**Purpose:** Live keyword search inside one channel, whether or not it's a registered source.

**Authentication:** None.

**Path parameters:** `channel_id` (string, required).

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | yes | — | Keyword |
| `limit` | integer | no | 20 | Max results |
| `cursor` | string | no | — | Pagination cursor from a previous response |
| `sender_username` | string | no | — | Filter |
| `from_date` / `to_date` | string | no | — | ISO datetime bounds |
| `media_type` | string | no | — | Filter |
| `include_raw` | boolean | no | false | Include raw Telegram payload per result |

**Success response:**
```json
{ "results": [{}], "pagination": {"next_cursor": "..."}, "warnings": [] }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/channels/1234567890/search?q=hello&limit=10"`

**Integration notes:** Fresh Telegram probe every call — no local caching. Use `pagination.next_cursor` (if present) as the next call's `cursor` parameter.

---

--------------------------------------------------
### Search Within a Registered Source

**Method:** `GET` · **Endpoint:** `/telegram/api/sources/{source_id}/search` · **Full URL:** `{BASE_URL}/telegram/api/sources/{source_id}/search`

**Purpose:** Same live search as above, scoped to an already-registered source by its local `source_id`.

**Authentication:** None. **Path parameters:** `source_id` (integer, required). **Query parameters:** same as channel search above (`q` required).

**Success response:** Same shape as channel search.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/sources/1/search?q=hello&limit=10"`

**Integration notes:** Trusts the source's already-stored access status rather than re-probing.

---

--------------------------------------------------
### Global Telegram Search

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/search/global` · **Full URL:** `{BASE_URL}/telegram/api/telegram/search/global`

**Purpose:** Cross-channel keyword search across all of Telegram (not scoped to any one source).

**Authentication:** None.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | yes | — | Keyword |
| `limit` | integer | no | 20 | Max results |
| `cursor` | string | no | — | Pagination |
| `from_date` / `to_date` | string | no | — | Bounds |
| `channel_username` / `channel_id` | string | no | — | Narrow to one channel |
| `media_type` | string | no | — | Filter |
| `sort` | string | no | `date_desc` | Sort order |
| `include_raw` | boolean | no | false | Include raw payload |

**Success response:**
```json
{
  "results": [{}], "pagination": {}, "method_used": "channels.searchPosts",
  "quota": {"remaining": 90}, "warnings": []
}
```

**Response fields:** `method_used` tells you which underlying Telegram API path served the request (a paid/quota'd global-search method with a daily cap, or a fallback). `quota`, when present, reflects that daily cap.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/search/global?q=breaking+news&limit=20"`

**Integration notes:** This endpoint has a shared daily quota on the underlying Telegram method — heavy use can exhaust it for the rest of the day; watch the `quota` field.

---

--------------------------------------------------
### Search Across Selected Sources

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/search/sources` · **Full URL:** `{BASE_URL}/telegram/api/telegram/search/sources`

**Purpose:** Runs the same keyword search across a list of registered sources (or all monitoring-enabled ones if none specified).

**Authentication:** None.

**Request body:**
```json
{
  "source_ids": [1, 2],
  "q": "keyword",
  "limit": 20,
  "cursor": null,
  "sender_username": null,
  "from_date": null, "to_date": null,
  "media_type": null, "include_raw": false
}
```
`source_ids` optional — omit to search every monitoring-enabled source.

**Success response:**
```json
{ "results": [{}], "searched_source_ids": [1, 2], "skipped": [], "warnings": [] }
```

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/telegram/api/telegram/search/sources" \
  -H "Content-Type: application/json" -d '{"q": "keyword", "source_ids": [1,2]}'
```

**Integration notes:** Runs sources **sequentially**, not in parallel — response time scales with how many sources you pass.

---

--------------------------------------------------
### Save a Search Result

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/search/results/save` · **Full URL:** `{BASE_URL}/telegram/api/telegram/search/results/save`

**Purpose:** **The only endpoint that persists a search result.** Ordinary search calls above return live data and store nothing — call this explicitly if you want a specific hit kept in local storage.

**Authentication:** None.

**Request body:**
```json
{
  "identifier": "somechannel",
  "telegram_message_id": 4821,
  "collection_method": "manual",
  "monitoring_enabled": false
}
```

**Success response:**
```json
{ "source": {}, "message": {}, "inserted": true }
```
`inserted: false` if that message was already saved (idempotent).

**HTTP status codes:** `201`, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/telegram/api/telegram/search/results/save" \
  -H "Content-Type: application/json" \
  -d '{"identifier": "somechannel", "telegram_message_id": 4821}'
```

**Integration notes:** See §4 — this is the explicit "save" step distinguished from live search.

---

### Provider API (storage-decoupled — never touches the database)

These endpoints resolve/fetch live Telegram data and return it directly; they never read or write local storage, and are independent of the `/sources`/`/messages` endpoints above.

--------------------------------------------------
### Resolve Channel Metadata

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/channel` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channel`

**Purpose:** Looks up a channel/group/bot's metadata live.

**Authentication:** None.

**Request body:** one of `username`, `url`, or `channel_id`:
```json
{ "username": "somechannel" }
```

**Success response:**
```json
{
  "id": "1234567890", "username": "somechannel", "title": "Some Channel",
  "type": "channel", "description": "...", "members_count": 5000,
  "photo_url": null, "is_public": true, "url": "https://t.me/somechannel"
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/channel" -H "Content-Type: application/json" -d '{"username": "somechannel"}'`

---

--------------------------------------------------
### Check Channel Access

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/channel/access` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channel/access`

**Purpose:** Whether the backing account can currently read this channel.

**Authentication:** None. **Request body:** same identifier shape as Resolve Channel Metadata.

**Success response:** `{ "accessible": true, "state": "public", "detail": null }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/channel/access" -H "Content-Type: application/json" -d '{"username": "somechannel"}'`

---

--------------------------------------------------
### Recent Channel Messages (Provider)

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/channel/messages` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channel/messages`

**Purpose:** Live fetch of recent messages (not a keyword search).

**Authentication:** None.

**Request body:**
```json
{ "username": "somechannel", "channel_id": null, "limit": 20, "cursor": null }
```

**Success response:** `{ "items": [{}], "cursor": "..." }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/channel/messages" -H "Content-Type: application/json" -d '{"username": "somechannel", "limit": 20}'`

**Integration notes:** Pass `cursor` from the response back in as the next call's `cursor` to page further back.

---

--------------------------------------------------
### Single Message by URL or ID (Provider)

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/message` · **Full URL:** `{BASE_URL}/telegram/api/telegram/message`

**Purpose:** Fetch one message.

**Authentication:** None.

**Request body:** one of `url` or (`channel_id` + `message_id`):
```json
{ "url": "https://t.me/somechannel/4821" }
```

**Success response:**
```json
{
  "id": "4821", "channel_id": "1234567890", "text": "...",
  "date": "2026-09-10T10:00:00Z", "url": "https://t.me/somechannel/4821",
  "views": 100, "forwards": 2, "replies_count": 0, "media": [], "author": {}
}
```

**HTTP status codes:** `200`, `404` message not found, `422`.

**Error response (404):** `{"error": {"code": "...", "message": "...", "retryable": false}}` (Telegram-module standard error shape, see §11).

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/message" -H "Content-Type: application/json" -d '{"url": "https://t.me/somechannel/4821"}'`

---

--------------------------------------------------
### Message Replies (Provider)

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/message/replies` · **Full URL:** `{BASE_URL}/telegram/api/telegram/message/replies`

**Purpose:** Comments/replies under a message — requires the channel to have a linked discussion group.

**Authentication:** None.

**Request body:**
```json
{ "channel_id": "1234567890", "message_id": "4821", "limit": 20, "cursor": null }
```

**Success response:** `{ "items": [{}], "cursor": "..." }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/message/replies" -H "Content-Type: application/json" -d '{"channel_id": "1234567890", "message_id": "4821"}'`

**Integration notes:** Returns an empty/appropriate result if the channel has no discussion group — check the response rather than assuming replies always exist.

---

--------------------------------------------------
### Resolve Any t.me Link (Provider)

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/resolve` · **Full URL:** `{BASE_URL}/telegram/api/telegram/resolve`

**Purpose:** Parses any `t.me/...` link and classifies it (channel, message, invite).

**Authentication:** None. **Request body:** `{ "url": "https://t.me/somechannel/4821" }`

**Success response:** `{ "kind": "message", "channel": {}, "message": {}, "invite": null }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/resolve" -H "Content-Type: application/json" -d '{"url": "https://t.me/somechannel/4821"}'`

---

--------------------------------------------------
### Join by Invite (Provider)

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/invite/join` · **Full URL:** `{BASE_URL}/telegram/api/telegram/invite/join`

**Purpose:** Joins (or requests to join) a private channel/group via invite link/hash.

**Authentication:** None. **Request body:** `{ "invite": "https://t.me/+AbCdEfGhIjK" }`

**Success response:** `{ "joined": true, "pending": false, "channel": {} }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/invite/join" -H "Content-Type: application/json" -d '{"invite": "https://t.me/+AbCdEfGhIjK"}'`

**Integration notes:** Real account action — joins using the backing Telegram account. Avoid repeated calls.

---

--------------------------------------------------
### Join by Invite (Sources-integrated variant)

**Method:** `POST` · **Endpoint:** `/telegram/api/telegram/invites/join` · **Full URL:** `{BASE_URL}/telegram/api/telegram/invites/join`

**Purpose:** Same join action, but also registers/updates the corresponding source record.

**Authentication:** None. **Request body:** `{ "identifier": "https://t.me/+AbCdEfGhIjK" }`

**Success response:** `{ "source": {}, "status": "joined", "reason": null }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/telegram/api/telegram/invites/join" -H "Content-Type: application/json" -d '{"identifier": "https://t.me/+AbCdEfGhIjK"}'`

---

--------------------------------------------------
### Discover Channels (Provider)

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/search/channels` · **Full URL:** `{BASE_URL}/telegram/api/telegram/search/channels`

**Purpose:** Provider-style channel discovery by keyword.

**Authentication:** None.

**Query parameters:** `q` (string, required), `limit` (integer, default 20).

**Success response:** `{ "items": [{}] }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/search/channels?q=news&limit=10"`

---

--------------------------------------------------
### Keyword Search Across Messages (Provider)

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/search/messages` · **Full URL:** `{BASE_URL}/telegram/api/telegram/search/messages`

**Purpose:** Provider-style message search.

**Authentication:** None.

**Query parameters:** `q` (required), `limit` (default 20), `cursor` (optional).

**Success response:** `{ "items": [{}], "cursor": "..." }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/search/messages?q=keyword&limit=20"`

---

### Media

--------------------------------------------------
### Fetch Channel Profile Photo

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/channels/{channel_id}/photo` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channels/{channel_id}/photo`

**Purpose:** Returns the full-resolution profile photo as binary image bytes.

**When to use it:** Display a channel/user avatar. Cached on the server after first fetch — repeat calls for the same entity are fast.

**Authentication:** None. **Path parameters:** `channel_id` (string, required).

**Success response:** Binary `image/jpeg` body, no JSON envelope. **Response fields:** N/A (binary).

**HTTP status codes:** `200` (photo bytes), `404` entity has no photo, `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/channels/1234567890/photo" --output photo.jpg`

**Python:**
```python
r = requests.get(f"{BASE_URL}/telegram/api/telegram/channels/{channel_id}/photo", timeout=15)
if r.status_code == 200:
    with open("photo.jpg", "wb") as f:
        f.write(r.content)
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/telegram/api/telegram/channels/${channelId}/photo`);
if (r.ok) {
  const blob = await r.blob();  // use as <img src={URL.createObjectURL(blob)}>
}
```

**Integration notes:** See §16 Files and Media — treat this as a binary download, not JSON.

---

--------------------------------------------------
### Fetch Message Media (Provider)

**Method:** `GET` · **Endpoint:** `/telegram/api/telegram/channels/{channel_id}/messages/{message_id}/media` · **Full URL:** `{BASE_URL}/telegram/api/telegram/channels/{channel_id}/messages/{message_id}/media`

**Purpose:** On-demand fetch of a message's attached photo/video/document, returned as binary bytes.

**Authentication:** None.

**Path parameters:** `channel_id` (string, required), `message_id` (string, required).

**Success response:** Binary body, `Content-Type` matches the media type.

**HTTP status codes:** `200`, `404` no media/not found, `413` if oversized (server-enforced max size), `422`.

**cURL:** `curl "{BASE_URL}/telegram/api/telegram/channels/1234567890/messages/4821/media" --output media_file`

**Integration notes:** Cached server-side after first fetch. Large media may return `413` — check `MAXIMUM_MEDIA_SIZE_BYTES` behavior with your operator if you hit this.

---

### Operational

--------------------------------------------------
### Health

**Method:** `GET` · **Endpoint:** `/telegram/health` · **Full URL:** `{BASE_URL}/telegram/health`

**Purpose:** Liveness check — never fails, no dependency checks.

**Success response:** `{"status": "ok"}` · **HTTP status codes:** `200` always.

**cURL:** `curl "{BASE_URL}/telegram/health"`

---

--------------------------------------------------
### Readiness

**Method:** `GET` · **Endpoint:** `/telegram/ready` · **Full URL:** `{BASE_URL}/telegram/ready`

**Purpose:** Reports whether Telegram credentials are *configured* — does **not** confirm the session is actually connected/authenticated (use Get Telegram Connection Status for that).

**Success response:** `{"status": "ready", "telegram_configured": true}`

**HTTP status codes:** `200` always (status reflects configuration, not failure).

**cURL:** `curl "{BASE_URL}/telegram/ready"`

---

# 4. Telegram Workflows

## Search → optionally save

```
GET /telegram/api/telegram/search/global (or /channels/{id}/search, /sources/{id}/search)
      ↓
receive results (LIVE — nothing stored)
      ↓
optionally: POST /telegram/api/telegram/search/results/save
      ↓
(now that specific message exists in local storage, retrievable via
 GET /telegram/api/sources/{id}/messages or /telegram/api/messages)
```

**Live result** — every search endpoint (`search/global`, `channels/{id}/search`, `sources/{id}/search`, `search/sources`, and every Provider-prefixed search) returns data fetched from Telegram at that moment. Calling it again returns fresh results; nothing is remembered.

**Explicitly saved result** — only `POST /telegram/api/telegram/search/results/save` writes anything. This is a deliberate, separate action your application must call if it wants a specific hit to persist. Do not assume an ordinary search call saves its results — it does not.

## Source lifecycle

```
POST /telegram/api/sources                (register — returns source_id)
      ↓
GET  /telegram/api/sources/{source_id}/access-status   (check if accessible)
      ↓ (if private/pending)
POST /telegram/api/sources/{source_id}/request-access
      ↓
POST /telegram/api/sources/{source_id}/monitoring/start
      ↓
(background collection now runs on the server's own schedule)
      ↓
GET  /telegram/api/sources/{source_id}/messages        (read what's been collected)
```

The `source_id` returned by Create Source is required by every other `/sources/{id}/...` endpoint — store it.

## Backfill (async job)

```
POST /telegram/api/sources/{source_id}/backfill   → 202 + job id
      ↓
GET  /telegram/api/sources/{source_id}/backfill/{job_id}   (poll until status is completed/failed)
      ↓
GET  /telegram/api/sources/{source_id}/messages            (collected messages now included)
```

---

# 5. Reddit API

The Reddit module provides two independent transports behind the same paths: the **official Reddit OAuth2 API** (subreddit/post/comment/user/resolve — requires the deployment to have Reddit developer credentials configured) and **Reddit's public RSS/Atom feeds** (`rss/*` routes — need no credentials at all and remain usable regardless of OAuth configuration).

**Authentication:** the deployment may require an `X-API-Key` header on every `/reddit/api/reddit/*` call (`Authorization` is not used — it is a custom header). **In the currently deployed environment, no API key is configured, so this header is not currently required** — confirm with your operator whether this remains true before going to production, since it can be turned on at any time.

```
Your Application
      │  request (+ X-API-Key if the deployment requires it)
      ▼
 Unified Reddit API
      │  (OAuth path) or (public RSS, no credentials)
      ▼
     Reddit
      │
      ▼
 JSON response back to your application
```

--------------------------------------------------
### Health

**Method:** `GET` · **Endpoint:** `/reddit/health` · **Full URL:** `{BASE_URL}/reddit/health`

**Purpose:** Liveness — never calls Reddit, never fails.

**Authentication:** None.

**Success response:** `{"status": "ok"}` · **HTTP status codes:** `200` always.

**cURL:** `curl "{BASE_URL}/reddit/health"`

**Integration notes:** Use this (not Connection Status below) for automated health checks/load-balancer probes — it never makes an outbound call.

---

--------------------------------------------------
### Readiness

**Method:** `GET` · **Endpoint:** `/reddit/ready` · **Full URL:** `{BASE_URL}/reddit/ready`

**Purpose:** Reports whether Reddit OAuth is configured — a configuration check only, no network call to Reddit.

**Authentication:** None.

**Success response:** `{"status": "ok", "reddit_configured": false}`

**HTTP status codes:** `200` always.

**cURL:** `curl "{BASE_URL}/reddit/ready"`

**Integration notes:** `reddit_configured: false` means OAuth-backed endpoints (subreddit/post/comment/user/resolve) will return `503` — the RSS endpoints work regardless.

---

--------------------------------------------------
### Connection Status

**Method:** `GET` · **Endpoint:** `/reddit/api/reddit/status` · **Full URL:** `{BASE_URL}/reddit/api/reddit/status`

**Purpose:** Makes a **real, live** call to Reddit to check OAuth connectivity.

**When to use it:** On-demand diagnostics only — **never** use as a health/liveness probe (an orchestrator restarting on this endpoint's failure would kill a healthy service during a transient Reddit outage).

**Authentication:** `X-API-Key` if the deployment requires it.

**Success response:** `{ "connected": true, "authorized": true, "account": {"username": "..."} }`

**HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/reddit/api/reddit/status"`

---

--------------------------------------------------
### Subreddit Metadata

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/subreddit` · **Full URL:** `{BASE_URL}/reddit/api/reddit/subreddit`

**Purpose:** Resolve a subreddit's metadata via the official OAuth API.

**When to use it:** Need subreddit description/subscriber count/etc.

**Authentication:** `X-API-Key` if required; **requires OAuth to be configured server-side** (returns `503` otherwise).

**Request headers:** `Content-Type: application/json`

**Path parameters:** None

**Query parameters:** None

**Request body:**
```json
{ "name": "python" }
```
`name` (string, required): subreddit name without `r/`.

**Success response:**
```json
{
  "id": "t5_2qh0y", "name": "python", "display_name": "Python",
  "title": "...", "description": "...", "subscribers": 1200000,
  "url": "https://www.reddit.com/r/python/", "public": true, "over18": false
}
```

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `id` | string \| null | Reddit's internal fullname |
| `name` / `display_name` | string | Subreddit identifiers |
| `subscribers` | integer | Subscriber count |
| `public` / `over18` | boolean | Visibility flags |

**HTTP status codes:** `200`, `422` validation, `503` if OAuth not configured.

**Error response:** `503` → `{"error": {"code": "REDDIT_NOT_CONFIGURED", "message": "Reddit is not configured. Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT."}}`

**cURL:**
```bash
curl -X POST "{BASE_URL}/reddit/api/reddit/subreddit" \
  -H "Content-Type: application/json" -d '{"name": "python"}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/reddit/api/reddit/subreddit", json={"name": "python"}, timeout=15)
if r.status_code == 503:
    print("Reddit OAuth not configured on this deployment")
else:
    subreddit = r.json()
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/reddit/api/reddit/subreddit`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({name: "python"})
});
```

**Integration notes:** In the currently deployed environment, Reddit OAuth credentials are **not configured**, so expect `503` from this endpoint today. Use the RSS endpoints below for a working alternative that needs no credentials.

---

--------------------------------------------------
### Subreddit Accessibility

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/subreddit/access` · **Full URL:** `{BASE_URL}/reddit/api/reddit/subreddit/access`

**Purpose:** Checks whether a subreddit is public/private/restricted/quarantined/nonexistent.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "subreddit": "python" }`

**Success response:** `{ "accessible": true, "state": "public", "detail": "..." }`

**HTTP status codes:** `200`, `422`, `503` (OAuth unconfigured).

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/subreddit/access" -H "Content-Type: application/json" -d '{"subreddit": "python"}'`

---

--------------------------------------------------
### Subreddit Posts

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/subreddit/posts` · **Full URL:** `{BASE_URL}/reddit/api/reddit/subreddit/posts`

**Purpose:** Paginated posts from one subreddit, or several combined with `+` (e.g. `"news+worldnews"`).

**When to use it:** Polling a subreddit for new posts (the primary intended use case for this whole module).

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:**
```json
{ "subreddit": "python", "sort": "new", "limit": 25, "cursor": null }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `subreddit` | string | yes | One name, or `+`-joined multiple |
| `sort` | string | no | e.g. `new`, `hot`, `top` |
| `limit` | integer \| null | no | Page size |
| `cursor` | string \| null | no | From a previous response, for the next page |

**Success response:**
```json
{
  "items": [
    {
      "id": "1abcde", "subreddit": "python", "title": "...", "text": "...",
      "author": {}, "created_at": "2026-09-13T12:00:00Z",
      "url": "https://reddit.com/...", "score": 42, "upvote_ratio": 0.95,
      "num_comments": 3, "media": []
    }
  ],
  "cursor": "t3_1abcde"
}
```

**Response fields:** `items[]` — post objects (see fields above); `cursor` — pass back as `cursor` in the next request to page forward, `null` when there are no more results.

**HTTP status codes:** `200`, `422`, `503`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/reddit/api/reddit/subreddit/posts" \
  -H "Content-Type: application/json" -d '{"subreddit": "python", "sort": "new", "limit": 25}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/reddit/api/reddit/subreddit/posts",
                   json={"subreddit": "python", "sort": "new", "limit": 25}, timeout=15)
page = r.json()
posts, cursor = page["items"], page["cursor"]
```

**Integration notes:** This is what a consuming application should poll on its own schedule — the API itself has no built-in polling/alerting.

---

--------------------------------------------------
### Post Details

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/post` · **Full URL:** `{BASE_URL}/reddit/api/reddit/post`

**Purpose:** Fetch one post by ID or URL.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** one of `post_id` or `url`: `{ "post_id": "1abcde" }`

**Success response:** Same post object shape as one item from Subreddit Posts.

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/post" -H "Content-Type: application/json" -d '{"post_id": "1abcde"}'`

---

--------------------------------------------------
### Post Comments

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/post/comments` · **Full URL:** `{BASE_URL}/reddit/api/reddit/post/comments`

**Purpose:** Flattened, cursor-paginated comment tree for a post (auto-expands up to 3 "load more" stubs server-side).

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "post_id": "1abcde", "limit": 50, "cursor": null }`

**Success response:**
```json
{
  "items": [
    { "id": "cde123", "post_id": "1abcde", "parent_id": null, "text": "...",
      "author": {}, "created_at": "...", "score": 5, "url": "..." }
  ],
  "cursor": null
}
```

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/post/comments" -H "Content-Type: application/json" -d '{"post_id": "1abcde", "limit": 50}'`

---

--------------------------------------------------
### Comment Details

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/comment` · **Full URL:** `{BASE_URL}/reddit/api/reddit/comment`

**Purpose:** Fetch one comment by ID.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "comment_id": "cde123" }`

**Success response:** Same comment object shape as above.

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/comment" -H "Content-Type: application/json" -d '{"comment_id": "cde123"}'`

---

--------------------------------------------------
### User Profile

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/user` · **Full URL:** `{BASE_URL}/reddit/api/reddit/user`

**Purpose:** Fetch a Reddit user's profile.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "username": "someuser" }`

**Success response:**
```json
{ "id": "t2_abc", "username": "someuser", "created_at": "...", "link_karma": 100, "comment_karma": 50, "is_mod": false }
```

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/user" -H "Content-Type: application/json" -d '{"username": "someuser"}'`

---

--------------------------------------------------
### Posts by User

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/user/posts` · **Full URL:** `{BASE_URL}/reddit/api/reddit/user/posts`

**Purpose:** Paginated posts submitted by a user.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "username": "someuser", "limit": 25, "cursor": null }`

**Success response:** `{ "items": [/* post objects */], "cursor": "..." }`

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/user/posts" -H "Content-Type: application/json" -d '{"username": "someuser", "limit": 25}'`

---

--------------------------------------------------
### Comments by User

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/user/comments` · **Full URL:** `{BASE_URL}/reddit/api/reddit/user/comments`

**Purpose:** Paginated comments by a user.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "username": "someuser", "limit": 25, "cursor": null }`

**Success response:** `{ "items": [/* comment objects */], "cursor": "..." }`

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/user/comments" -H "Content-Type: application/json" -d '{"username": "someuser", "limit": 25}'`

---

--------------------------------------------------
### Search Posts (Global Discovery)

**Method:** `GET` · **Endpoint:** `/reddit/api/reddit/search/posts` · **Full URL:** `{BASE_URL}/reddit/api/reddit/search/posts`

**Purpose:** Keyword-based post discovery across all of Reddit via OAuth.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | no | — | Keyword |
| `keywords` | array | no | — | Multiple keywords |
| `limit` | integer | no | 25 | Page size |
| `cursor` | string | no | — | Pagination |
| `sort` | string | no | `relevance` | Sort order |
| `time` | string | no | `all` | Time range |
| `subreddit` | string | no | — | Narrow to one subreddit |

**Success response:** `{ "items": [/* post objects */], "cursor": "..." }`

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl "{BASE_URL}/reddit/api/reddit/search/posts?q=python&limit=10"`

---

--------------------------------------------------
### Search Subreddits

**Method:** `GET` · **Endpoint:** `/reddit/api/reddit/search/subreddits` · **Full URL:** `{BASE_URL}/reddit/api/reddit/search/subreddits`

**Purpose:** Discover subreddits by keyword.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Query parameters:** `q` (string, **required**), `limit` (integer, default 25), `cursor` (string, optional).

**Success response:**
```json
{ "items": [{"id": "t5_2qh0y", "name": "python", "display_name": "Python", "title": "...", "subscribers": 1200000, "url": "..."}], "cursor": null }
```

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl "{BASE_URL}/reddit/api/reddit/search/subreddits?q=machine+learning&limit=10"`

---

--------------------------------------------------
### Unified Search

**Method:** `GET` · **Endpoint:** `/reddit/api/reddit/search` · **Full URL:** `{BASE_URL}/reddit/api/reddit/search`

**Purpose:** One endpoint that returns either posts or subreddits depending on `type`.

**Authentication:** `X-API-Key` if required; needs OAuth configured for `type=posts`/`subreddits`.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | no | — | Keyword |
| `keywords` | array | no | — | Multiple keywords |
| `type` | string | no | `posts` | `posts` or `subreddits` |
| `limit` | integer | no | 25 | Page size |
| `cursor` | string | no | — | Pagination |
| `subreddit` | string | no | — | Narrow (posts only) |
| `time` | string | no | `all` | Time range (posts only) |

**Success response:** Either a posts page or a subreddits page (same shapes as Search Posts / Search Subreddits above), selected by `type`.

**HTTP status codes:** `200`, `400` if `type=comments` is passed (not supported on this route — use RSS event monitoring or the OAuth comment endpoints instead), `422`, `503`.

**cURL:** `curl "{BASE_URL}/reddit/api/reddit/search?q=python&type=posts&limit=10"`

**Integration notes:** `type=comments` is explicitly unsupported here and returns `400 UNSUPPORTED_SEARCH` — this is not a bug, it's by design; use Post Comments or RSS event monitoring instead.

---

--------------------------------------------------
### Resolve a Reddit URL

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/resolve` · **Full URL:** `{BASE_URL}/reddit/api/reddit/resolve`

**Purpose:** Classifies an arbitrary Reddit URL into subreddit/post/comment.

**Authentication:** `X-API-Key` if required; needs OAuth configured.

**Request body:** `{ "url": "https://www.reddit.com/r/python/comments/1abcde/some_post/" }`

**Success response:** `{ "kind": "post", "subreddit": {}, "post": {}, "comment": null }`

**HTTP status codes:** `200`, `422`, `503`.

**cURL:** `curl -X POST "{BASE_URL}/reddit/api/reddit/resolve" -H "Content-Type: application/json" -d '{"url": "https://www.reddit.com/r/python/comments/1abcde/"}'`

---

--------------------------------------------------
### RSS Monitor (POST)

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/rss/monitor` · **Full URL:** `{BASE_URL}/reddit/api/reddit/rss/monitor`

**Purpose:** Keyword/event monitoring via Reddit's **public RSS/Atom feeds — needs no Reddit credentials at all**, works even when OAuth is unconfigured.

**When to use it:** The reliable default choice on any deployment, regardless of whether OAuth is set up.

**Authentication:** `X-API-Key` if required (a separate per-caller RSS request budget also applies — see §17).

**Request body:**
```json
{
  "query": "python", "keywords": [], "strong_keywords": [], "exclude": [],
  "match_field": "full", "min_matches": 1,
  "subreddits": [], "sort": "new", "time_range": "day",
  "from_date": null, "to_date": null, "limit": 50
}
```
Only `query` or `keywords` is typically needed — the rest have working defaults.

**Success response:**
```json
{
  "source": "reddit", "transport": "rss", "authenticated": false,
  "query": "python", "subreddits": [], "sort": "new", "time_range": "day",
  "from_date": null, "to_date": null, "count": 3,
  "posts": [
    { "id": "1abcde", "guid": "t3_1abcde", "title": "...", "author": "someuser",
      "url": "https://www.reddit.com/...", "subreddit": "python", "flair": null,
      "published_at": "2026-09-13T12:00:00Z", "updated_at": "2026-09-13T12:00:00Z",
      "content": "...", "source": "reddit", "source_type": "rss",
      "matched_keywords": [], "signal": false }
  ]
}
```

**Response fields:** `authenticated` is always `false` here (RSS needs no login); `posts[]` — see fields above; no `cursor`/pagination on this route (RSS feeds are a fixed-size snapshot per call, not incrementally paginated).

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/reddit/api/reddit/rss/monitor" \
  -H "Content-Type: application/json" -d '{"query": "python", "limit": 10}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/reddit/api/reddit/rss/monitor",
                   json={"query": "python", "limit": 10}, timeout=15)
posts = r.json()["posts"]
```

**Integration notes:** This is the module's most reliable endpoint — no credentials, no `503` possible for lack of OAuth. Repeated identical calls are served faster from a short-TTL shared cache (see §17).

---

--------------------------------------------------
### RSS Search (GET convenience form)

**Method:** `GET` · **Endpoint:** `/reddit/api/reddit/rss/search` · **Full URL:** `{BASE_URL}/reddit/api/reddit/rss/search`

**Purpose:** Same as RSS Monitor, as a GET with query parameters — convenient for quick calls/testing.

**Authentication:** `X-API-Key` if required.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `q` | string | no | — | Keyword |
| `keywords` | array | no | — | Multiple keywords |
| `strong_keywords` | array | no | — | Must-match keywords |
| `exclude` | array | no | — | Exclusion terms |
| `match_field` | string | no | `full` | Where to match |
| `min_matches` | integer | no | 1 | Minimum keyword matches |
| `subreddit` | string | no | — | Narrow to one subreddit |
| `sort` | string | no | `new` | Sort order |
| `time` | string | no | `day` | Time range |
| `from_date` / `to_date` | string | no | — | Bounds |
| `limit` | integer | no | 50 | Max results |

**Success response:** Same shape as RSS Monitor.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/reddit/api/reddit/rss/search?q=python&limit=5"`

**Python:**
```python
r = requests.get(f"{BASE_URL}/reddit/api/reddit/rss/search",
                  params={"q": "python", "limit": 5}, timeout=15)
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/reddit/api/reddit/rss/search?q=python&limit=5`);
const data = await r.json();
```

---

--------------------------------------------------
### RSS Event Monitoring

**Method:** `POST` · **Endpoint:** `/reddit/api/reddit/rss/event` · **Full URL:** `{BASE_URL}/reddit/api/reddit/rss/event`

**Purpose:** Same RSS-based monitoring, plus a stateless `event_signal` flag indicating whether matched-post volume crossed a configured threshold.

**Authentication:** `X-API-Key` if required.

**Request body:** Same shape as RSS Monitor.

**Success response:** Same shape as RSS Monitor, plus:
```json
"event_signal": { "detected": true, "count": 12, "threshold": 10 }
```

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/reddit/api/reddit/rss/event" \
  -H "Content-Type: application/json" -d '{"query": "outage", "limit": 50}'
```

**Integration notes:** `event_signal` is computed fresh per call (stateless) — your application decides what to do when `detected: true`, the API does not alert/notify on its own.

---

# 6. Response Handling (Reddit)

Every Reddit endpoint follows the synchronous pattern: request → live Reddit/RSS fetch → JSON response. There are no job IDs, no polling, no asynchronous endpoints in this module.

# 7. Cache Behavior

Identical RSS requests (same query/params) made close together are served from a short-lived shared in-process cache rather than re-fetching Reddit every time — you may observe a repeat call return in a few milliseconds instead of the ~0.1-2s a fresh fetch takes. This is transparent; no cache-control header or parameter is needed or supported to bypass it.

# 8. Rate Limiting (Reddit)

The API paces its own outbound calls to Reddit's public RSS endpoints to avoid IP-level blocking, and separately may cap how many RSS requests your application can make per time window if `X-API-Key` auth is enabled. If you receive a `429`, back off and retry after a delay rather than retrying immediately (see §17/§18).

---

# 9. OSINT API

The OSINT module runs identifier lookups (phone, email, username, domain, person name) against free, keyless public sources, and supports deeper multi-step "investigations" that pivot across discovered identifiers. Every lookup — sync or async — creates an **investigation** your application can look up again later by ID.

**Authentication:** none — every endpoint is open to any caller that can reach the base URL.

**Error shape (all OSINT endpoints):**
```json
{ "error": { "code": 422, "message": "request validation failed", "request_id": "...", "details": [...] } }
```
`code` is the HTTP status code (as a number, not a string) — different from Reddit/Telegram's string error codes.

--------------------------------------------------
### Health

**Method:** `GET` · **Endpoint:** `/osint/health` · **Full URL:** `{BASE_URL}/osint/health`

**Purpose:** Liveness, no dependency checks.

**Success response:** `{"status": "ok"}` · **HTTP status codes:** `200` always.

**cURL:** `curl "{BASE_URL}/osint/health"`

---

--------------------------------------------------
### Readiness

**Method:** `GET` · **Endpoint:** `/osint/ready` · **Full URL:** `{BASE_URL}/osint/ready`

**Purpose:** Confirms the database is reachable.

**Success response:** `{"status": "ready"}` on success.

**HTTP status codes:** `200` ready, `503` not ready.

**Error response (503):** `{"status": "not_ready", "reason": "<exception text>"}`

**cURL:** `curl "{BASE_URL}/osint/ready"`

---

--------------------------------------------------
### Phone Lookup

**Method:** `POST`

**Endpoint:** `/osint/api/v1/phone/lookup`

**Full URL:** `{BASE_URL}/osint/api/v1/phone/lookup`

**Purpose:** Validates and enriches a phone number (carrier region, line type, timezone) using an offline library — no external calls, always fast.

**When to use it:** Quick, synchronous phone intelligence. **This creates an investigation** you can look up again later (see §10).

**Authentication:** None.

**Request headers:** `Content-Type: application/json`

**Path parameters:** None

**Query parameters:** None

**Request body:**
```json
{ "phone": "+14155552671" }
```
`phone` (string, required): E.164 or locally-formatted phone number.

**Success response:**
```json
{
  "investigation_id": "de96e7a2-8736-42cb-a15a-a053de77c091",
  "input_identifier": "+14155552671",
  "identifier_type": "PHONE",
  "normalized_identifier": "+14155552671",
  "status": "completed",
  "jobs": [
    { "id": "...", "source_name": "phonenumbers", "status": "completed",
      "error": null, "result_count": 1, "created_at": "...", "started_at": "...", "finished_at": "..." }
  ],
  "entities": [ { "...": "..." } ],
  "evidence": [ { "...": "..." } ]
}
```

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `investigation_id` | string (UUID) | **Save this** — use it with the Investigation endpoints (§10) to look up this same result later |
| `status` | string | `completed`, `partial`, `failed` |
| `jobs` | array | One entry per source consulted, with its own status |
| `entities` / `evidence` | array | Structured findings |

**HTTP status codes:** `200`, `422` validation error.

**Error response:** see OSINT error shape above.

**cURL:**
```bash
curl -X POST "{BASE_URL}/osint/api/v1/phone/lookup" \
  -H "Content-Type: application/json" -d '{"phone": "+14155552671"}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/osint/api/v1/phone/lookup",
                   json={"phone": "+14155552671"}, timeout=15)
result = r.json()
investigation_id = result["investigation_id"]
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/osint/api/v1/phone/lookup`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({phone: "+14155552671"})
});
const result = await r.json();
```

**Integration notes:** Synchronous — no polling needed, `status` is already terminal by the time you get the response. Store `investigation_id` if you want to re-fetch or view it in the fuller investigation views later.

---

--------------------------------------------------
### Email Lookup

**Method:** `POST` · **Endpoint:** `/osint/api/v1/email/lookup` · **Full URL:** `{BASE_URL}/osint/api/v1/email/lookup`

**Purpose:** Checks which of ~140 web services an email address is registered on.

**When to use it:** Synchronous email intelligence. Creates an investigation.

**Authentication:** None.

**Request body:** `{ "email": "someone@example.com" }`

**Success response:** Same `LookupResultOut` shape as Phone Lookup.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/osint/api/v1/email/lookup" -H "Content-Type: application/json" -d '{"email": "someone@example.com"}'`

**Integration notes:** Slower than phone lookup (real outbound probes to ~140 sites) — expect several seconds.

---

--------------------------------------------------
### Username Lookup

**Method:** `POST` · **Endpoint:** `/osint/api/v1/username/lookup` · **Full URL:** `{BASE_URL}/osint/api/v1/username/lookup`

**Purpose:** Checks a username's presence across hundreds of sites (Sherlock + Maigret engines merged).

**When to use it:** Synchronous username intelligence — **can take minutes**. Creates an investigation.

**Authentication:** None.

**Request body:** `{ "username": "someuser" }`

**Success response:** Same `LookupResultOut` shape as Phone Lookup, with many more `evidence` entries (one per confirmed site hit).

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/osint/api/v1/username/lookup" -H "Content-Type: application/json" -d '{"username": "someuser"}' --max-time 300`

**Python:**
```python
r = requests.post(f"{BASE_URL}/osint/api/v1/username/lookup",
                   json={"username": "someuser"}, timeout=300)  # generous timeout required
```

**Integration notes:** **Set a client timeout of several minutes** — this is the slowest synchronous endpoint in the whole API. Do not assume a short default timeout is safe here.

---

--------------------------------------------------
### Domain Lookup

**Method:** `POST` · **Endpoint:** `/osint/api/v1/domain/lookup` · **Full URL:** `{BASE_URL}/osint/api/v1/domain/lookup`

**Purpose:** DNS/WHOIS recon plus a public-web search pass for a domain.

**Authentication:** None.

**Request body:** `{ "domain": "example.com" }`

**Success response:** Same `LookupResultOut` shape. `status` may be `"partial"` if the public-web source is unavailable (e.g. the deployment's search backend isn't configured) while DNS/WHOIS still succeeded.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/osint/api/v1/domain/lookup" -H "Content-Type: application/json" -d '{"domain": "example.com"}'`

**Integration notes:** Check each entry in `jobs[]` — one source failing (`status: "unavailable"`) does not mean the whole lookup failed; read `status` at the top level (`completed`/`partial`) to know if you got a full or partial result.

---

--------------------------------------------------
### Person Lookup

**Method:** `POST` · **Endpoint:** `/osint/api/v1/person/lookup` · **Full URL:** `{BASE_URL}/osint/api/v1/person/lookup`

**Purpose:** Looks up a person's name against Wikidata and public web search.

**Authentication:** None.

**Request body:** `{ "person_name": "Jane Doe" }`

**Success response:** Same `LookupResultOut` shape.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/osint/api/v1/person/lookup" -H "Content-Type: application/json" -d '{"person_name": "Jane Doe"}'`

---

--------------------------------------------------
### Create Investigation

**Method:** `POST`

**Endpoint:** `/osint/api/v1/investigations`

**Full URL:** `{BASE_URL}/osint/api/v1/investigations`

**Purpose:** Starts a deeper, asynchronous, multi-step investigation with automatic pivoting across discovered identifiers.

**When to use it:** When you want the fuller investigation engine (pivoting, document mining) rather than a single-source lookup — **this is asynchronous**, see §10.

**Authentication:** None.

**Request headers:**

| Header | Required | Description |
|---|---|---|
| `Content-Type` | yes | `application/json` |
| `Idempotency-Key` | no | Same key + same body → same investigation returned again instead of creating a duplicate |

**Request body:**
```json
{
  "input_identifier": "+14155552671",
  "identifier_type": "PHONE",
  "analyst_notes": null,
  "mode": "standard"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `input_identifier` | string | yes | The value to investigate |
| `identifier_type` | string \| null | no | `PHONE` / `EMAIL` / `USERNAME` / `PERSON_NAME` / `DOMAIN` — auto-detected if omitted |
| `analyst_notes` | string \| null | no | Free-text notes |
| `mode` | string | no | e.g. `quick`, `standard`, `deep` — controls pivot depth/thoroughness |

**Success response:**
```json
{
  "id": "de96e7a2-8736-42cb-a15a-a053de77c091",
  "input_identifier": "+14155552671", "identifier_type": "PHONE",
  "normalized_identifier": "+14155552671", "mode": "standard",
  "status": "queued", "cancel_requested": false,
  "created_at": "2026-09-13T12:09:08Z", "updated_at": "2026-09-13T12:09:08Z"
}
```

**Response fields:** `id` — **save this**, it's the investigation ID for every endpoint below. `status` starts `queued`, transitions to `running`, then `completed`/`failed`/`cancelled`.

**HTTP status codes:** `201` created, `409` idempotency-key conflict (same key, different body), `422`, `429` if the deployment's concurrent-investigation cap is reached.

**Error response (409):** OSINT error shape, `code: 409`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/osint/api/v1/investigations" \
  -H "Content-Type: application/json" \
  -d '{"input_identifier": "+14155552671", "identifier_type": "PHONE", "mode": "standard"}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/osint/api/v1/investigations",
                   json={"input_identifier": "+14155552671", "identifier_type": "PHONE", "mode": "standard"},
                   timeout=15)
investigation_id = r.json()["id"]
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/osint/api/v1/investigations`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({input_identifier: "+14155552671", identifier_type: "PHONE", mode: "standard"})
});
const {id: investigationId} = await r.json();
```

**Integration notes:** Returns immediately with `status: "queued"` — the actual work happens in the background. See §10 for the full poll workflow.

---

--------------------------------------------------
### List Investigations

**Method:** `GET` · **Endpoint:** `/osint/api/v1/investigations` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations`

**Purpose:** Lists investigations.

**Authentication:** None.

**Query parameters:** `limit` (integer, default 50, max 200).

**Success response:** Array of the same summary object shape as Create Investigation's response.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/investigations?limit=20"`

---

--------------------------------------------------
### Get Investigation (full detail)

**Method:** `GET` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}`

**Purpose:** Full detail — jobs, entities, relationships, evidence, pivots.

**Authentication:** None. **Path parameters:** `investigation_id` (string UUID, required).

**Success response:**
```json
{
  "id": "...", "input_identifier": "...", "identifier_type": "PHONE",
  "normalized_identifier": "...", "mode": "standard", "status": "completed",
  "cancel_requested": false, "created_at": "...", "updated_at": "...",
  "technical_metadata": {}, "analyst_notes": null,
  "jobs": [], "entities": [], "relationships": [], "evidence": [], "pivots": []
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/investigations/de96e7a2-8736-42cb-a15a-a053de77c091"`

---

--------------------------------------------------
### Get Investigation Status (lightweight poll)

**Method:** `GET` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}/status` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}/status`

**Purpose:** Cheap polling endpoint — job/pivot counts only, no full payload.

**Authentication:** None. **Path parameters:** `investigation_id` (required).

**Success response:**
```json
{ "id": "...", "status": "running", "mode": "standard", "cancel_requested": false, "elapsed_seconds": 4.2, "jobs": {"total": 3, "completed": 2}, "pivots": {"total": 1, "completed": 0} }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/investigations/de96e7a2.../status"`

**Integration notes:** Use this for polling loops — it's cheaper than Get Investigation. Poll until `status` is `completed`/`failed`/`cancelled`.

---

--------------------------------------------------
### Get Investigation Report

**Method:** `GET` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}/report` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}/report`

**Purpose:** Human-readable case report (sources, discovered entities/relationships, evidence, limitations, analyst notes).

**Authentication:** None. **Path parameters:** `investigation_id` (required).

**Success response:** A structured report object (fields: `investigation_id`, `case`, `sources`, `technical_data`, `discovered_entities`, `relationships`, `evidence`, `pivots`, `limitations`, `analyst_notes`).

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/investigations/de96e7a2.../report"`

**Integration notes:** Best endpoint for displaying a finished investigation to an end user — call only once `status` is terminal.

---

--------------------------------------------------
### Get Investigation Graph

**Method:** `GET` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}/graph` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}/graph`

**Purpose:** Entity relationship graph for visualization.

**Authentication:** None. **Path parameters:** `investigation_id` (required).

**Success response:** `{ "investigation_id": "...", "nodes": [...], "edges": [...] }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/investigations/de96e7a2.../graph"`

**Integration notes:** Feed directly into a graph-visualization library (nodes/edges shape is generic, library-agnostic).

---

--------------------------------------------------
### Get Investigation Timeline

**Method:** `GET` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}/timeline` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}/timeline`

**Purpose:** Chronological event list for the investigation.

**Authentication:** None. **Path parameters:** `investigation_id` (required).

**Success response:** Array of timeline events.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/investigations/de96e7a2.../timeline"`

---

--------------------------------------------------
### Update Investigation Notes

**Method:** `PUT` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}/notes` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}/notes`

**Purpose:** Attach/replace analyst notes on an investigation.

**Authentication:** None. **Path parameters:** `investigation_id` (required).

**Request body:** `{ "analyst_notes": "Confirmed via secondary source." }`

**Success response:** Updated `InvestigationSummary` (same shape as Create Investigation's response).

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X PUT "{BASE_URL}/osint/api/v1/investigations/de96e7a2.../notes" \
  -H "Content-Type: application/json" -d '{"analyst_notes": "Confirmed via secondary source."}'
```

---

--------------------------------------------------
### Cancel Investigation

**Method:** `POST` · **Endpoint:** `/osint/api/v1/investigations/{investigation_id}/cancel` · **Full URL:** `{BASE_URL}/osint/api/v1/investigations/{investigation_id}/cancel`

**Purpose:** Cooperatively requests cancellation of a running investigation.

**Authentication:** None. **Path parameters:** `investigation_id` (required).

**Success response:** Updated `InvestigationSummary`, `cancel_requested: true`.

**HTTP status codes:** `200`, `409` if already in a terminal state, `422`.

**cURL:** `curl -X POST "{BASE_URL}/osint/api/v1/investigations/de96e7a2.../cancel"`

**Integration notes:** Cooperative, not instant — the worker checks `cancel_requested` between steps, so `status` may stay `running` briefly after this call before settling to `cancelled`.

---

--------------------------------------------------
### Search

**Method:** `POST` and `GET` · **Endpoint:** `/osint/api/v1/search` · **Full URL:** `{BASE_URL}/osint/api/v1/search`

**Purpose:** General public web search via the deployment's configured search backend.

**Authentication:** None.

**Request body (POST):** `{ "query": "site:example.com", "page": 1, "language": "en", "safesearch": 0 }`

**Query parameters (GET):** `q` (required), `page` (default 1), `language` (default `en`), `safesearch` (default 0).

**Success response:** `{ "query": "...", "results": [...], "meta": {"total": 10, "successful_engines": 3, "failed_engines": 0} }`

**HTTP status codes:** `200`, `422`.

**cURL (GET):** `curl "{BASE_URL}/osint/api/v1/search?q=site:example.com"`

**cURL (POST):** `curl -X POST "{BASE_URL}/osint/api/v1/search" -H "Content-Type: application/json" -d '{"query": "site:example.com"}'`

**Integration notes:** Depends on the deployment's search backend being configured — check `meta.successful_engines`; `0` means the backend is unreachable/unconfigured and `results` will be empty, not an error.

---

--------------------------------------------------
### Source Health

**Method:** `GET` · **Endpoint:** `/osint/api/v1/sources/health` · **Full URL:** `{BASE_URL}/osint/api/v1/sources/health`

**Purpose:** Per-adapter reachability/health status (phonenumbers, holehe, sherlock, maigret, wikidata, domain_recon, public_web, searxng).

**When to use it:** Diagnostics — checking which sources are currently working before relying on a lookup.

**Authentication:** None.

**Success response:** Array of:
```json
{
  "source_name": "sherlock", "enabled": true, "installed": true, "version": null,
  "last_success": "2026-09-13T12:00:00Z", "last_failure": null, "last_error": null,
  "success_count": 10, "failure_count": 0, "timeout_count": 0,
  "success_rate": 1.0, "average_duration_seconds": 4.2, "run_count": 10, "status": "healthy"
}
```

**HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/osint/api/v1/sources/health"`

---

--------------------------------------------------
### Pwned Password Check

**Method:** `POST` · **Endpoint:** `/osint/api/v1/utils/pwned-password` · **Full URL:** `{BASE_URL}/osint/api/v1/utils/pwned-password`

**Purpose:** k-anonymity check against Have I Been Pwned's breach corpus — checks if a password has appeared in known breaches, without ever sending the full password to a third party.

**Authentication:** None.

**Request body:** `{ "password": "hunter2" }`

**Success response:** `{ "pwned": true, "times_seen": 12345, "note": "..." }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/osint/api/v1/utils/pwned-password" -H "Content-Type: application/json" -d '{"password": "hunter2"}'`

**Integration notes:** Do not log the password field anywhere in your own application.

---

--------------------------------------------------
### Metrics

**Method:** `GET` · **Endpoint:** `/osint/metrics` · **Full URL:** `{BASE_URL}/osint/metrics`

**Purpose:** Prometheus-format metrics text.

**Authentication:** None.

**Success response:** `text/plain`, Prometheus exposition format — not JSON.

**HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/osint/metrics"`

**Integration notes:** For monitoring tooling (Prometheus scraper), not for application logic.

---

# 10. OSINT ID Workflow

Every lookup and every investigation returns an ID your application should keep. The exact sequence:

```
1. Send a lookup request (phone/email/domain/username/person)
   OR create an investigation (POST /investigations)
        ↓
2. Receive investigation_id (lookups) or id (investigations — same kind of ID)
        ↓
3. Store that ID in your own application
        ↓
4. Poll  GET /osint/api/v1/investigations/{id}/status   (cheap)
        ↓  (repeat until status is completed/failed/cancelled)
5. Fetch GET /osint/api/v1/investigations/{id}           (full detail)
      or GET /osint/api/v1/investigations/{id}/report     (human-readable)
      or GET /osint/api/v1/investigations/{id}/graph       (visualization)
      or GET /osint/api/v1/investigations/{id}/timeline    (chronological)
```

**Synchronous endpoints** (respond with a complete, terminal result in one call, no polling needed): Phone Lookup, Email Lookup, Domain Lookup, Person Lookup, Username Lookup (slow, but still one call), Search.

**Asynchronous endpoint** (returns immediately with `status: "queued"`, work happens in the background): `POST /investigations`. Even though the sync lookups also create an investigation record, calling them does not require polling — the response you get back already reflects the completed work.

---

# 11. Bluweb / Scrape API

The Bluweb module crawls public websites, extracts structured content (articles, forum threads, listings, etc.), tracks changes over time, and clusters related documents into "stories." Every crawl is asynchronous; document/version/change/diff retrieval is synchronous and can be called at any later time.

**Authentication:** none — every endpoint is open to any caller that can reach the base URL.

**Error shape:** custom errors (raised by business logic) →
```json
{ "error": { "code": "SOME_CODE", "message": "...", "details": {}, "request_id": "..." } }
```
Validation errors (`422`) use FastAPI's plain default shape: `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}` — no `error` wrapper on those specifically.

--------------------------------------------------
### Health

**Method:** `GET` · **Endpoint:** `/scrape/health` · **Full URL:** `{BASE_URL}/scrape/health`

**Purpose:** Liveness, no dependency checks. **Success response:** `{"status": "ok"}`. **HTTP status codes:** `200` always.

**cURL:** `curl "{BASE_URL}/scrape/health"`

---

--------------------------------------------------
### Liveness (alias)

**Method:** `GET` · **Endpoint:** `/scrape/health/live` · **Full URL:** `{BASE_URL}/scrape/health/live`

**Purpose:** Same as Health, alternate path. **Success response:** `{"status": "alive"}`. **HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/scrape/health/live"`

---

--------------------------------------------------
### Readiness

**Method:** `GET` · **Endpoint:** `/scrape/health/ready` · **Full URL:** `{BASE_URL}/scrape/health/ready`

**Purpose:** Confirms the Postgres database is reachable (`SELECT 1`).

**Success response:** `{"status": "ready", "database": "ok"}`

**HTTP status codes:** `200` ready, `503` not ready (`{"status": "not_ready", "database": "error: ..."}`).

**cURL:** `curl "{BASE_URL}/scrape/health/ready"`

---

--------------------------------------------------
### Metrics

**Method:** `GET` · **Endpoint:** `/scrape/metrics` · **Full URL:** `{BASE_URL}/scrape/metrics`

**Purpose:** Prometheus-format metrics. **Success response:** `text/plain`, not JSON. **HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/scrape/metrics"`

---

--------------------------------------------------
### Run Pre-flight Assessment

**Method:** `POST`

**Endpoint:** `/scrape/api/v1/preflight`

**Full URL:** `{BASE_URL}/scrape/api/v1/preflight`

**Purpose:** Probes a URL (DNS, HTTP reachability, robots.txt, sitemap/feed discovery, a sample extraction) and scores how well this site can be crawled.

**When to use it:** **Required before registering a monitored Source** (Create Source below needs a valid, unexpired preflight for the same domain). Also useful standalone to check feasibility before crawling.

**Authentication:** None.

**Request headers:** `Content-Type: application/json`

**Path parameters:** None

**Query parameters:** None

**Request body:**
```json
{ "url": "https://example-news-site.com" }
```
`url` (string, required).

**Success response:**
```json
{
  "preflight_id": "837351cc-cfac-4e91-a992-26d69d5485dd",
  "url": "https://example.com", "final_url": "https://example.com",
  "status": "completed",
  "capability": { "score": 70, "confidence": "low", "discovery_score": 0, "fetch_score": 100, "extraction_score": 100, "rendering_score": 100, "content_type_score": 100 },
  "discovery": { "sitemap": false, "rss": false, "atom": false, "html_links": false },
  "fetch": {}, "content": {}, "extraction": {}, "sample": {},
  "limitations": [], "recommendations": [],
  "duration_ms": 1234.5, "error": null,
  "created_at": "2026-09-13T12:00:00Z", "expires_at": "2026-09-14T12:00:00Z"
}
```

**Response fields:**

| Field | Type | Description |
|---|---|---|
| `preflight_id` | string (UUID) | **Save this** — required by Create Source |
| `status` | string | `completed` / `failed` |
| `capability.score` | integer 0-100 | Overall crawlability estimate |
| `expires_at` | string | This preflight becomes invalid for Create Source after this time |

**HTTP status codes:** `201` created (always, even if the target itself failed to respond — check `status`/`error` fields), `422` validation.

**cURL:**
```bash
curl -X POST "{BASE_URL}/scrape/api/v1/preflight" \
  -H "Content-Type: application/json" -d '{"url": "https://example.com"}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/scrape/api/v1/preflight", json={"url": "https://example.com"}, timeout=40)
report = r.json()
preflight_id = report["preflight_id"]
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/scrape/api/v1/preflight`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({url: "https://example.com"})
});
const report = await r.json();
```

**Integration notes:** Can take several seconds (DNS + HTTP + sample extraction all run synchronously). Use a client timeout of at least 30-40s.

---

--------------------------------------------------
### Get Pre-flight Report

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/preflight/{preflight_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/preflight/{preflight_id}`

**Purpose:** Retrieve a previously run report.

**Authentication:** None. **Path parameters:** `preflight_id` (string UUID, required).

**Success response:** Same shape as Run Pre-flight Assessment.

**HTTP status codes:** `200`, `404` not found, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/preflight/837351cc-cfac-4e91-a992-26d69d5485dd"`

---

--------------------------------------------------
### Create Crawl

**Method:** `POST`

**Endpoint:** `/scrape/api/v1/crawls`

**Full URL:** `{BASE_URL}/scrape/api/v1/crawls`

**Purpose:** Starts crawling a URL (and, unless `same_domain_only` is false, its same-domain links) in the background.

**When to use it:** To collect and extract content from a site — **this is asynchronous**, see §12.

**Authentication:** None.

**Request body:**
```json
{ "url": "https://example.com", "max_pages": 50, "max_depth": 3, "same_domain_only": true }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | Seed URL |
| `max_pages` | integer \| null | no | 1-1000, server default if omitted |
| `max_depth` | integer \| null | no | 0-10, server default if omitted |
| `same_domain_only` | boolean | no, default `true` | Stay on the seed's domain |

**Success response:**
```json
{
  "crawl_id": "968b8e25-90e9-440d-ba1d-9fc3faee20bc", "status": "queued",
  "seed_url": "https://example.com", "max_pages": 50, "max_depth": 3,
  "created_at": "2026-09-13T12:00:00Z", "started_at": null, "completed_at": null,
  "error": null, "statistics": {}
}
```

**Response fields:** `crawl_id` — **save this**, `status` progresses `queued` → `running` → `completed`/`failed`.

**HTTP status codes:** `202` Accepted, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/scrape/api/v1/crawls" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "max_pages": 10, "same_domain_only": true}'
```

**Python:**
```python
r = requests.post(f"{BASE_URL}/scrape/api/v1/crawls",
                   json={"url": "https://example.com", "max_pages": 10}, timeout=15)
crawl_id = r.json()["crawl_id"]
```

**JavaScript/TypeScript:**
```javascript
const r = await fetch(`${BASE_URL}/scrape/api/v1/crawls`, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify({url: "https://example.com", max_pages: 10})
});
const {crawl_id: crawlId} = await r.json();
```

**Integration notes:** Returns immediately — the actual fetch/extraction happens afterward. Poll with Get Crawl below. **Known deployment limitation**: if the deployment's archival storage is enabled and unreachable, a crawl can remain stuck at `status: "running"` indefinitely rather than transitioning to `failed` — if a crawl never completes after a reasonable time, treat it as failed on your side too rather than waiting forever.

---

--------------------------------------------------
### List Crawls

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/crawls` · **Full URL:** `{BASE_URL}/scrape/api/v1/crawls`

**Purpose:** Lists crawl jobs.

**Authentication:** None. **Query parameters:** None. **Success response:** Array of the same object shape as Create Crawl's response.

**HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/crawls"`

---

--------------------------------------------------
### Get Crawl (poll status)

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/crawls/{crawl_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/crawls/{crawl_id}`

**Purpose:** Check a crawl's progress/completion.

**Authentication:** None. **Path parameters:** `crawl_id` (string UUID, required).

**Success response:** Same shape as Create Crawl, with `statistics` populated once running/complete:
```json
"statistics": {
  "pages_fetched": 10, "pages_attempted": 10, "pages_failed": 0,
  "new_documents": 8, "updated_documents": 0, "duplicate_documents": 0,
  "unchanged_documents": 2, "bytes_downloaded": 45210, "duration_ms": 8231.4
}
```

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/crawls/968b8e25-90e9-440d-ba1d-9fc3faee20bc"`

**Python:**
```python
import time
while True:
    job = requests.get(f"{BASE_URL}/scrape/api/v1/crawls/{crawl_id}", timeout=10).json()
    if job["status"] in ("completed", "failed"):
        break
    time.sleep(2)
```

---

--------------------------------------------------
### Cancel Crawl

**Method:** `POST` · **Endpoint:** `/scrape/api/v1/crawls/{crawl_id}/cancel` · **Full URL:** `{BASE_URL}/scrape/api/v1/crawls/{crawl_id}/cancel`

**Purpose:** Cancels a running/queued crawl.

**Authentication:** None. **Path parameters:** `crawl_id` (required).

**Success response:** Updated crawl object.

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/scrape/api/v1/crawls/968b8e25.../cancel"`

---

--------------------------------------------------
### List Crawl Pages

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/crawls/{crawl_id}/pages` · **Full URL:** `{BASE_URL}/scrape/api/v1/crawls/{crawl_id}/pages`

**Purpose:** Per-URL fetch attempts for one crawl (what was fetched, HTTP status, resulting document if any).

**Authentication:** None. **Path parameters:** `crawl_id` (required).

**Success response:** Array of:
```json
{ "url": "...", "normalized_url": "...", "depth": 0, "status": "success", "fetch_strategy": "http", "http_status": 200, "document_id": "252f8142-...", "error": null }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/crawls/968b8e25.../pages"`

**Integration notes:** `document_id` here is what you pass to the Documents endpoints below.

---

--------------------------------------------------
### List Documents

**Method:** `GET`

**Endpoint:** `/scrape/api/v1/documents`

**Full URL:** `{BASE_URL}/scrape/api/v1/documents`

**Purpose:** Lists extracted documents.

**Authentication:** None.

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `crawl_id` | string | no | Scope to one crawl |
| `source_id` | string | no | Scope to one monitored source |
| `domain` | string | no | Scope to one domain |
| `page_type` | string | no | e.g. `NEWS_ARTICLE`, `FORUM_THREAD` |

**Success response:** Array of:
```json
{
  "document_id": "252f8142-efa9-45f9-8bec-b05ee262a901",
  "url": "https://example.com", "canonical_url": "https://example.com",
  "domain": "example.com", "title": "Example Domain", "author": null,
  "published_at": null, "language": null, "content_type": "html",
  "page_type": "HOME", "extraction_method": "generic", "extraction_confidence": 0.5,
  "current_version": 1, "collected_at": "2026-09-13T12:00:00Z",
  "latest_change_type": null, "latest_change_severity": null, "latest_change_at": null,
  "entity_ids": [], "story_id": null, "story_match_confidence": null
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/documents?domain=example.com"`

**Integration notes:** **No pagination on this endpoint** — it returns up to a fixed server-side cap (currently 50 rows); scope with `crawl_id`/`source_id`/`domain` to narrow results rather than expecting a `limit`/`offset`.

---

--------------------------------------------------
### Get Document

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/documents/{document_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/documents/{document_id}`

**Purpose:** One document's detail, including linked `entity_ids`/`story_id`.

**Authentication:** None. **Path parameters:** `document_id` (string UUID, required).

**Success response:** Same shape as one item from List Documents.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/documents/252f8142-efa9-45f9-8bec-b05ee262a901"`

---

--------------------------------------------------
### List Document Versions

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/documents/{document_id}/versions` · **Full URL:** `{BASE_URL}/scrape/api/v1/documents/{document_id}/versions`

**Purpose:** Version history summary (no body content — use Get Document Version for that).

**Authentication:** None. **Path parameters:** `document_id` (required).

**Success response:** Array of:
```json
{ "version_number": 1, "change_type": "NEW", "content_hash": "95455b6e...", "created_at": "2026-09-08T19:52:43Z" }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/documents/68d454c9.../versions"`

---

--------------------------------------------------
### Get Document Version (with content)

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/documents/{document_id}/versions/{version_number}` · **Full URL:** `{BASE_URL}/scrape/api/v1/documents/{document_id}/versions/{version_number}`

**Purpose:** **The only endpoint that returns full extracted text content.**

**Authentication:** None.

**Path parameters:** `document_id` (string UUID, required), `version_number` (integer, required).

**Success response:**
```json
{ "version_number": 1, "change_type": "NEW", "title": "Example Domain", "content": "This domain is for use in documentation examples...", "content_hash": "95455b6e...", "created_at": "2026-09-08T19:52:43Z" }
```

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/documents/68d454c9.../versions/1"`

**Python:**
```python
r = requests.get(f"{BASE_URL}/scrape/api/v1/documents/{doc_id}/versions/1", timeout=10)
full_text = r.json()["content"]
```

---

--------------------------------------------------
### List Document Changes

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/documents/{document_id}/changes` · **Full URL:** `{BASE_URL}/scrape/api/v1/documents/{document_id}/changes`

**Purpose:** Stored change-detection events for a document (when monitoring finds it changed).

**Authentication:** None.

**Path parameters:** `document_id` (required)

**Query parameters:** `severity`, `change_type`, `from`, `to` — all optional filters.

**Success response:** Array of:
```json
{
  "change_id": "...", "document_id": "...", "previous_version_id": "...", "current_version_id": "...",
  "page_type": "NEWS_ARTICLE", "change_type": "content_updated", "severity": "minor",
  "similarity": 0.92, "change_confidence": 0.8, "changed_fields": ["body"],
  "diff": {}, "reasons": ["..."], "created_at": "2026-09-13T12:00:00Z"
}
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/documents/68d454c9.../changes"`

---

--------------------------------------------------
### Diff Document Versions

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/documents/{document_id}/diff` · **Full URL:** `{BASE_URL}/scrape/api/v1/documents/{document_id}/diff`

**Purpose:** Unified line diff between two specific versions.

**Authentication:** None.

**Path parameters:** `document_id` (required)

**Query parameters:** `from_version` (integer, **required**), `to_version` (integer, **required**).

**Success response:**
```json
{ "document_id": "...", "from_version": 1, "to_version": 1, "changed": false, "summary": {"added_lines": 0, "removed_lines": 0}, "diff": [] }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/documents/68d454c9.../diff?from_version=1&to_version=2"`

---

--------------------------------------------------
### Search

**Method:** `POST` · **Endpoint:** `/scrape/api/v1/search` · **Full URL:** `{BASE_URL}/scrape/api/v1/search`

**Purpose:** Full-text search over already-crawled documents (does not crawl anything new).

**Authentication:** None.

**Request body:**
```json
{ "query": "keyword", "domain": null, "source_id": null, "language": null, "date_from": null, "date_to": null, "limit": 20, "offset": 0 }
```
Only `query` is typically needed; the rest narrow the search.

**Success response:** `{ "total": 42, "results": [{"...": "..."}] }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/scrape/api/v1/search" -H "Content-Type: application/json" -d '{"query": "keyword"}'`

**Integration notes:** Use `limit`/`offset` for pagination — see §15.

---

--------------------------------------------------
### Instant Search

**Method:** `POST`

**Endpoint:** `/scrape/api/v1/search/instant`

**Full URL:** `{BASE_URL}/scrape/api/v1/search/instant`

**Purpose:** Starts a crawl **and** waits (server-side) up to a configurable number of seconds for partial results, returning whatever completed within that window.

**When to use it:** When you want a "search this new site right now" experience and can tolerate a request that takes up to ~30 seconds.

**Authentication:** None.

**Request body:**
```json
{ "url": "https://example.com", "query": "keyword", "max_pages": 10, "wait_seconds": 20 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | yes | Site to crawl |
| `query` | string \| null | no | Filter results by keyword |
| `max_pages` | integer | no | Crawl scope |
| `wait_seconds` | number | no | How long to wait before returning partial results |

**Success response:**
```json
{ "crawl_id": "...", "crawl_status": "running", "total": 3, "results": [{"...": "..."}], "note": "Crawl still in progress; poll /crawls/{id} for completion." }
```

**HTTP status codes:** `200`, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/scrape/api/v1/search/instant" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "wait_seconds": 15}' --max-time 30
```

**Integration notes:** **Set a client timeout above `wait_seconds`** (e.g. `wait_seconds + 10`) — the server genuinely blocks for that duration. `crawl_id` is still returned so you can keep polling Get Crawl afterward if the crawl wasn't finished within the wait window.

---

--------------------------------------------------
### Create Source

**Method:** `POST`

**Endpoint:** `/scrape/api/v1/sources`

**Full URL:** `{BASE_URL}/scrape/api/v1/sources`

**Purpose:** Registers a URL for continuous monitoring (recurring crawls, change detection).

**When to use it:** For ongoing monitoring rather than a one-off crawl. **Requires a valid, unexpired preflight for the same domain** (§13 workflow).

**Authentication:** None.

**Request body:**
```json
{
  "name": "Example News", "url": "https://example-news-site.com",
  "preflight_id": "837351cc-cfac-4e91-a992-26d69d5485dd",
  "source_type": "news", "interval_seconds": 3600, "max_interval_seconds": 86400,
  "crawl_policy": null
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Display name |
| `url` | string | yes | Site to monitor |
| `preflight_id` | string | yes | From a preflight run against the **same domain**, not expired |
| `source_type` | string | no | Category label |
| `interval_seconds` / `max_interval_seconds` | integer | no | Monitoring cadence bounds |
| `crawl_policy` | object \| null | no | Advanced crawl tuning |

**Success response:**
```json
{
  "source_id": "...", "name": "Example News", "base_url": "https://example-news-site.com",
  "domain": "example-news-site.com", "source_type": "news", "status": "active",
  "crawl_policy": {}, "min_interval_seconds": 3600, "max_interval_seconds": 86400,
  "current_interval_seconds": 3600, "created_at": "...", "updated_at": "...",
  "last_crawl_at": null, "next_crawl_at": null
}
```

**HTTP status codes:** `201`, `400` invalid preflight/domain mismatch, `404` preflight not found, `409` conflict, `422`.

**cURL:**
```bash
curl -X POST "{BASE_URL}/scrape/api/v1/sources" \
  -H "Content-Type: application/json" \
  -d '{"name": "Example News", "url": "https://example-news-site.com", "preflight_id": "837351cc-..."}'
```

**Integration notes:** Always run a fresh Preflight first — if it has expired (`expires_at` in the past) or was run against a different domain, this call fails with `400`/`404`.

---

--------------------------------------------------
### List Sources

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/sources` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources`

**Purpose:** Lists monitored sources.

**Authentication:** None. **Success response:** Array of the same shape as Create Source's response.

**HTTP status codes:** `200`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/sources"`

---

--------------------------------------------------
### Get Source

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/sources/{source_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}`

**Purpose:** One source's current state.

**Authentication:** None. **Path parameters:** `source_id` (string UUID, required).

**Success response:** Same shape as Create Source.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/sources/{source_id}"`

---

--------------------------------------------------
### Update Source

**Method:** `PATCH` · **Endpoint:** `/scrape/api/v1/sources/{source_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}`

**Purpose:** Change name/type/crawl policy (URL/domain cannot be changed — delete and recreate instead).

**Authentication:** None. **Path parameters:** `source_id` (required).

**Request body:** `{ "name": "New name", "source_type": null, "crawl_policy": null }` — send only fields to change.

**Success response:** Updated source object.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl -X PATCH "{BASE_URL}/scrape/api/v1/sources/{source_id}" -H "Content-Type: application/json" -d '{"name": "New name"}'`

---

--------------------------------------------------
### Delete Source

**Method:** `DELETE` · **Endpoint:** `/scrape/api/v1/sources/{source_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}`

**Purpose:** Removes a monitored source.

**Authentication:** None. **Path parameters:** `source_id` (required). **Success response:** No body.

**HTTP status codes:** `204`, `404`, `422`.

**cURL:** `curl -X DELETE "{BASE_URL}/scrape/api/v1/sources/{source_id}"`

**Integration notes:** Does not delete already-collected documents — only stops future monitoring.

---

--------------------------------------------------
### Start/Resume Source

**Method:** `POST` · **Endpoints:** `/scrape/api/v1/sources/{source_id}/start` and `/scrape/api/v1/sources/{source_id}/resume` (identical behavior, two aliases)

**Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}/start`

**Purpose:** Activates monitoring for a paused/new source.

**Authentication:** None. **Path parameters:** `source_id` (required). **Success response:** Updated source object, `status: "active"`.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/scrape/api/v1/sources/{source_id}/start"`

---

--------------------------------------------------
### Pause Source

**Method:** `POST` · **Endpoint:** `/scrape/api/v1/sources/{source_id}/pause` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}/pause`

**Purpose:** Temporarily stops monitoring without deleting the source.

**Authentication:** None. **Path parameters:** `source_id` (required). **Success response:** Updated source object, `status: "paused"`.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl -X POST "{BASE_URL}/scrape/api/v1/sources/{source_id}/pause"`

---

--------------------------------------------------
### List Source Events

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/sources/{source_id}/events` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}/events`

**Purpose:** Monitoring events for a source (new/updated/unchanged/removed detections).

**Authentication:** None. **Path parameters:** `source_id` (required).

**Success response:** Array of:
```json
{ "event_id": "...", "document_id": "...", "event_type": "UPDATED", "previous_version": 1, "new_version": 2, "detected_at": "...", "change_summary": {} }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/sources/{source_id}/events"`

---

--------------------------------------------------
### Get Source Statistics

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/sources/{source_id}/statistics` · **Full URL:** `{BASE_URL}/scrape/api/v1/sources/{source_id}/statistics`

**Purpose:** Summary counters for a monitored source.

**Authentication:** None. **Path parameters:** `source_id` (required).

**Success response:**
```json
{ "source_id": "...", "status": "active", "current_interval_seconds": 3600, "consecutive_unchanged_crawls": 2, "last_crawl_at": "...", "next_crawl_at": "...", "total_documents": 42, "total_events": 5 }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/sources/{source_id}/statistics"`

---

--------------------------------------------------
### Get Domain Profile

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/domains/{domain}/profile` · **Full URL:** `{BASE_URL}/scrape/api/v1/domains/{domain}/profile`

**Purpose:** Learned crawl-health/politeness profile for a domain (built up from prior crawl attempts).

**Authentication:** None. **Path parameters:** `domain` (string, required, e.g. `example.com`).

**Success response:** `{ "domain": "...", "health_score": 0.9, "insufficient_data": false, "signals": {}, "preferred_strategy": "http", "preferred_extractors": {}, "statistics": {}, "url_patterns": [], "politeness": {}, "recent_failures": {}, "routing_recommendation": {} }`

**HTTP status codes:** `200`, `404` (never crawled), `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/domains/example.com/profile"`

---

--------------------------------------------------
### Get Domain Capabilities

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/domains/{domain}/capabilities` · **Full URL:** `{BASE_URL}/scrape/api/v1/domains/{domain}/capabilities`

**Purpose:** Interpreted discovery/fetch/browser/extraction capability summary for a domain.

**Authentication:** None. **Path parameters:** `domain` (required).

**Success response:** `{ "domain": "...", "observations": 5, "confidence": "medium", "discovery": {}, "fetch": {}, "browser": {}, "extraction": [], "recommendation": {}, "last_observed_at": "..." }`

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/domains/example.com/capabilities"`

---

--------------------------------------------------
### List Entities

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/entities` · **Full URL:** `{BASE_URL}/scrape/api/v1/entities`

**Purpose:** Lists extracted named entities (people, organizations, locations, etc.) across all documents.

**Authentication:** None. **Query parameters:** `entity_type` (string, optional filter).

**Success response:** Array of:
```json
{ "entity_id": "...", "entity_type": "PERSON", "canonical_name": "Jane Doe", "normalized_name": "jane doe", "language": "en", "confidence": 0.9, "first_seen_at": "...", "last_seen_at": "..." }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/entities?entity_type=PERSON"`

---

--------------------------------------------------
### Get Entity

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/entities/{entity_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/entities/{entity_id}`

**Purpose:** One entity's detail.

**Authentication:** None. **Path parameters:** `entity_id` (string UUID, required). **Success response:** Same shape as one item from List Entities.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/entities/{entity_id}"`

---

--------------------------------------------------
### Get Entity Documents

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/entities/{entity_id}/documents` · **Full URL:** `{BASE_URL}/scrape/api/v1/entities/{entity_id}/documents`

**Purpose:** Document IDs that mention this entity.

**Authentication:** None. **Path parameters:** `entity_id` (required). **Success response:** `{ "entity_id": "...", "document_ids": ["..."] }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/entities/{entity_id}/documents"`

---

--------------------------------------------------
### Get Entity Stories

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/entities/{entity_id}/stories` · **Full URL:** `{BASE_URL}/scrape/api/v1/entities/{entity_id}/stories`

**Purpose:** Stories this entity is linked to.

**Authentication:** None. **Path parameters:** `entity_id` (required). **Success response:** `{ "entity_id": "...", "stories": [...] }`

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/entities/{entity_id}/stories"`

---

--------------------------------------------------
### List Stories

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/stories` · **Full URL:** `{BASE_URL}/scrape/api/v1/stories`

**Purpose:** Lists clustered "stories" (groups of documents about the same real-world event/topic).

**Authentication:** None. **Query parameters:** `status`, `from`, `to` (all optional).

**Success response:** Array of:
```json
{ "story_id": "...", "canonical_title": "Some event", "status": "active", "first_seen_at": "...", "last_activity_at": "...", "document_count": 5, "source_count": 3, "entity_count": 4 }
```

**HTTP status codes:** `200`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/stories"`

---

--------------------------------------------------
### Get Story

**Method:** `GET` · **Endpoint:** `/scrape/api/v1/stories/{story_id}` · **Full URL:** `{BASE_URL}/scrape/api/v1/stories/{story_id}`

**Purpose:** One story's summary. **Authentication:** None. **Path parameters:** `story_id` (required). **Success response:** Same shape as one item from List Stories.

**HTTP status codes:** `200`, `404`, `422`.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/stories/{story_id}"`

---

--------------------------------------------------
### Get Story Documents / Entities / Sources / Timeline

**Method:** `GET` · **Endpoints:**
- `/scrape/api/v1/stories/{story_id}/documents` — attachment scores per document
- `/scrape/api/v1/stories/{story_id}/entities` — entities in the story with mention counts
- `/scrape/api/v1/stories/{story_id}/sources` — contributing domains
- `/scrape/api/v1/stories/{story_id}/timeline` — chronological entries

**Full URL pattern:** `{BASE_URL}/scrape/api/v1/stories/{story_id}/<documents|entities|sources|timeline>`

**Authentication:** None. **Path parameters:** `story_id` (string UUID, required) for all four.

**Success responses:**
```json
// documents
[{ "document_id": "...", "match_score": 0.9, "match_method": "...", "confidence": "high", "feature_scores": {}, "matching_evidence": [], "scoring_version": "1", "attached_at": "..." }]
// entities
[{ "entity_id": "...", "entity_type": "PERSON", "canonical_name": "...", "mention_count": 3, "importance": 0.8 }]
// sources
[{ "domain": "...", "document_count": 2, "first_published_at": "...", "last_published_at": "..." }]
// timeline
[{ "document_id": "...", "domain": "...", "timestamp": "...", "match_method": "...", "confidence": "high" }]
```

**HTTP status codes:** `200`, `422` each.

**cURL:** `curl "{BASE_URL}/scrape/api/v1/stories/{story_id}/timeline"`

---

# 12. Bluweb Workflow

```
POST /scrape/api/v1/preflight              → preflight_id
        ↓
POST /scrape/api/v1/sources                → source_id  (needs preflight_id, same domain, not expired)
        │                                     (optional — skip straight to a crawl if you don't need recurring monitoring)
        ▼
POST /scrape/api/v1/crawls                 → crawl_id
        ↓
GET  /scrape/api/v1/crawls/{crawl_id}       (poll until status is completed/failed)
        ↓
GET  /scrape/api/v1/crawls/{crawl_id}/pages → document_id(s)
        ↓
GET  /scrape/api/v1/documents/{document_id}                       (metadata)
GET  /scrape/api/v1/documents/{document_id}/versions               (history list)
GET  /scrape/api/v1/documents/{document_id}/versions/{n}            (full content)
GET  /scrape/api/v1/documents/{document_id}/changes                 (what changed, when)
GET  /scrape/api/v1/documents/{document_id}/diff?from_version=&to_version=   (line diff between two versions)
```

**ID relationships:**

| ID | Returned by | Consumed by |
|---|---|---|
| `preflight_id` | `POST /preflight` | `POST /sources` |
| `source_id` | `POST /sources` | every `/sources/{id}/...` endpoint |
| `crawl_id` | `POST /crawls`, `POST /search/instant` | `GET /crawls/{id}`, `GET /crawls/{id}/pages`, `POST /crawls/{id}/cancel` |
| `document_id` | `GET /crawls/{id}/pages`, `GET /documents` | every `/documents/{id}/...` endpoint |
| `version_number` | `GET /documents/{id}/versions` | `GET /documents/{id}/versions/{n}`, `.../diff` |
| `entity_id` / `story_id` | `GET /documents/{id}` (`entity_ids`, `story_id`) | `/entities/{id}/...`, `/stories/{id}/...` |

You do not need a monitored Source to crawl a URL once — `POST /crawls` works standalone. Sources are only for recurring, scheduled monitoring.

---

# 13. Response Handling

**Fundamental rule: your application uses the HTTP response directly. Never attempt to access this API's underlying database, filesystem, or storage — everything you need comes back in the response body.**

**Synchronous endpoints** (most of Reddit, most of OSINT's single lookups, Bluweb preflight/search/document-retrieval, Telegram's live search/status):
```
Request → API processing → HTTP response (final result) → Your application
```
Nothing further to do — the response body is the complete answer.

**Asynchronous endpoints** (Bluweb crawls, OSINT investigations created via `POST /investigations`, Telegram backfill):
```
Request → job/investigation created → ID returned (202/201) → your app polls using that ID → final result
```
Store the ID, poll a lightweight status endpoint (not the heavy detail endpoint) until it reaches a terminal state, then fetch the full result.

---

# 14. Error Handling

Generic client-side pattern (works for all four modules):

```python
response = requests.post(url, json=payload, timeout=30)

if response.ok:  # 2xx
    data = response.json()
else:
    try:
        error_body = response.json()
    except ValueError:
        error_body = {"raw": response.text}
    # inspect response.status_code and error_body to decide how to handle it
```

Module-specific error shapes:

| Module | Shape | Notes |
|---|---|---|
| Bluweb | `{"error": {"code": "STRING_CODE", "message": "...", "details": {}, "request_id": "..."}}` for business-logic errors and 500s; plain FastAPI `{"detail": [...]}` for `422` validation errors specifically | Two different shapes depending on error type — check for the `error` key first, fall back to `detail` |
| OSINT | `{"error": {"code": <int status>, "message": "...", "request_id": "...", "details": [...] or null}}` for **every** error including `422` | `code` here is a number, not a string |
| Reddit | `{"error": {"code": "STRING_CODE", "message": "..."}}` (plus extra fields merged in for some errors) for both business errors and `422` validation | Consistent shape everywhere on this module |
| Telegram | `{"error": {"code": "STRING_CODE", "message": "...", "retryable": true/false, "retry_after_seconds": <int, optional>}}` for Telegram-specific errors (e.g. message not found); plain FastAPI `{"detail": [...]}` for `422` validation | `retryable` tells you whether retrying the same call might succeed later |

Categories you will encounter:
- **Validation errors** (`422`): malformed/missing request fields — fix the request, don't retry as-is.
- **Not found** (`404`): resource doesn't exist (Telegram message, Bluweb document/preflight, etc.) — don't retry.
- **Conflict** (`409`): OSINT idempotency-key body mismatch, cancel-on-terminal-investigation — inspect and decide, don't blindly retry.
- **Upstream/service unavailable** (`503`): Reddit OAuth not configured — this is a deployment configuration state, not a transient failure; retrying won't help until the deployment is reconfigured.
- **Rate limited** (`429`): OSINT's concurrent-investigation cap — back off and retry later (§17/§18).
- **Internal errors** (`500`): unexpected server-side failure — safe to retry once after a short delay; if persistent, it's not something your application can fix.
- **Unauthorized/forbidden**: not currently applicable to this deployment — no module enforces authentication that would produce `401`/`403` today (see §15 Authentication for the important caveat on this).

---

# 15. HTTP Status Codes

| Module | Status | Meaning |
|---|---|---|
| All | `200` | Success |
| Bluweb | `201` | Preflight / Source created |
| Bluweb | `202` | Crawl accepted, processing in background |
| Bluweb | `204` | Source deleted, no body |
| OSINT | `201` | Investigation created |
| OSINT | `409` | Idempotency-key conflict, or cancel on a terminal investigation |
| OSINT | `429` | Concurrent-investigation cap reached |
| Reddit | `503` | Reddit OAuth not configured on this deployment |
| Reddit | `400` | Unsupported combination (e.g. `type=comments` on unified search) |
| Telegram | `201` | Source created, search-result saved, channel registered |
| Telegram | `202` | Backfill job accepted |
| Telegram | `204` | Source deleted |
| Telegram | `404` | Message/photo/media not found |
| Telegram | `413` | Requested media exceeds the server's size limit |
| All | `422` | Request validation failed |
| All | `500` | Unexpected internal error |

---

# 16. Authentication

| Module | Authentication |
|---|---|
| Telegram | None — every endpoint is open to any caller reaching the base URL |
| Reddit | Optional `X-API-Key` header, controlled entirely by the deployment. **In the currently deployed environment this is not enforced** (no key configured) — confirm with your operator before depending on this remaining open |
| OSINT | None — every endpoint is open |
| Bluweb | None — every endpoint is open |

No module in this deployment currently requires a bearer token, session cookie, or `Authorization` header. **This means access control, if you need it, is handled outside the application** (e.g. network placement, a reverse proxy in front of the API) — not something your integration code needs to implement today, but also not something you should assume is guaranteed at every possible deployment of this API.

Never include real API keys, tokens, passwords, or Telegram session data in your own logs or version control — even though the deployed environment doesn't currently require Reddit's `X-API-Key`, treat it as a secret if/when one is issued.

---

# 17. Common Headers

| Header | When | Value |
|---|---|---|
| `Content-Type` | Every request with a JSON body (`POST`/`PATCH`/`PUT`) | `application/json` |
| `X-API-Key` | Every Reddit request, only if the deployment has enabled it | (deployment-issued key) |
| `Idempotency-Key` | Optional, on `POST /osint/api/v1/investigations` | Any string you choose — reusing it with the same body returns the existing investigation instead of creating a duplicate |

Response headers worth knowing: Bluweb and OSINT responses include `X-Request-ID` (useful for correlating a specific call with server-side logs if you ever need to report an issue).

---

# 18. Pagination

Three different pagination styles exist across the API — use the one each endpoint actually documents above:

| Style | Used by | How it works |
|---|---|---|
| `limit` / `offset` | Telegram messages/sources/notifications; Bluweb search | Pass `offset` = how many rows to skip, increase by `limit` each page |
| `limit` / `cursor` | Reddit posts/comments/users; Telegram live searches | First call omits `cursor`; response includes a `cursor` value (or `null`/absent when done) — pass it back as the `cursor` parameter for the next page |
| No pagination | Bluweb `GET /documents` (fixed ~50-row cap, narrow with filters instead); most OSINT/Bluweb list endpoints (return everything, no paging) | Use query filters to scope results instead of paging |

There is no universal maximum page size documented across modules — each endpoint's own default (shown in its parameter table above) is a reasonable value to start from.

---

# 19. Files and Media

Two Telegram endpoints return **binary bytes, not JSON**:

- `GET /telegram/api/telegram/channels/{channel_id}/photo` — `image/jpeg`
- `GET /telegram/api/telegram/channels/{channel_id}/messages/{message_id}/media` — `Content-Type` matches the actual media (photo/video/document)

Handle these as binary downloads:

```python
r = requests.get(f"{BASE_URL}/telegram/api/telegram/channels/{channel_id}/photo", timeout=15)
if r.status_code == 200:
    with open("photo.jpg", "wb") as f:
        f.write(r.content)
elif r.status_code == 404:
    print("no photo available")
```

```javascript
const r = await fetch(`${BASE_URL}/telegram/api/telegram/channels/${channelId}/photo`);
if (r.ok) {
  const blob = await r.blob();
  const imgUrl = URL.createObjectURL(blob);
}
```

Every other endpoint in the API returns JSON, even ones that reference URLs/files (e.g. Reddit's `media` array on a post is a JSON array of URL/metadata objects, not the media itself).

---

# 20. Rate Limiting / Throttling

- **Reddit**: the API paces its own outbound RSS calls to Reddit to avoid IP-level blocking, and may cap your application's own request rate to the `/reddit/api/reddit/rss/*` endpoints if `X-API-Key` auth is enabled on the deployment. A `429` means: **stop, wait, then retry** — do not immediately retry or increase request frequency.
- **OSINT**: `POST /investigations` returns `429` if the deployment's concurrent-investigation cap is already reached. Wait for existing investigations to finish before creating more, or implement your own queueing on the client side.
- **Telegram**: `global_search` operates against a Telegram method with its own daily quota — watch the `quota` field in its response and slow down as it approaches zero. `bots/{id}/start` and the auth endpoints trigger real Telegram-side actions (SMS, account interaction) — never call these in a loop.
- **Bluweb**: no explicit per-caller rate limit is enforced by the API itself; be considerate of the external sites you ask it to crawl (`max_pages`/`max_depth` control your own footprint).

Never attempt to bypass a `429`/`503` by hammering the endpoint faster or from multiple connections — back off with increasing delay (see §21).

---

# 21. Retry / Timeout Guidance

These are **client-side recommendations**, not requirements enforced by the API:

| Call type | Suggested client timeout | Retry guidance |
|---|---|---|
| Normal synchronous request (health, get-by-id, list) | 10-15s | Retry once on timeout/connection error; don't retry on 4xx |
| Bluweb preflight | 40s | Real DNS/HTTP work — don't retry aggressively, one retry is reasonable |
| Bluweb crawl (`POST /crawls`) | 15s (it returns immediately) | Then poll `GET /crawls/{id}` every 2-5s until terminal |
| Bluweb instant search | `wait_seconds` + 10s buffer | Single call, no retry needed — `crawl_id` lets you keep polling afterward |
| OSINT phone/email/domain/person lookup | 20-30s | Real outbound probes; one retry on timeout is reasonable |
| OSINT username lookup | 300s (several minutes) | This is the slowest endpoint in the API by design — do not set a short timeout here |
| OSINT investigation status poll | 10s, poll every 2-5s | Standard polling pattern |
| Reddit OAuth-backed calls | 15s | On `503` (not configured), don't retry — it's a configuration state, not transient |
| Reddit RSS calls | 15s | Cache means repeat identical calls are fast; on `429`, back off exponentially |
| Telegram live search/status | 15-20s | Real Telegram RPC; one retry reasonable on timeout |
| Telegram backfill poll | 10s, poll every 5s | Standard polling; job may never reach a terminal state if the server restarted mid-job (a known limitation) — apply your own overall timeout (e.g. give up after 30 minutes) |

---

# 22. Complete Integration Examples

### Telegram
```python
import requests, time

BASE_URL = "https://api.example.com"

# 1. Register a source
r = requests.post(f"{BASE_URL}/telegram/api/sources", json={"identifier": "somechannel"}, timeout=30)
source = r.json()
source_id = source["id"]

# 2. Start monitoring
requests.post(f"{BASE_URL}/telegram/api/sources/{source_id}/monitoring/start", timeout=15)

# 3. Later: read collected messages
r = requests.get(f"{BASE_URL}/telegram/api/sources/{source_id}/messages", params={"limit": 20}, timeout=10)
messages = r.json()
```

### Reddit
```python
import requests

BASE_URL = "https://api.example.com"

# Public RSS monitoring — no credentials needed
r = requests.post(f"{BASE_URL}/reddit/api/reddit/rss/monitor",
                   json={"query": "python", "limit": 20}, timeout=15)
feed = r.json()
for post in feed["posts"]:
    print(post["title"], post["url"])
```

### OSINT
```python
import requests, time

BASE_URL = "https://api.example.com"

r = requests.post(f"{BASE_URL}/osint/api/v1/investigations",
                   json={"input_identifier": "example.com", "identifier_type": "DOMAIN", "mode": "standard"},
                   timeout=15)
investigation_id = r.json()["id"]

while True:
    status = requests.get(f"{BASE_URL}/osint/api/v1/investigations/{investigation_id}/status", timeout=10).json()
    if status["status"] in ("completed", "failed", "cancelled"):
        break
    time.sleep(3)

report = requests.get(f"{BASE_URL}/osint/api/v1/investigations/{investigation_id}/report", timeout=10).json()
print(report)
```

### Bluweb
```python
import requests, time

BASE_URL = "https://api.example.com"

# 1. Preflight
preflight = requests.post(f"{BASE_URL}/scrape/api/v1/preflight",
                           json={"url": "https://example.com"}, timeout=40).json()

# 2. Crawl
crawl = requests.post(f"{BASE_URL}/scrape/api/v1/crawls",
                       json={"url": "https://example.com", "max_pages": 5}, timeout=15).json()
crawl_id = crawl["crawl_id"]

# 3. Poll
while True:
    job = requests.get(f"{BASE_URL}/scrape/api/v1/crawls/{crawl_id}", timeout=10).json()
    if job["status"] in ("completed", "failed"):
        break
    time.sleep(2)

# 4. Read documents found by this crawl
docs = requests.get(f"{BASE_URL}/scrape/api/v1/documents", params={"crawl_id": crawl_id}, timeout=10).json()
for doc in docs:
    version = requests.get(f"{BASE_URL}/scrape/api/v1/documents/{doc['document_id']}/versions/1", timeout=10).json()
    print(doc["title"], "->", version["content"][:200])
```

---

# 23. Quick Reference Table

| Service | Method | Endpoint | Purpose |
|---|---|---|---|
| Telegram | GET | `/telegram/health` | Liveness |
| Telegram | GET | `/telegram/ready` | Config readiness |
| Telegram | GET | `/telegram/api/telegram/status` | Connection/auth status |
| Telegram | POST | `/telegram/api/telegram/auth/send-code` | Start login (setup only) |
| Telegram | POST | `/telegram/api/telegram/auth/verify-code` | Complete login step 2 |
| Telegram | POST | `/telegram/api/telegram/auth/verify-password` | Complete 2FA |
| Telegram | POST | `/telegram/api/sources` | Register a source |
| Telegram | GET | `/telegram/api/sources` | List sources |
| Telegram | GET | `/telegram/api/sources/{id}` | Get source |
| Telegram | PATCH | `/telegram/api/sources/{id}` | Update source |
| Telegram | DELETE | `/telegram/api/sources/{id}` | Delete source |
| Telegram | POST | `/telegram/api/sources/{id}/request-access` | Request access |
| Telegram | GET | `/telegram/api/sources/{id}/access-status` | Access status |
| Telegram | POST | `/telegram/api/sources/{id}/check-access` | Re-probe access |
| Telegram | POST | `/telegram/api/sources/{id}/monitoring/start` | Start monitoring |
| Telegram | POST | `/telegram/api/sources/{id}/monitoring/stop` | Stop monitoring |
| Telegram | GET | `/telegram/api/sources/{id}/monitoring-status` | Monitoring status |
| Telegram | GET | `/telegram/api/sources/{id}/messages` | Stored messages (one source) |
| Telegram | GET | `/telegram/api/messages` | Stored messages (all sources) |
| Telegram | GET | `/telegram/api/messages/{id}` | One stored message |
| Telegram | GET | `/telegram/api/notifications` | List notifications |
| Telegram | POST | `/telegram/api/notifications/{id}/read` | Mark notification read |
| Telegram | POST | `/telegram/api/sources/{id}/backfill` | Start backfill |
| Telegram | GET | `/telegram/api/sources/{id}/backfill` | List backfill jobs |
| Telegram | GET | `/telegram/api/sources/{id}/backfill/{job_id}` | Poll backfill job |
| Telegram | POST | `/telegram/api/telegram/bots/{id}/start` | Start a bot, get its reply |
| Telegram | POST | `/telegram/api/telegram/discovery/extract-links` | Extract t.me links from text |
| Telegram | GET | `/telegram/api/telegram/channels/search` | Discover channels |
| Telegram | POST | `/telegram/api/telegram/channels/{id}/register` | Register discovered channel |
| Telegram | GET | `/telegram/api/telegram/channels/{id}/search` | Search one channel |
| Telegram | GET | `/telegram/api/sources/{id}/search` | Search one registered source |
| Telegram | GET | `/telegram/api/telegram/search/global` | Global search |
| Telegram | POST | `/telegram/api/telegram/search/sources` | Search selected sources |
| Telegram | POST | `/telegram/api/telegram/search/results/save` | Save a search result |
| Telegram | POST | `/telegram/api/telegram/channel` | Provider: channel metadata |
| Telegram | POST | `/telegram/api/telegram/channel/access` | Provider: access check |
| Telegram | POST | `/telegram/api/telegram/channel/messages` | Provider: recent messages |
| Telegram | POST | `/telegram/api/telegram/message` | Provider: single message |
| Telegram | POST | `/telegram/api/telegram/message/replies` | Provider: message replies |
| Telegram | POST | `/telegram/api/telegram/resolve` | Provider: resolve t.me link |
| Telegram | POST | `/telegram/api/telegram/invite/join` | Provider: join by invite |
| Telegram | POST | `/telegram/api/telegram/invites/join` | Join by invite (source-integrated) |
| Telegram | GET | `/telegram/api/telegram/search/channels` | Provider: discover channels |
| Telegram | GET | `/telegram/api/telegram/search/messages` | Provider: search messages |
| Telegram | GET | `/telegram/api/telegram/channels/{id}/photo` | Fetch profile photo (binary) |
| Telegram | GET | `/telegram/api/telegram/channels/{cid}/messages/{mid}/media` | Fetch message media (binary) |
| Reddit | GET | `/reddit/health` | Liveness |
| Reddit | GET | `/reddit/ready` | Config readiness |
| Reddit | GET | `/reddit/api/reddit/status` | Connection status (diagnostic only) |
| Reddit | POST | `/reddit/api/reddit/subreddit` | Subreddit metadata |
| Reddit | POST | `/reddit/api/reddit/subreddit/access` | Subreddit accessibility |
| Reddit | POST | `/reddit/api/reddit/subreddit/posts` | Subreddit posts |
| Reddit | POST | `/reddit/api/reddit/post` | Post details |
| Reddit | POST | `/reddit/api/reddit/post/comments` | Post comments |
| Reddit | POST | `/reddit/api/reddit/comment` | Comment details |
| Reddit | POST | `/reddit/api/reddit/user` | User profile |
| Reddit | POST | `/reddit/api/reddit/user/posts` | Posts by user |
| Reddit | POST | `/reddit/api/reddit/user/comments` | Comments by user |
| Reddit | GET | `/reddit/api/reddit/search/posts` | Search posts |
| Reddit | GET | `/reddit/api/reddit/search/subreddits` | Search subreddits |
| Reddit | GET | `/reddit/api/reddit/search` | Unified search |
| Reddit | POST | `/reddit/api/reddit/resolve` | Resolve a Reddit URL |
| Reddit | POST | `/reddit/api/reddit/rss/monitor` | RSS monitor (POST) |
| Reddit | GET | `/reddit/api/reddit/rss/search` | RSS monitor (GET) |
| Reddit | POST | `/reddit/api/reddit/rss/event` | RSS event monitoring |
| OSINT | GET | `/osint/health` | Liveness |
| OSINT | GET | `/osint/ready` | DB readiness |
| OSINT | POST | `/osint/api/v1/phone/lookup` | Phone lookup |
| OSINT | POST | `/osint/api/v1/email/lookup` | Email lookup |
| OSINT | POST | `/osint/api/v1/username/lookup` | Username lookup (slow) |
| OSINT | POST | `/osint/api/v1/domain/lookup` | Domain lookup |
| OSINT | POST | `/osint/api/v1/person/lookup` | Person lookup |
| OSINT | POST | `/osint/api/v1/investigations` | Create investigation |
| OSINT | GET | `/osint/api/v1/investigations` | List investigations |
| OSINT | GET | `/osint/api/v1/investigations/{id}` | Full detail |
| OSINT | GET | `/osint/api/v1/investigations/{id}/status` | Poll status |
| OSINT | GET | `/osint/api/v1/investigations/{id}/report` | Human-readable report |
| OSINT | GET | `/osint/api/v1/investigations/{id}/graph` | Entity graph |
| OSINT | GET | `/osint/api/v1/investigations/{id}/timeline` | Timeline |
| OSINT | PUT | `/osint/api/v1/investigations/{id}/notes` | Update notes |
| OSINT | POST | `/osint/api/v1/investigations/{id}/cancel` | Cancel |
| OSINT | GET/POST | `/osint/api/v1/search` | Public web search |
| OSINT | GET | `/osint/api/v1/sources/health` | Adapter health |
| OSINT | POST | `/osint/api/v1/utils/pwned-password` | Pwned password check |
| OSINT | GET | `/osint/metrics` | Prometheus metrics |
| Bluweb | GET | `/scrape/health` / `/health/live` / `/health/ready` | Health/readiness |
| Bluweb | GET | `/scrape/metrics` | Prometheus metrics |
| Bluweb | POST | `/scrape/api/v1/preflight` | Run preflight |
| Bluweb | GET | `/scrape/api/v1/preflight/{id}` | Get preflight report |
| Bluweb | POST | `/scrape/api/v1/crawls` | Create crawl |
| Bluweb | GET | `/scrape/api/v1/crawls` | List crawls |
| Bluweb | GET | `/scrape/api/v1/crawls/{id}` | Get crawl status |
| Bluweb | POST | `/scrape/api/v1/crawls/{id}/cancel` | Cancel crawl |
| Bluweb | GET | `/scrape/api/v1/crawls/{id}/pages` | List crawl pages |
| Bluweb | GET | `/scrape/api/v1/documents` | List documents |
| Bluweb | GET | `/scrape/api/v1/documents/{id}` | Get document |
| Bluweb | GET | `/scrape/api/v1/documents/{id}/versions` | List versions |
| Bluweb | GET | `/scrape/api/v1/documents/{id}/versions/{n}` | Get version content |
| Bluweb | GET | `/scrape/api/v1/documents/{id}/changes` | List changes |
| Bluweb | GET | `/scrape/api/v1/documents/{id}/diff` | Diff two versions |
| Bluweb | POST | `/scrape/api/v1/search` | Search documents |
| Bluweb | POST | `/scrape/api/v1/search/instant` | Crawl + wait for results |
| Bluweb | POST | `/scrape/api/v1/sources` | Create monitored source |
| Bluweb | GET | `/scrape/api/v1/sources` | List sources |
| Bluweb | GET | `/scrape/api/v1/sources/{id}` | Get source |
| Bluweb | PATCH | `/scrape/api/v1/sources/{id}` | Update source |
| Bluweb | DELETE | `/scrape/api/v1/sources/{id}` | Delete source |
| Bluweb | POST | `/scrape/api/v1/sources/{id}/start` \| `/resume` | Activate source |
| Bluweb | POST | `/scrape/api/v1/sources/{id}/pause` | Pause source |
| Bluweb | GET | `/scrape/api/v1/sources/{id}/events` | List source events |
| Bluweb | GET | `/scrape/api/v1/sources/{id}/statistics` | Source statistics |
| Bluweb | GET | `/scrape/api/v1/domains/{domain}/profile` | Domain profile |
| Bluweb | GET | `/scrape/api/v1/domains/{domain}/capabilities` | Domain capabilities |
| Bluweb | GET | `/scrape/api/v1/entities` | List entities |
| Bluweb | GET | `/scrape/api/v1/entities/{id}` | Get entity |
| Bluweb | GET | `/scrape/api/v1/entities/{id}/documents` | Entity's documents |
| Bluweb | GET | `/scrape/api/v1/entities/{id}/stories` | Entity's stories |
| Bluweb | GET | `/scrape/api/v1/stories` | List stories |
| Bluweb | GET | `/scrape/api/v1/stories/{id}` | Get story |
| Bluweb | GET | `/scrape/api/v1/stories/{id}/documents` \| `/entities` \| `/sources` \| `/timeline` | Story detail views |

---

# 24. OpenAPI

Machine-readable schemas (always in sync with the live deployment — prefer these for generating a typed client):

```
{BASE_URL}/scrape/openapi.json
{BASE_URL}/osint/openapi.json
{BASE_URL}/reddit/openapi.json
{BASE_URL}/telegram/openapi.json
```

Interactive Swagger UI is also available at each module's `/docs` path:

```
{BASE_URL}/scrape/docs
{BASE_URL}/osint/docs
{BASE_URL}/reddit/docs
{BASE_URL}/telegram/docs
```

(No ReDoc UI is enabled on this deployment.)

---

# Implementation Report

- **Total client-facing endpoints found:** 117 unique paths (145 method-rows, counting each HTTP method on a shared path separately), extracted directly from the four live `openapi.json` documents on the deployed server.
- **Telegram endpoints documented:** 44 (all client-facing routes, including the Provider sub-API, auth handshake, sources/messages/notifications/backfill/bots/discovery/search/media, plus health/ready).
- **Reddit endpoints documented:** 19 (all OAuth-backed and RSS routes, plus health/ready).
- **OSINT endpoints documented:** 19 (all five lookup types, full investigation lifecycle, search, source health, pwned-password check, plus health/ready/metrics).
- **Bluweb endpoints documented:** 37 (preflight, crawls, documents/versions/changes/diff, search/instant-search, sources, domains, entities, stories, plus health/live/ready/metrics).
- **Endpoints intentionally excluded:** none of the 117 discovered paths were excluded; the three Telegram `auth/*` setup endpoints are documented but explicitly flagged as operational/setup-time actions rather than routes a consuming application calls as part of normal traffic.
- **Request/response schemas verified:** extracted directly from each module's live `openapi.json` (component schemas resolved, including nested/array/union types), cross-checked against the actual exception-handler source code for accurate error-body shapes (which OpenAPI's autogenerated `422` schema does not always reflect for modules that override validation-error handling, e.g. OSINT and Reddit).
- **Authentication verified:** confirmed by reading each module's actual security dependency wiring (`require_api_key` for Reddit, none registered for the other three) and the deployment's current `.env` state (Reddit OAuth/API-key not configured in this deployment).
- **Example integrations created:** one complete, runnable Python example per module (§22), plus inline cURL/Python/JavaScript for every individual endpoint above.

The Unified Saga API developer integration guide has been generated from the actual deployed API routes, OpenAPI schemas, and implementation behavior. No application behavior was changed.

