# OSINT API Endpoints

Base URL:

`http://<SERVER_HOST>:<API_PORT>`

The app has no authentication by design. If a client can reach the port, it can call every endpoint.

Every response includes `X-Request-ID`. If the caller sends one, the server echoes it back; otherwise the server generates one. Structured errors include the same request ID in the JSON body.

## Status and error semantics

Use these meanings consistently across the API:

| Outcome | Meaning |
|---|---|
| Successful result | The requested source or workflow completed and returned data |
| Empty result | The source ran successfully but found nothing |
| Partial result | Some jobs completed and at least one job was unavailable or failed |
| Source unavailable | The backing tool, dependency, or search backend was missing or unreachable |
| Source failure | The source ran but errored unexpectedly |
| Invalid input | Validation or normalization failed before a source ran |
| Job failure | An investigation or job reached a terminal failed state |

## Active intelligence services

| Area | Implementation |
|---|---|
| PHONE | `phonenumbers` |
| EMAIL | `holehe` |
| USERNAME | Sherlock, Maigret |
| DOMAIN | `domain_recon` using DNS/NS, MX, SPF, DMARC, and optional WHOIS |
| PERSON_NAME | Wikidata |
| SEARCH | SearxNG |
| `public_web` | Used internally by the search endpoint and by person/domain/investigation flows |
| `document_extraction` | Used internally by deep investigations to mine discovered document URLs |

MCA and eCourts are not active intelligence sources in the current codebase. They are not exposed as working endpoints and must not be described as available capabilities.

## Confidence and evidence rules

The implementation is intentionally conservative:

- Phone results from `phonenumbers` are technical, not ownership proof.
- Email, username, person, and domain results are public associations, not identity claims.
- Confidence is computed from corroborating source names, exact-match signals, recency, and source reliability.
- A result that shows an account exists does not prove the subject owns that account.

For every lookup or investigation, the internal flow is:

`request` -> `normalization` -> `adapter execution` -> `evidence` -> `confidence` -> `response`

