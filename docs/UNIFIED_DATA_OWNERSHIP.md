# Unified API — Data Ownership & Persistence Audit (Milestone 4)

Complete write inventory across all four services, classified, with the two Option-B decisions made and everything else left untouched.

## Classification key

A. Required operational state · B. Required historical/application state · C. Required session state · D. Required cache · E. Required temporary data · F. Generic API response data · G. Archival data · H. Debug/diagnostic data

## Bluweb

| Write | Module | Storage | Data | Reader | Classification |
|---|---|---|---|---|---|
| `CrawlRepository` (job/run/page rows) | `db/repositories/crawl_repository.py` | Postgres (`webintel_unified`) | Crawl job status, per-page fetch outcomes, domain learning stats | `GET /crawls`, `/crawls/{id}`, `/crawls/{id}/pages`, `/domains/{d}/profile` | **A** operational + **B** — job status IS the API response |
| `DocumentRepository` (document + versions) | `db/repositories/document_repository.py` | Postgres | Extracted `content`, title, metadata, per-version history | `GET /documents*`, `/versions*`, `/diff`, `/changes` | **B required historical state** — these endpoints exist specifically to serve this |
| `IntelligenceRepository` (entities, stories) | `db/repositories/intelligence_repository.py` | Postgres | Extracted entities, story clustering | `GET /entities*`, `/stories*` | **B** |
| `PreflightRepository` | `db/repositories/preflight_repository.py` | Postgres | Preflight assessment report | `GET /preflight/{id}`, gates `POST /sources` | **A/B** — sources require a valid unexpired preflight |
| `SourceRepository` | `db/repositories/source_repository.py` | Postgres | Monitored source config, scheduling state | `GET/PATCH /sources*` | **A** operational |
| Crawlee `RequestQueue` | `services/crawling/crawl_engine.py` | Local `storage/` dir | URL frontier for the in-flight crawl | Nothing outside the crawl itself | **A** required operational (crawl-scoped, not exposed via any API) |
| `ArtifactStore.put_bytes` | `services/storage/artifact_store.py` | MinIO | Raw fetched bytes (pre-extraction) | **Nobody** — `get_bytes` has zero callers anywhere in the codebase, confirmed by full-repo grep | **G archival, unnecessary for API functionality** — see decision below |

**Decision (Phase 12, Option B applied)**: MinIO archival is not required by any endpoint. Added `ARCHIVAL_ENABLED` (default `true`, preserves current behavior exactly) and a `NoopArtifactStore` implementation alongside the existing `ArtifactStore`/MinIO one — same interface, zero MinIO calls when disabled, still computes and stores the sha256/size/content-type metadata columns (nullable `storage_key` becomes `None`). MinIO code, config, and default behavior are all untouched; this only adds an off switch. **Live-verified**: a real crawl against `https://example.org` with `ARCHIVAL_ENABLED=false` completed successfully end to end — document created, content retrievable via `/documents/{id}/versions/1` — with zero MinIO calls (this also incidentally sidesteps the pre-existing MinIO-credential mismatch documented in Milestone 3, as a side effect, not the goal).

## OSINT

| Write | Module | Storage | Data | Reader | Classification |
|---|---|---|---|---|---|
| `Investigation` | `models.py` | SQLite/Postgres | Full investigation state machine, mode, status | `GET /investigations*`, `/status`, `/report`, `/graph`, `/timeline` | **B required** — this table *is* the API response backing store |
| `SearchJob` | `models.py` | same | Per-adapter job status/results/errors | Same investigation endpoints | **B required** |
| `Entity`, `EntityRelationship`, `Evidence`, `Pivot` | `models.py` | same | Discovered entities, relationships, evidence, pivot chain | Investigation detail/graph/report | **B required** |
| `AuditLog` | `models.py` | same | Request method/path/status/duration | Not exposed via any endpoint | **H** debug/diagnostic |
| `SourceHealth` | `models.py` | same | Adapter reachability | `GET /sources/health` | **A** operational |

