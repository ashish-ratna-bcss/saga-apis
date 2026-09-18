# Result Storage / Response-Only Architecture Investigation

Investigation only — no code, config, data, or production state was changed to produce this document.

## 1. Executive Summary

**Direct answer: NO.** Across all four services, no endpoint persists the literal API-request result merely because it needs to return it. Every write found either (a) serves a separate, later-called endpoint (async job status polling, historical document/version/investigation retrieval), (b) is a genuine read-through cache, or (c) is the two already-known, already-configurable archival writes from Milestone 4 (Bluweb MinIO, Telegram raw evidence). This investigation traced every write mechanism in all four codebases (Phase 1) and every representative endpoint's request→result→persistence path (Phase 2) and found **zero new response-only candidates** beyond what Milestone 4 already identified and already made optional.

## 2. Current Request Lifecycle (observed pattern, all four services)

Two lifecycle shapes exist, by design, never a wasteful third:
```
Synchronous, single-endpoint results (Reddit everything; OSINT sync lookups' HTTP
response; Bluweb preflight's HTTP response; Telegram live search/status):
    request → process → result object → HTTP response
    (persistence, if any, is a SIDE EFFECT for a *different* later endpoint,
     not a read-back for *this* response)

Asynchronous, job-style results (Bluweb crawls; OSINT investigations;
Telegram backfill):
    request → create job row → 202/201 + job id → HTTP response
    (a *separate* later request polls/reads the job — this is not
     "unnecessarily storing a response", it is the entire point of the
     async contract)
```

## 3. Bluweb storage analysis

### 3.1 Postgres (single physical table `webintel_unified`, discriminated by `record_kind`)

| record_kind | Writer(s) | Reader(s) | Used by endpoint(s) | Returned to client? | Required? | Classification |
|---|---|---|---|---|---|---|
| `crawl_job` | `crawl_repository.py` (`create_job`, `mark_started`, etc.) | same repo's `get_job`/`list_jobs` | `POST /crawls` (write), `GET /crawls`, `GET /crawls/{id}` (read, later call) | Yes, but the *stored* row is what a **separate, later** `GET` returns — not re-read within the same request | Yes — this is the entire async-crawl contract | **D operational** |
| `crawl_page` | `record_fetch_outcome` (crawl_repository.py) | `get_pages` | `GET /crawls/{id}/pages` | Yes, via a separate endpoint | Yes | **D operational** |
| `document` (+ versions) | `document_repository.py` (`create`, version-append logic) | same repo's `get`, `list`, `get_version`, `list_versions` | `GET /documents`, `/documents/{id}`, `/versions`, `/versions/{n}`, `/changes`, `/diff` | Yes — but always via a **separate GET call**, made at an arbitrary later time (the whole point of history/versioning) | Yes — explicitly the product feature these six endpoints exist for | **C required historical state** |
| `source` | `source_repository.py` | same | `POST/GET/PATCH/DELETE /sources*` | Yes, via separate calls | Yes | **D operational** |
| `preflight` | `preflight_repository.py::save` (called once, inside `POST /preflight`) | same repo's `get` | `POST /preflight` (write+return), `GET /preflight/{id}` (read, later call); **also a hard dependency of `POST /sources`**, which requires "a valid, unexpired preflight for the same domain" | The `POST /preflight` response is built from the just-inserted row (`_row_to_response(row)`) to pick up DB-generated fields (id, timestamps) the client needs to later call `GET /preflight/{id}` and `POST /sources` | Yes — proven by the cross-endpoint dependency, not just "nice to have" | **C required** (not response-only: verified no extra SELECT round-trip exists either — a single insert+flush, not a wasteful read-after-write) |
| `entity` / `story` | `intelligence_repository.py`, written from `story_service.py::process_document_intelligence` (runs as part of ingesting a document, not a client-facing request) | same repo | `GET /entities*`, `/stories*` | Yes, via separate calls | Yes | **C required historical state** |

**Confirmed no separate "search_results" or "response cache" table exists anywhere.** `POST /search` and `POST /search/instant` were checked line-by-line (`bluweb_app/api/v1/search.py`, `services/search/search_repository.py`) — zero `session.add`/`commit` calls found. Search is a pure read over already-persisted documents; nothing new is written for a search request.