## Route inventory

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/ready` | Database readiness check |
| GET | `/metrics` | Prometheus metrics |
| GET | `/api/v1/search` | SearxNG search |
| POST | `/api/v1/search` | SearxNG search |
| POST | `/api/v1/phone/lookup` | Phone lookup |
| POST | `/api/v1/email/lookup` | Email lookup |
| POST | `/api/v1/username/lookup` | Username lookup |
| POST | `/api/v1/person/lookup` | Person-name lookup |
| POST | `/api/v1/domain/lookup` | Domain lookup |
| POST | `/api/v1/investigations` | Create investigation |
| GET | `/api/v1/investigations` | List investigations |
| GET | `/api/v1/investigations/{investigation_id}` | Investigation detail |
| GET | `/api/v1/investigations/{investigation_id}/status` | Investigation progress |
| POST | `/api/v1/investigations/{investigation_id}/cancel` | Cancel investigation |
| GET | `/api/v1/investigations/{investigation_id}/report` | Investigation report |
| GET | `/api/v1/investigations/{investigation_id}/graph` | Investigation graph |
| GET | `/api/v1/investigations/{investigation_id}/timeline` | Investigation timeline |
| PUT | `/api/v1/investigations/{investigation_id}/notes` | Update analyst notes |
| GET | `/api/v1/sources/health` | Source health status |
| POST | `/api/v1/utils/pwned-password` | Pwned Passwords utility |

## Common response and error format

Successful JSON responses use the route's schema.

Structured error responses use this shape:

```json
{
  "error": {
    "code": 422,
    "message": "request validation failed",
    "request_id": "c0f0a4c3-6dc3-4e9f-8a7a-0a9f3df6e8f0",
    "details": []
  }
}
```

The app's exception handlers return this shape for `HTTPException` and request validation errors.

## `/health`

- Method: `GET`
- Path: `/health`
- Purpose: basic liveness probe
- Auth: none

### Response

- `200 OK`: `{"status":"ok"}`

### Errors

- No app-specific error body is defined here.

### Example

```bash
curl -i "http://<SERVER_HOST>:<API_PORT>/health"
```

## `/ready`

- Method: `GET`
- Path: `/ready`
- Purpose: verify the database is reachable
- Auth: none

### Response

- `200 OK`: `{"status":"ready"}` when the database accepts a simple `SELECT 1`
- `503 Service Unavailable`: `{"status":"not_ready","reason":"..."}` when the database check fails

### Example

```bash
curl -i "http://<SERVER_HOST>:<API_PORT>/ready"
```

## `/metrics`

- Method: `GET`
- Path: `/metrics`
- Purpose: Prometheus text exposition for per-process counters
- Auth: none

### Response

Text/plain Prometheus metrics such as:

```text
# TYPE osint_jobs_total counter
osint_jobs_total 0
```

### Notes

- Counters are per process.
- Aggregation happens in Prometheus, not in the API.

## `/api/v1/search` GET and POST

- Method: `GET` or `POST`
- Path: `/api/v1/search`
- Purpose: public web search through the shared SearxNG client
- When to use it: when you need search results directly or want to inspect the same search backend used by `public_web`
- Auth: none

### GET parameters

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `q` | string | yes | - | Search query, minimum length 1 |
| `page` | integer | no | `1` | Must be `>= 1` |
| `language` | string | no | `en` | Passed through to SearxNG |
| `safesearch` | integer | no | `0` | Must be between `0` and `2` |

### POST body

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `query` | string | yes | - | Search query, minimum length 1 |
| `page` | integer | no | `1` | Must be `>= 1` |
| `language` | string | no | `en` | Passed through to SearxNG |
| `safesearch` | integer | no | `0` | Must be between `0` and `2` |

### Successful response

`SearchResponseOut`:

- `query`: normalized query string that was sent
- `results`: list of search hits
- `meta`: search summary

Each result item contains:

- `title`
- `url`
- `snippet`
- `engine`
- `category`
- `published_at` optionally

`meta` contains:

- `total`: number of returned results
- `successful_engines`: engines that returned results
- `failed_engines`: engines that were unresponsive or otherwise failed

### Status codes

- `200 OK` on success, even when `results` is empty
- `503 Service Unavailable` when SearxNG is not configured or is unreachable
- `422 Unprocessable Entity` for invalid query/body values

### Empty vs unavailable

- Empty result means SearxNG ran successfully but returned no hits.
- Unavailable means the client could not reach a configured SearxNG backend or `OSINT_SEARXNG_URL` is unset.

### Example requests

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/search?q=open%20source%20intelligence&page=1"
```

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/search" \
  -H 'Content-Type: application/json' \
  -d '{"query":"open source intelligence","page":1,"language":"en","safesearch":0}'
```

## `/api/v1/phone/lookup`

- Method: `POST`
- Path: `/api/v1/phone/lookup`
- Purpose: deterministic phone metadata lookup
- When to use it: when you want the number parsed and normalized, plus technical metadata from `phonenumbers`
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `phone` | string | yes | Must be non-empty |

### Processing flow

`request` -> `normalization` -> `phonenumbers` adapter -> evidence -> confidence -> response

### Successful response

`LookupResultOut` with:

- `investigation_id`
- `input_identifier`
- `identifier_type` = `PHONE`
- `normalized_identifier` in E.164 form
- `status`
- `jobs`
- `entities`
- `evidence`

For phone lookups, the adapter is technical only:

- `claim_type` is `TECHNICAL`
- `confidence` is `1.0` for the adapter output
- the returned `technical_metadata` is stored on the investigation, not in the lookup response schema

### Status codes

- `200 OK`
- `422 Unprocessable Entity` if normalization fails or the request body is invalid

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/phone/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"phone":"+14155552671"}'
```

## `/api/v1/email/lookup`

- Method: `POST`
- Path: `/api/v1/email/lookup`
- Purpose: public account-presence probes via Holehe
- When to use it: when you need a synchronous email lookup without automatic pivots
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `email` | string | yes | Must be non-empty |

### Processing flow

`request` -> `normalization` -> `holehe` adapter -> evidence -> confidence -> response

### Successful response

`LookupResultOut` with the same top-level fields as the phone lookup.

Evidence rows represent public associations, not proof of account ownership.

### Status codes