**No unnecessary persistence found.** Every OSINT table is either the literal backing store for a GET endpoint that returns exactly that data (Category B, required), or operational/diagnostic state (audit log, source health — Categories A/H) explicitly excluded from removal by this milestone's own instructions. **No code change made.** Confirmed no filesystem writes anywhere in OSINT (full-repo grep for file-write patterns returned only `json.dumps` calls used for log formatting and idempotency-key fingerprint hashing, neither of which touches disk).

## reddit_server

| Write | Module | Storage | Data | Reader | Classification |
|---|---|---|---|---|---|
| `FeedCache` | `reddit/feed_cache.py` | In-process memory only | Raw RSS feed bytes, short TTL | Same-process subsequent requests to the same feed | **D required cache** — directly determines response latency, proven live in Milestone 3 (1.24s cold, 0.006s cached) |
| `TokenBucket` | `reddit/rate_limiter.py` | In-process memory only | Outbound request pacing state | N/A (control-flow only) | **A required operational** |
| `ClientRateLimiter` | `core/rate_limit.py` | In-process memory only | Per-caller inbound request counts | N/A | **A required operational** |

**No persistent (disk/DB) writes exist at all** — confirmed by full-repo grep (zero `session.add`/`commit`/`write_text`/`write_bytes`/`open(...,'w')` hits; the only `json.dumps` calls are pagination-cursor encoding, which goes straight into the API response, never to disk). Fully matches "Reddit remains primarily stateless" — nothing to change, nothing to gate.

## telegram_poc

| Write | Module | Storage | Data | Reader | Classification |
|---|---|---|---|---|---|
| MTProto session | `telegram/client.py` (Telethon-managed) | `data/telegram_service.session` | Live authenticated session | Every Telegram RPC; must survive restart | **C required session state** — never touched |
| `TelegramSource`, `TelegramAccessRequest`, `CollectionCheckpoint` | `database/models.py` | SQLite | Monitored source state, access workflow, forward-collection checkpoint | `GET/PATCH /sources*`, monitoring jobs | **A/B required** |
| `TelegramMessage`, `TelegramMedia` | `database/models.py` | SQLite | Collected message/media metadata | `GET /sources/{id}/messages`, `/messages/{id}`, `/messages` | **B required** — these endpoints are explicitly DB-only, never call Telegram |
| `TelegramEntity` | `database/models.py` | SQLite | Resolved-entity cache | Internal lookups | **D required cache** |
| `Notification`, `BackfillJob`, `SchedulerJobRun` | `database/models.py` | SQLite | Notification queue, backfill progress, job run history | `GET /notifications`, `/sources/{id}/backfill*` | **A/B required** |
| `AuditLog` | `database/models.py` | SQLite | Request audit trail | Not exposed via any endpoint | **H** debug/diagnostic |
| Avatar cache | `services/search_service.py` | `data/avatars/{id}.jpg` | Full-res profile photo bytes | `GET /channels/{id}/photo` — genuine read-through cache (`if cache_path.exists(): return cache_path.read_bytes()`) | **D required cache** — the endpoint's response *is* this file's bytes |
| Message media cache | `services/provider_service.py` | `data/message_media/` | On-demand media bytes | Provider API's media-by-message-id endpoint | **D required cache** — same pattern |
| **Raw evidence JSON** | `services/message_service.py::_write_raw_evidence` | `data/raw_messages/{source_id}/{message_id}.json` | Full raw Telegram message payload, one file per message | **Nobody** — the returned `raw_ref` path is stored in `TelegramMessage.raw_data_ref` (nullable), but that column is never read by any route or exposed in any response schema | **G archival, unnecessary for API functionality** — see decision below |