### 3.2 MinIO — reconfirmed (Milestone 4 finding stands)
`get_bytes` still has zero callers anywhere in the codebase (re-verified this pass). `put_bytes`/`ensure_bucket` are the only calls, from `crawl_engine.py`'s `run_crawl`, storing raw fetched bytes keyed by `source_id/crawl_id/page_id`. No endpoint reads it. It does not duplicate response data — the actual document `content` field (what every retrieval endpoint returns) is computed by the extraction pipeline and stored in Postgres, independent of whatever raw bytes MinIO holds. **Classification: G Archival.** Already configurable (`ARCHIVAL_ENABLED`, Milestone 4) — no further change proposed.

### 3.3 Crawl storage (Crawlee `RequestQueue`, local `storage/` dir)
Holds the URL frontier (which URLs are queued/visited) for the *duration of one crawl job*. Never contains the API response body (that's `document.content` in Postgres). Not read by any endpoint — purely Crawlee's own internal bookkeeping. **Classification: G Temporary/operational** (scoped to a crawl's lifetime, not a global cache, not response data).

## 4. OSINT storage analysis

| Storage | Written during request? | Read later? | Returned directly in same response? | Required? | Classification |
|---|---|---|---|---|---|
| `Investigation` row (created by every one of: phone/email/domain/username/person sync lookups, and `POST /investigations`) | Yes | Yes — `GET /investigations/{id}`, `/status`, `/report`, `/graph`, `/timeline` all read the **same** row | The sync-lookup response **is** built from data the same pipeline just computed, but the row is *also* kept so the client's own `investigation_id` (present in every lookup response) is independently queryable afterward | Yes — proven: the response contract itself hands back an ID meant for later lookup | **B/D required** |
| `SearchJob` (per-adapter job row) | Yes | Yes — `GET /investigations/{id}` detail view | No (the sync response is a merged/reshaped view; the docstring in `lookups.py` explicitly says the reshaping "never changes the stored Evidence rows -- /investigations and /report keep full per-source provenance") | Yes — full per-source provenance is a documented, deliberate feature distinct from the merged response | **B required** |
| `Entity` / `Evidence` / `Pivot` | Yes (investigation pipeline) | Yes — `/report`, `/graph` | Overlaps with response content but is the actual backing store those two GET endpoints exist to serve | Yes | **B/C required** |
| `AuditLog` | Yes (every request, via middleware) | Never (no endpoint exposes it) | No | Not client-facing at all — diagnostic | **H diagnostic**, already excluded from removal consideration in Milestone 4 |
| `SourceHealth` | Yes (adapter health probes) | Yes — `GET /sources/health` | No | Yes | **A operational** |

**Could OSINT return results without persisting?** Traced precisely: no. Every sync lookup's own response explicitly contains an `investigation_id` that is part of the documented API contract for later retrieval (confirmed live in Milestone 6: `POST /osint/api/v1/phone/lookup` → response includes `investigation_id`). Removing the write would break that contract, not just an internal optimization — **KEEP, not a candidate.**

## 5. Reddit storage analysis

Re-confirmed: zero disk/DB writes anywhere in `reddit_app` (full-repo grep for write patterns again returns only pagination-cursor `json.dumps`, which goes straight into the HTTP response body, never to disk). The only "storage" is `FeedCache` (in-process memory, short TTL, holds raw RSS bytes) and two rate-limiter token buckets (in-process memory). All three are lost on restart by design — **not** persistent, **not** response storage, purely upstream-protection/performance. **Classification: D cache / A operational control-flow.** Nothing to remove; nothing here is even disk-backed to begin with.

## 6. Telegram storage analysis

| Category | Item | Verdict |
|---|---|---|
| Required state | Telethon session, `TelegramSource`, `TelegramAccessRequest`, `CollectionCheckpoint`, `TelegramMessage`, `TelegramMedia`, `Notification`, `BackfillJob`, `SchedulerJobRun` | All read by a **separate, later** endpoint or scheduled job (`GET /sources/{id}/messages` is explicitly DB-only, never calls Telegram again) — **KEEP, required** |
| Cache | Avatar cache (`GET /channels/{id}/photo`), message-media cache (provider API) | Genuine read-through: `if cache_path.exists(): return cache_path.read_bytes()` — the cached bytes **are** the response on a repeat call, by design, to avoid a second Telegram RPC — **KEEP, cache** |
| Archival | Raw evidence JSON (`_write_raw_evidence`) | Re-confirmed: `raw_data_ref` column exists but is never read by any route/response schema — **Archival, already configurable** (`RAW_EVIDENCE_ENABLED`, Milestone 4) |
| Request result, not separately persisted | `GET /telegram/search/global`, `/search/sources`, `/channels/{id}/search`, `/discovery/extract-links`, `/channels/search` (all live Telegram queries) | Traced: none of these write to any table. The **only** place a search result is ever persisted is `POST /telegram/search/results/save` — an explicit, separate, opt-in client action, not automatic. This is exactly the target architecture already (`request → result → response`, no auto-persistence) |

**No candidate found in Telegram either.** The one endpoint that *does* persist a "just-generated result" (`POST /telegram/search/results/save`) does so because that is the endpoint's entire declared purpose (the client is explicitly asking "save this"), not an accidental side effect — this is required behavior by definition, not a removal candidate.

## 7. Response-data correlation

| Service | Endpoint | Stored data | Response data | Same data? | Why stored |
|---|---|---|---|---|---|
| Bluweb | `POST /preflight` | Full `PreflightReportRow` | Same row, serialized | Same | Needed by `GET /preflight/{id}` and `POST /sources`'s validity check |
| Bluweb | `POST /crawls` | `CrawlJob` row (queued) | Job metadata only (not the eventual documents) | Subset | Async job pattern — the actual crawl result is a *different* later resource (`GET /documents`) |
| Bluweb | `GET /documents/{id}` | — (pure read) | Existing row | Same | N/A — read-only endpoint |
| OSINT | `POST /phone/lookup` | `Investigation`+`SearchJob`+`Entity`+`Evidence` rows | Merged/reshaped view of the same data | Superset (stored has full per-source provenance; response is deduplicated/merged) | `GET /investigations/{id}` needs the full provenance later |
| Reddit | any | nothing | live-fetched data | N/A | No storage exists |
| Telegram | `GET /search/global` | nothing | live-fetched data | N/A | No storage exists for this path |
| Telegram | `POST /search/results/save` | `TelegramMessage`-equivalent row | Confirmation of save | Same | Client explicitly requested persistence |

## 8. Unnecessary-storage candidates

**None found.** Every write traced in Phases 3-7 satisfies at least one of: (a) serves a documented, separately-callable GET/status endpoint, (b) is required for a cross-endpoint dependency (preflight→sources, investigation_id→GET /investigations), (c) is a genuine performance/upstream-protection cache, or (d) is the already-known, already-configurable archival case. No write exists solely to shuttle a value from "just computed" back to "this same response" with no other consumer — the two places that pattern might have plausibly existed (Bluweb preflight, OSINT sync lookups) both turned out to have a real, separate, later reader by design.

## 9. Storage classification summary

- **KEEP — REQUIRED**: Bluweb's entire Postgres schema (crawl/document/version/source/preflight/entity/story), OSINT's Investigation/SearchJob/Entity/Evidence/Pivot/SourceHealth, Telegram's session + all "required state" tables.
- **KEEP — CACHE**: Reddit's `FeedCache`/rate-limiter buckets, Telegram's avatar/media-file caches.
- **KEEP — ARCHIVAL (already configurable)**: Bluweb MinIO (`ARCHIVAL_ENABLED`), Telegram raw evidence (`RAW_EVIDENCE_ENABLED`).
- **KEEP — DIAGNOSTIC**: OSINT `AuditLog`, Telegram `AuditLog`, Bluweb's Crawlee frontier (all internal, none client-facing, none proposed for change here — out of this investigation's scope, which is specifically response-result storage).
- **REMOVE / CHANGE candidates found**: **zero.**

## 10-14. Target architecture, implementation plan, test plan

**Not applicable — no changes are proposed.** The investigation, traced at the file/function/table level requested, did not surface any endpoint matching the strict 8-point "response-only candidate" test in Phase 9 of the brief. Per that same phase's own instruction ("do not guess" / only classify as removable if all 8 conditions hold), nothing qualifies. No implementation plan, test plan, or staged rollout is warranted this cycle, since there is nothing identified to implement.

## 15. Production safety

Not applicable for the same reason — no change is proposed, so no staged rollout is needed. The current production deployment (validated in the prior loop) already matches the target architecture described in this task's own "desired behavior" diagram, with the sole exceptions being the two already-known, already-configurable archival writes, which are explicitly *not* in scope for removal (Milestone 4 decided, and this task reconfirmed, that they should remain configurable rather than deleted).

## 16. Risks

None introduced — no code was touched. The only risk this document surfaces is informational: if a future change ever *did* try to remove OSINT's sync-lookup investigation persistence or Bluweb's preflight persistence (mistaking them for "just a response"), it would silently break the documented `investigation_id`/`preflight_id` follow-up contract and `POST /sources`'s preflight-validity check respectively — flagged here explicitly so it is never attempted by mistake.

## 17. Rollback strategy

Not applicable — nothing was changed.

---

# FINAL REPORT

## 1. Direct answer

**NO** — no service currently persists the actual API request result unnecessarily. Every persistence mechanism found across all four services and traced end-to-end serves a proven, separate purpose: a later-callable GET/status endpoint, a cross-endpoint dependency, a genuine cache, or one of the two already-known-and-already-configurable archival writes.

## 2. Service summary

### Bluweb
- Request-result persistence: none found beyond what's required
- Required state: crawl jobs/pages, documents+versions, sources, preflight reports, entities/stories — all serve separate GET endpoints or cross-endpoint dependencies (preflight → sources)
- Historical state: documents/versions/changes/diff — the explicit product feature
- Cache: none (no in-memory or disk cache layer in Bluweb)
- Archival: MinIO raw bytes (already configurable, `ARCHIVAL_ENABLED`)
- Unnecessary storage candidates: **none**

### OSINT
- Request-result persistence: sync lookups (phone/email/domain/username/person) do persist Investigation/SearchJob/Entity/Evidence — traced and proven required (the returned `investigation_id` is a documented follow-up-lookup contract)
- Required state: same tables, serving `GET /investigations/{id}`, `/report`, `/graph`, `/timeline`
- Investigation state: full per-source provenance kept even though the sync response merges/dedupes it for display
- Cache: none (job queue uses Postgres `SKIP LOCKED`, not a cache)
- Unnecessary storage candidates: **none**

### Reddit
- Request-result persistence: **none exists at all** — fully stateless beyond in-memory cache
- Persistent storage: none
- Cache: `FeedCache` (RSS bytes, short TTL) + rate-limiter token buckets, both in-process memory only
- Unnecessary storage candidates: **none** (nothing to remove — there was never any disk/DB storage to begin with)

### Telegram
- Request-result persistence: only when the client explicitly asks (`POST /search/results/save`) — everything else (status, live search, discovery) returns data with zero automatic persistence
- Required state: Telethon session, message/source/notification/backfill/scheduler tables — all serve separate DB-only GET endpoints or background jobs
- Cache: avatar + message-media caches, genuine read-through
- Archival: raw evidence JSON (already configurable, `RAW_EVIDENCE_ENABLED`)
- Unnecessary storage candidates: **none**

## 3. Exact candidates

**None.** (Empty by design — the investigation found no write matching all 8 required conditions for the "response-only candidate" classification.)

## 4. Things that MUST NOT be removed

- Bluweb Postgres: crawl_job/crawl_page (async job contract), document+versions (the historical-retrieval feature itself), source (monitoring config), preflight (cross-referenced by `POST /sources`), entity/story (backing store for `/entities`, `/stories`)
- OSINT: Investigation/SearchJob/Entity/Evidence/Pivot (backing store for every investigation-detail endpoint; `investigation_id` is a load-bearing part of every lookup response's contract), SourceHealth
- Reddit: `FeedCache` and both rate-limiter token buckets (proven live in Milestone 3/6 to be the mechanism behind the 1.24s→0.006s cache-hit speedup and the outbound-pacing that keeps this service from getting IP-banned by Reddit)
- Telegram: session file, SQLite (all tables), avatar cache, message-media cache — all proven required or genuine cache by this investigation

## 5. Implementation recommendation

**No implementation is warranted.** The architecture already matches the "generate result → return to client, persist only where justified" target described in this task's own objective. Recommend closing this investigation with no follow-up milestone for result-storage removal. If a future audit is desired, the more productive angle (already flagged as a *deferred*, not *unnecessary-storage*, item in Milestones 4/5) is retention/TTL policy for the two archival writes (Bluweb MinIO objects, Telegram raw evidence JSON) — neither currently has a cleanup policy, but that is a retention question, not a "shouldn't be stored at all" question, and was explicitly out of scope for removal per this task's own rules.

**AUDIT COMPLETE — NO RESPONSE-ONLY STORAGE FOUND, NO CHANGE PROPOSED.**