- `200 OK`
- `422 Unprocessable Entity`

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/email/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"email":"example@example.com"}'
```

## `/api/v1/username/lookup`

- Method: `POST`
- Path: `/api/v1/username/lookup`
- Purpose: synchronous username investigation via Sherlock and Maigret
- When to use it: when you need a single-identifier username check and can tolerate a longer request time
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `username` | string | yes | Must be non-empty |

### Processing flow

`request` -> `normalization` -> `Sherlock` and `Maigret` adapters -> evidence -> confidence -> response

### Successful response

`LookupResultOut`.

Implementation detail:

- The endpoint merges Sherlock and Maigret evidence for the same site into one response row keyed by registrable host.
- The stored evidence is not collapsed; the merge only affects the direct lookup response.

### Status codes

- `200 OK`
- `422 Unprocessable Entity`

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/username/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"username":"example_username"}'
```

## `/api/v1/person/lookup`

- Method: `POST`
- Path: `/api/v1/person/lookup`
- Purpose: person-name lookup using Wikidata plus public web search
- When to use it: when you want structured, conservative name-based enrichment and search corroboration
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `person_name` | string | yes | Must be non-empty |

### Processing flow

`request` -> `normalization` -> `Wikidata` adapter + `public_web` -> evidence -> confidence -> response

### Successful response

`LookupResultOut`.

Notes:

- Exact-name Wikidata matches are intentionally conservative.
- A same-name match is not treated as proof of identity.
- Structured claims from Wikidata may include official websites and social profiles when present in Wikidata itself.

### Status codes

- `200 OK`
- `422 Unprocessable Entity`

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/person/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"person_name":"Linus Torvalds"}'
```

## `/api/v1/domain/lookup`

- Method: `POST`
- Path: `/api/v1/domain/lookup`
- Purpose: DNS/WHOIS recon plus public web search
- When to use it: when you want technical domain facts and search corroboration
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `domain` | string | yes | Must be non-empty |

### Processing flow

`request` -> `normalization` -> `domain_recon` adapter + `public_web` -> evidence -> confidence -> response

### Successful response

`LookupResultOut`.

WHOIS fallback behavior:

- if `whois` is installed: DNS + MX + SPF + DMARC + WHOIS
- if `whois` is missing: DNS + MX + SPF + DMARC

### Status codes

- `200 OK`
- `422 Unprocessable Entity`

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/domain/lookup" \
  -H 'Content-Type: application/json' \
  -d '{"domain":"anthropic.com"}'
```

## `/api/v1/investigations` POST

- Method: `POST`
- Path: `/api/v1/investigations`
- Purpose: create an asynchronous investigation
- When to use it: when you want automatic source fan-out, pivoting, and optional document mining
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `input_identifier` | string | yes | Raw identifier to investigate |
| `identifier_type` | enum | no | One of `PHONE`, `EMAIL`, `USERNAME`, `PERSON_NAME`, `DOMAIN`; auto-detected when omitted |
| `analyst_notes` | string or null | no | Freeform notes stored on the investigation |
| `mode` | enum | no | `quick`, `standard`, or `deep`; default `standard` |

### Idempotency

`Idempotency-Key` is supported.

- same key + same body returns the original investigation
- same key + different body returns `409`
- without the header, the request is not idempotent at the API layer

### Lifecycle

`created` -> `queued/running` -> `pivoting` -> terminal state

Terminal states supported by the code:

- `completed`
- `partial`
- `failed`
- `cancelled`
- `unavailable`

### Successful response

`201 Created` with `InvestigationSummary`:

- `id`
- `input_identifier`
- `identifier_type`
- `normalized_identifier`
- `mode`
- `status`
- `cancel_requested`
- `created_at`
- `updated_at`

The initial status is typically `queued`.

### Status codes

- `201 Created`
- `409 Conflict` if the same idempotency key was already used with a different body
- `429 Too Many Requests` if `OSINT_MAX_CONCURRENT_INVESTIGATIONS` is reached
- `422 Unprocessable Entity` if normalization or request validation fails

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations" \
  -H 'Content-Type: application/json' \
  -d '{"input_identifier":"example@example.com","mode":"standard"}'