**Decision (same Option B pattern as Bluweb's MinIO)**: added `RAW_EVIDENCE_ENABLED` (default `true`, preserves current behavior exactly). When `false`, `_write_raw_evidence` is skipped and `raw_data_ref` is stored as `None` (the column was already nullable — no schema change). Message collection, storage, and every message/source endpoint's behavior is completely unaffected either way, since nothing ever read the file or the ref.

**Media download** (`media_download_enabled`, default `false`) was already an existing, pre-Milestone-4 config gate on actual media *downloading* — left completely untouched; not part of this audit's changes.

## MinIO final status

**Option B**: archival-only, not required by API functionality, kept installed and default-on, now toggleable via `ARCHIVAL_ENABLED=false` without removing any code, config, or the MinIO dependency itself.

## What was changed (complete list)

1. `Bluweb/bluweb_app/core/config.py` — added `archival_enabled: bool = True`.
2. `Bluweb/bluweb_app/services/storage/artifact_store.py` — added `NoopArtifactStore` + `build_artifact_store()` factory; `StoredArtifact.storage_key` type widened to `str | None`. `ArtifactStore`/`MinioArtifactStore` behavior itself: unchanged.
3. `Bluweb/bluweb_app/services/crawling/crawl_engine.py` — one line: `ArtifactStore(settings)` → `build_artifact_store(settings)` (plus keeping the `ArtifactStore` import for its still-used `.build_key()` static method).
4. `telegram_poc/telegram_app/config.py` — added `raw_evidence_enabled: bool = Field(default=True)`.
5. `telegram_poc/telegram_app/services/message_service.py` — one call site gated: `_write_raw_evidence(...)` only runs `if self.settings.raw_evidence_enabled`.

Nothing else. No schema changed, no endpoint changed, no response model changed, no test changed.

## What was deliberately NOT changed

- OSINT: no persistence removed (all of it is required investigation/job state or explicitly-excluded diagnostic state).
- reddit_server: no change (nothing to change — fully stateless already, existing cache/limiter untouched).
- Telegram session, SQLite, avatar cache, message-media cache: untouched — all required (session/state) or genuine read-through caches (avatar/media).
- Bluweb Postgres (documents, crawls, sources, preflight, entities/stories): untouched — this is the required historical state the document/version/diff/changes endpoints exist to serve.
- No database consolidation, no tenancy, no schema changes anywhere.
- MinIO and the raw-evidence writer are still ON by default — nothing about existing deployments changes unless an operator explicitly sets the new flag to `false`.

## Test results (Phase 14)

| Service | Before | After |
|---|---|---|
| Bluweb | 331 passed / 2 pre-existing | 331 passed / 2 pre-existing (identical) |
| OSINT | 183 passed | 183 passed (identical, no code touched) |
| reddit_server | 223 passed / 1 skipped | 223 passed / 1 skipped (identical, no code touched) |
| telegram_poc | 226 passed / 3 pre-existing | 226 passed / 3 pre-existing (identical) |

## Live data tests (Phase 15)

- Bluweb: real crawl with `ARCHIVAL_ENABLED=false` completed end-to-end (previously this exact crawl got stuck forever on a pre-existing MinIO credential mismatch — see Milestone 3); document created and fully retrievable with real content.
- Default-config (archival/raw-evidence both on) representative checks re-run through the full Docker+nginx stack: Bluweb document list, OSINT investigation creation, Reddit RSS — all unchanged from Milestone 3.

## Restart test (Phase 16)

Container restarted; exactly 4 scheduler jobs added (no duplicates), all 4 namespaces healthy immediately after.

## Filesystem audit (Phase 17)

Before/after snapshot of `Bluweb/storage/`, `OSINT/osint.db`, `telegram_poc/data/*` across the full live-test + restart cycle: only expected mtime bumps on files touched by real requests (OSINT investigation write, Telegram session reconnect) — **zero new files or directories anywhere**, including no recurrence of the Milestone-3-fixed stray `unified_api/data`/`unified_api/osint.db` problem.

## Remaining architectural decisions (none required, none made)

None. Every item in this milestone's own final acceptance checklist is satisfied by either "no change" (OSINT, reddit_server, Telegram session/DB/caches, Bluweb Postgres) or the two symmetric, minimal, default-preserving config gates described above.