```

## `/api/v1/investigations` GET

- Method: `GET`
- Path: `/api/v1/investigations`
- Purpose: list investigations
- Auth: none

### Query parameters

| Parameter | Type | Required | Default | Notes |
|---|---|---|---|---|
| `limit` | integer | no | `50` | Capped at `200` |

### Response

`200 OK` with `InvestigationSummary[]` ordered by `created_at` descending.

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations?limit=10"
```

## `/api/v1/investigations/{investigation_id}` GET

- Method: `GET`
- Path: `/api/v1/investigations/{investigation_id}`
- Purpose: retrieve the full investigation record and all stored relations
- Auth: none

### Path parameters

| Parameter | Type | Required |
|---|---|---|
| `investigation_id` | string | yes |

### Response

`200 OK` with `InvestigationDetail`:

- summary fields from `InvestigationSummary`
- `technical_metadata`
- `analyst_notes`
- `jobs`: all `SearchJob` rows
- `entities`: all discovered entities
- `relationships`: all entity relationships
- `evidence`: all evidence rows
- `pivots`: all pivot records, including skipped pivots

### Field notes

- `technical_metadata` holds deterministic technical data such as phone or domain recon metadata.
- `jobs` reflect adapter execution state.
- `pivots` includes both executed and skipped pivot decisions.

### Status codes

- `200 OK`
- `404 Not Found` if the investigation does not exist
- `422 Unprocessable Entity` for invalid request shape

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>"
```

## `/api/v1/investigations/{investigation_id}/status` GET

- Method: `GET`
- Path: `/api/v1/investigations/{investigation_id}/status`
- Purpose: lightweight progress polling
- When to use it: when you want to poll without loading the full entity/evidence graph
- Auth: none

### Response

`InvestigationStatusOut`:

- `id`
- `status`
- `mode`
- `cancel_requested`
- `elapsed_seconds`
- `jobs`
- `pivots`

`jobs` is `JobCounts`:

- `total`
- `queued`
- `running`
- `completed`
- `failed`
- `unavailable`
- `cancelled`

`pivots` is `PivotCounts`:

- `total`
- `pending`
- `running`
- `completed`
- `skipped`

### Status codes

- `200 OK`
- `404 Not Found`
- `422 Unprocessable Entity`

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>/status"
```

## `/api/v1/investigations/{investigation_id}/cancel` POST

- Method: `POST`
- Path: `/api/v1/investigations/{investigation_id}/cancel`
- Purpose: request cooperative cancellation
- Auth: none

### Behavior

- Sets `cancel_requested = true`
- Stops new adapter calls and new pivots from starting
- Does not abort work already in flight mid-write

### Response

`200 OK` with `InvestigationSummary` for the updated investigation.

### Status codes

- `200 OK`
- `404 Not Found`
- `409 Conflict` if the investigation is already in a terminal state
- `422 Unprocessable Entity`

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>/cancel"
```

## `/api/v1/investigations/{investigation_id}/report` GET

- Method: `GET`
- Path: `/api/v1/investigations/{investigation_id}/report`
- Purpose: human-readable structured case report
- Auth: none

### Response shape

The response is a JSON object with these top-level keys:

- `investigation_id`
- `case`
- `sources`
- `technical_data`
- `discovered_entities`
- `relationships`
- `evidence`
- `pivots`
- `limitations`
- `analyst_notes`

### Field notes

- `case` includes input identifier, detected type, normalized identifier, mode, timestamp, and status.
- `sources.queried` lists sources that were attempted.
- `sources.completed`, `sources.unavailable`, and `sources.failed` split job outcomes by source name.
- `technical_data` contains merged technical metadata, such as phone or domain recon details.
- `discovered_entities` groups non-technical entities by type.
- `limitations` explicitly states what the system cannot prove, including the fact that public association is not identity verification.

### Status codes

- `200 OK`
- `404 Not Found`
- `422 Unprocessable Entity`

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>/report"
```

## `/api/v1/investigations/{investigation_id}/graph` GET

- Method: `GET`
- Path: `/api/v1/investigations/{investigation_id}/graph`
- Purpose: machine-readable entity graph
- Auth: none

### Response shape

`200 OK` with:

- `investigation_id`
- `nodes`
- `edges`

`nodes` items contain:

- `id`
- `type`
- `label`
- `claim_type`
- `confidence`
- `is_root`

`edges` items contain:

- `id`
- `source`
- `target`
- `type`
- `confidence`

### Status codes

- `200 OK`
- `404 Not Found`
- `422 Unprocessable Entity`

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>/graph"
```

## `/api/v1/investigations/{investigation_id}/timeline` GET

- Method: `GET`
- Path: `/api/v1/investigations/{investigation_id}/timeline`
- Purpose: chronological event feed
- Auth: none

### Response shape

`200 OK` with a list of events sorted by timestamp.

Event types currently emitted by the implementation:

- `investigation_created`
- `job_started`
- `job_completed`
- `job_failed`
- `job_partial`
- `job_unavailable`
- `job_cancelled`
- `entity_discovered`
- `pivot_decided`

### Status codes

- `200 OK`
- `404 Not Found`
- `422 Unprocessable Entity`

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>/timeline"
```

## `/api/v1/investigations/{investigation_id}/notes` PUT

- Method: `PUT`
- Path: `/api/v1/investigations/{investigation_id}/notes`
- Purpose: update analyst notes on an existing investigation
- Auth: none

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `analyst_notes` | string | yes | Freeform text |

### Response

`200 OK` with `InvestigationSummary`.

### Status codes

- `200 OK`
- `404 Not Found`
- `422 Unprocessable Entity`

### Example

```bash
curl -X PUT "http://<SERVER_HOST>:<API_PORT>/api/v1/investigations/<id>/notes" \
  -H 'Content-Type: application/json' \
  -d '{"analyst_notes":"Check public-web corroboration before escalation."}'
```

## `/api/v1/sources/health` GET

- Method: `GET`
- Path: `/api/v1/sources/health`
- Purpose: source inventory and reachability status
- Auth: none

### Response

Array of `SourceHealthOut` rows, one per registered adapter plus a live SearxNG row.

Fields:

- `source_name`
- `enabled`
- `installed`
- `version`
- `last_success`
- `last_failure`
- `last_error`
- `success_count`
- `failure_count`
- `timeout_count`
- `success_rate`
- `average_duration_seconds`
- `run_count`
- `status`

### Status derivation

`status` is computed as follows:

- `disabled` when the row exists but is disabled
- `unavailable` when the adapter is not installed or SearxNG is unreachable
- `degraded` when success rate is below 0.5
- `working` otherwise

### Status codes

- `200 OK`

### Example

```bash
curl "http://<SERVER_HOST>:<API_PORT>/api/v1/sources/health"
```

## `/api/v1/utils/pwned-password` POST

- Method: `POST`
- Path: `/api/v1/utils/pwned-password`
- Purpose: HIBP Pwned Passwords exposure check
- When to use it: when you need to know whether a password has appeared in public breach corpora
- Auth: none

### Important scope note

This is a password utility only. It is not an email breach lookup and is not part of the identifier investigation pipeline.

### Request body

| Field | Type | Required | Notes |
|---|---|---|---|
| `password` | string | yes | Must be non-empty |

### Successful response

`PwnedPasswordResponse`:

- `pwned`: boolean
- `times_seen`: integer
- `note`: fixed explanatory text

### Status codes

- `200 OK`
- `422 Unprocessable Entity` if the password is empty or HIBP is unavailable

### Example

```bash
curl -X POST "http://<SERVER_HOST>:<API_PORT>/api/v1/utils/pwned-password" \
  -H 'Content-Type: application/json' \
  -d '{"password":"correcthorsebatterystaple"}'
```

## Investigation lifecycle summary

The investigation state machine uses these states:

- `queued`
- `running`
- `completed`
- `failed`
- `partial`
- `cancelled`
- `unavailable`

Operationally, the flow is:

`created` -> `queued/running` -> `pivoting` -> terminal state

## Response interpretation guide

- `completed`: all attempted jobs finished successfully
- `partial`: at least one job completed and at least one other job was unavailable or failed
- `failed`: the investigation had no successful jobs and did not fall into `unavailable`
- `unavailable`: every attempted source was unavailable
- `cancelled`: cancellation was requested before completion

## No-identity guarantee

Do not interpret any successful lookup as proof of ownership or identity. The code intentionally models public-source associations, technical facts, and corroboration, not verified subscriber records.
