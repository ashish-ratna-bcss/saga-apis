"""Job orchestration: two interchangeable ways to get an investigation from
`queued` to actually running (see app/db.py's is_sqlite/is_postgres):

- SQLite / single-process dev: an in-process asyncio.Queue + a small worker
  pool, both living in this module (unchanged from Phase 1).
- Postgres / multi-process production: app/job_queue.py's polling workers,
  which call `process_investigation` (defined here) directly -- no push
  queue needed, since `SELECT ... FOR UPDATE SKIP LOCKED` against the
  `investigations` table already gives every worker process a safe way to
  claim work. `enqueue_investigation` becomes a no-op in that mode: the row
  is already sitting there with status=queued for a poller to find.

Investigating one identifier means: run every adapter for its type + public
web, then hand the evidence to the pivot engine, then recurse into whatever
it approves (see app/pivot_engine.py) -- bounded by max_pivot_depth, a
wall-clock budget, and cooperative cancellation, so this can never become an
uncontrolled crawler and can always be stopped cleanly mid-flight.
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from osint_app import document_mining, metrics, pivot_engine
from osint_app.adapters.base import AdapterResult, SourceAdapter, SourceUnavailable
from osint_app.adapters.phone_adapter import generate_search_queries
from osint_app.adapters.registry import DOCUMENT_ADAPTER, PUBLIC_WEB_ADAPTER, adapters_for
from osint_app.config import settings
from osint_app.db import SessionLocal, is_postgres
from osint_app.entity_resolution import get_or_create_root_entity, ingest_adapter_result
from osint_app.enums import IdentifierType, JobStatus, PivotStatus, can_transition
from osint_app.models import Entity, Evidence, Investigation, SearchJob, SourceHealth

logger = logging.getLogger("orchestrator")

_queue: "asyncio.Queue[str] | None" = None
_worker_tasks: list[asyncio.Task] = []
_loop: asyncio.AbstractEventLoop | None = None


@dataclass(frozen=True)
class ModeConfig:
    """See INVESTIGATION MODES. quick = normalization + one primary adapter,
    no public web, no pivots, no document mining. standard = every per-type
    adapter + public web, one level of pivoting, no document mining (that's
    reserved for deep -- fetching PDFs/pages is the most expensive step).
    deep = everything, pivoting to the configured max_pivot_depth, plus
    document mining up to max_documents_per_investigation."""

    run_public_web: bool
    adapter_limit: int | None
    max_pivot_depth: int
    mine_documents: bool


def _mode_config(mode: str) -> ModeConfig:
    if mode == "quick":
        return ModeConfig(run_public_web=False, adapter_limit=1, max_pivot_depth=-1, mine_documents=False)
    if mode == "deep":
        return ModeConfig(
            run_public_web=True, adapter_limit=None, max_pivot_depth=settings.max_pivot_depth, mine_documents=True
        )
    return ModeConfig(
        run_public_web=True, adapter_limit=None, max_pivot_depth=min(1, settings.max_pivot_depth),
        mine_documents=False,
    )


def enqueue_investigation(investigation_id: str) -> None:
    """On Postgres this is a no-op: the row was already created with
    status=queued (by the route handler, in the same transaction), and any
    of the polling job_queue.py workers -- in this process or another --
    will pick it up on their next poll. See module docstring.

    On SQLite: FastAPI's sync `def` routes run in a threadpool, off the
    event-loop thread that owns `_queue` -- asyncio.Queue isn't thread-safe,
    so a plain put_nowait() here can silently fail to wake a waiting worker.
    Route through call_soon_threadsafe whenever we know the loop (i.e.
    workers have started); fall back to a direct put for callers already on
    the loop thread with no workers running yet (e.g. tests calling
    process_investigation directly, which never calls this at all)."""
    if is_postgres:
        return
    if _queue is None:
        raise RuntimeError("orchestrator workers have not been started (call start_workers first)")
    if _loop is not None:
        _loop.call_soon_threadsafe(_queue.put_nowait, investigation_id)
    else:
        _queue.put_nowait(investigation_id)


async def start_workers(concurrency: int) -> None:
    global _loop, _queue
    if _worker_tasks:
        return
    _loop = asyncio.get_running_loop()
    # Created here, not at module import time: asyncio.Queue's internal
    # waiter futures bind to whichever loop first touches it, and a
    # module-level singleton would be stuck on whatever loop happened to be
    # running at import time (an issue across independent event-loop
    # lifetimes -- e.g. each TestClient(app) run -- though a real deployment
    # only ever has one loop for the process's life).
    _queue = asyncio.Queue()
    for _ in range(concurrency):
        _worker_tasks.append(asyncio.create_task(_worker_loop()))


async def stop_workers() -> None:
    global _loop, _queue
    for task in _worker_tasks:
        task.cancel()
    _worker_tasks.clear()
    _loop = None
    _queue = None


async def _worker_loop() -> None:
    while True:
        investigation_id = await _queue.get()
        try:
            await process_investigation(investigation_id)
        except Exception:
            pass  # a single investigation's bug must never kill the worker loop
        finally:
            _queue.task_done()


def _is_cancelled(db: Session, investigation_id: str) -> bool:
    """A fresh, minimal column read -- never trusts the long-lived `investigation`
    ORM object the recursive investigation loop holds, since a cancel request
    is written via a *different* session (the API request's) and SQLAlchemy's
    identity map won't pick that up without an explicit re-read."""
    return bool(db.query(Investigation.cancel_requested).filter_by(id=investigation_id).scalar())


_source_semaphores: dict[str, asyncio.Semaphore] = {}


def _semaphore_for(source_name: str) -> asyncio.Semaphore:
    """Per-process, per-source concurrency cap (STEP 10) -- built lazily so
    it's always bound to the running event loop."""
    sem = _source_semaphores.get(source_name)
    if sem is None:
        sem = asyncio.Semaphore(settings.source_max_concurrency)
        _source_semaphores[source_name] = sem
    return sem


def _set_status(db: Session, investigation: Investigation, target: JobStatus) -> bool:
    """Re-reads the current status straight from the DB (same reasoning as
    _is_cancelled) and only writes if the transition is valid, so a worker
    whose lease was reclaimed mid-job (see job_queue.reclaim_expired_leases)
    can never clobber a terminal status another worker already wrote for the
    same investigation after picking it back up. Returns whether it wrote."""
    db.refresh(investigation)
    if not can_transition(investigation.status, target):
        logger.warning(
            "refusing invalid investigation status transition %s -> %s for %s",
            investigation.status, target, investigation.id,
        )
        return False
    investigation.status = target
    return True


def _is_timeout_error(exc: BaseException) -> bool:
    cause = exc.__cause__
    if isinstance(cause, TimeoutError | asyncio.TimeoutError):
        return True
    return "timeout" in str(exc).lower()


def _update_source_health(
    db: Session, source_name: str, *, installed: bool, success: bool, duration: float, timed_out: bool = False
) -> None:
    health = db.get(SourceHealth, source_name)
    if health is None:
        # SQLAlchemy column `default=` only applies at flush, so these must be
        # set explicitly -- otherwise the `+=` below hits None, not 0.
        health = SourceHealth(
            source_name=source_name, run_count=0, success_count=0, failure_count=0, timeout_count=0,
            total_duration_seconds=0.0,
        )
        db.add(health)
    health.installed = installed
    health.run_count += 1
    health.total_duration_seconds += duration
    now = datetime.now(UTC)
    if success:
        health.success_count += 1
        health.last_success = now
        health.last_error = None
    else:
        health.failure_count += 1
        health.last_failure = now
    if timed_out:
        health.timeout_count += 1
    db.flush()


async def _run_adapter_job(
    db: Session,
    investigation: Investigation,
    adapter: SourceAdapter,
    run_coro_factory: Callable[[], Awaitable[list[AdapterResult]]],
    anchor_entity: Entity,
) -> tuple[list[Evidence], str]:
    """Returns (newly created Evidence rows, job id) -- the evidence feeds
    the pivot engine; empty when the job didn't complete successfully.

    Transient failures (SourceUnavailable(retryable=True), the default) get
    a bounded, backed-off retry -- see RATE_LIMITING/RETRY POLICY. Permanent
    ones (CAPTCHA-blocked adapters, retryable=False) and unexpected bugs
    (bare Exception -> FAILED) are never retried."""
    job = SearchJob(investigation_id=investigation.id, source_name=adapter.name, status=JobStatus.RUNNING)
    job.started_at = datetime.now(UTC)
    db.add(job)
    # commit (not just flush): releases SQLite's write lock before the
    # `await`s below, some of which run for minutes (sherlock, maigret) --
    # holding an uncommitted write open that whole time would starve any
    # other request needing to write concurrently (e.g. POST .../cancel),
    # even with WAL mode + busy_timeout (see db.py). Real bug, caught live.
    db.commit()

    health = db.get(SourceHealth, adapter.name)
    if health is not None and not health.enabled:
        job.status = JobStatus.UNAVAILABLE
        job.error = f"{adapter.name} is disabled by operator"
        job.finished_at = datetime.now(UTC)
        db.commit()
        return [], job.id

    installed = await adapter.is_available()
    if not installed:
        job.status = JobStatus.UNAVAILABLE
        job.error = f"{adapter.name} is not available (dependency missing or unreachable)"
        job.finished_at = datetime.now(UTC)
        _update_source_health(db, adapter.name, installed=False, success=False, duration=0.0)
        db.commit()
        return [], job.id

    start = time.monotonic()
    evidence_rows: list[Evidence] = []
    attempt = 0
    while True:
        try:
            async with _semaphore_for(adapter.name):
                results = await run_coro_factory()
        except SourceUnavailable as exc:
            duration = time.monotonic() - start
            timed_out = _is_timeout_error(exc)
            if exc.retryable and attempt < settings.adapter_max_retries:
                attempt += 1
                _update_source_health(db, adapter.name, installed=True, success=False, duration=duration, timed_out=timed_out)
                await asyncio.sleep(settings.adapter_retry_backoff_seconds * (2 ** (attempt - 1)))
                continue
            job.status = JobStatus.UNAVAILABLE
            job.error = str(exc)
            _update_source_health(db, adapter.name, installed=True, success=False, duration=duration, timed_out=timed_out)
        except Exception as exc:  # noqa: BLE001 -- an adapter bug must become a FAILED job, not a crashed worker
            duration = time.monotonic() - start
            job.status = JobStatus.FAILED
            job.error = str(exc)
            _update_source_health(db, adapter.name, installed=True, success=False, duration=duration)
        else:
            duration = time.monotonic() - start
            job.status = JobStatus.COMPLETED
            job.result_count = len(results)
            evidence_rows = [ingest_adapter_result(db, investigation, job.id, r, anchor_entity) for r in results]
            _update_source_health(db, adapter.name, installed=True, success=True, duration=duration)
        break

    job.finished_at = datetime.now(UTC)
    db.commit()

    metrics.increment("jobs_total")
    job_counter_name = f"jobs_{job.status}"
    if job_counter_name in metrics.COUNTER_NAMES:
        metrics.increment(job_counter_name)
    metrics.increment("source_success_total" if job.status == JobStatus.COMPLETED else "source_failure_total")

    return evidence_rows, job.id


def _finalize_investigation_status(db: Session, investigation: Investigation) -> None:
    if _is_cancelled(db, investigation.id):
        if not _set_status(db, investigation, JobStatus.CANCELLED):
            return  # another worker already finalized this investigation
        metrics.increment("investigations_total")
        metrics.increment("investigations_cancelled")
        return
    jobs = db.query(SearchJob).filter_by(investigation_id=investigation.id).all()
    if not jobs:
        target = JobStatus.COMPLETED
    else:
        statuses = {j.status for j in jobs}
        if statuses <= {JobStatus.COMPLETED}:
            target = JobStatus.COMPLETED
        elif JobStatus.COMPLETED in statuses:
            target = JobStatus.PARTIAL
        elif statuses <= {JobStatus.UNAVAILABLE}:
            target = JobStatus.UNAVAILABLE
        else:
            target = JobStatus.FAILED

    if not _set_status(db, investigation, target):
        return  # another worker already finalized this investigation

    metrics.increment("investigations_total")
    inv_counter_name = f"investigations_{investigation.status}"
    if inv_counter_name in metrics.COUNTER_NAMES:
        metrics.increment(inv_counter_name)


async def _run_public_web_job(
    db: Session, investigation: Investigation, entity: Entity, identifier_type: IdentifierType, normalized_value: str
) -> tuple[list[Evidence], str]:
    """Shared by full investigations (_investigate_identifier) and direct
    lookup endpoints (run_direct_lookup) -- one place that knows phone
    identifiers need phone_adapter's richer generated query set while every
    other identifier type just searches the literal value."""
    if identifier_type == IdentifierType.PHONE:
        queries = generate_search_queries(normalized_value)
        return await _run_adapter_job(
            db, investigation, PUBLIC_WEB_ADAPTER, lambda q=queries: PUBLIC_WEB_ADAPTER.run_queries(q), entity
        )
    return await _run_adapter_job(
        db, investigation, PUBLIC_WEB_ADAPTER, lambda: PUBLIC_WEB_ADAPTER.run(normalized_value), entity
    )


async def _investigate_identifier(
    db: Session,
    investigation: Investigation,
    entity: Entity,
    identifier_type: IdentifierType,
    normalized_value: str,
    depth: int,
    start_time: float,
    mode_cfg: ModeConfig,
) -> list[str]:
    """Runs every applicable adapter (+ public_web, mode permitting) for one
    identifier, anchored to `entity`, then -- budget permitting -- asks the
    pivot engine what's worth investigating next and recurses. Returns this
    level's own job ids (not nested pivots' -- those live on their own
    Pivot row). Checked for cooperative cancellation at entry and before
    recursing into pivots -- never mid-adapter-call, so a cancel never
    corrupts an in-flight write (see JOB CANCELLATION)."""
    job_ids: list[str] = []
    all_evidence: list[Evidence] = []

    if _is_cancelled(db, investigation.id):
        return job_ids

    type_adapters = adapters_for(identifier_type)
    if mode_cfg.adapter_limit is not None:
        type_adapters = type_adapters[: mode_cfg.adapter_limit]
    for adapter in type_adapters:
        rows, job_id = await _run_adapter_job(
            db, investigation, adapter, lambda a=adapter: a.run(normalized_value), entity
        )
        all_evidence.extend(rows)
        job_ids.append(job_id)

    if mode_cfg.run_public_web:
        rows, job_id = await _run_public_web_job(db, investigation, entity, identifier_type, normalized_value)
        all_evidence.extend(rows)
        job_ids.append(job_id)

    if time.monotonic() - start_time > settings.max_investigation_runtime_seconds:
        return job_ids

    if mode_cfg.mine_documents:
        for doc_url in document_mining.select_document_urls(db, investigation, all_evidence, DOCUMENT_ADAPTER.name):
            if time.monotonic() - start_time > settings.max_investigation_runtime_seconds:
                break
            rows, job_id = await _run_adapter_job(
                db, investigation, DOCUMENT_ADAPTER, lambda u=doc_url: DOCUMENT_ADAPTER.run(u), entity
            )
            all_evidence.extend(rows)
            job_ids.append(job_id)

    if time.monotonic() - start_time > settings.max_investigation_runtime_seconds or _is_cancelled(db, investigation.id):
        return job_ids

    targets = pivot_engine.evaluate_pivots(db, investigation, entity, all_evidence, depth + 1, mode_cfg.max_pivot_depth)
    for target in targets:
        if time.monotonic() - start_time > settings.max_investigation_runtime_seconds or _is_cancelled(db, investigation.id):
            pivot_engine.mark_pivot_status(db, target.pivot_id, PivotStatus.SKIPPED_LIMIT_REACHED)
            continue
        pivot_engine.mark_pivot_status(db, target.pivot_id, PivotStatus.RUNNING)
        pivot_job_ids = await _investigate_identifier(
            db, investigation, target.pivot_entity, target.identifier_type, target.normalized_value,
            target.depth, start_time, mode_cfg,
        )
        pivot_engine.mark_pivot_status(db, target.pivot_id, PivotStatus.COMPLETED, job_ids=pivot_job_ids)

    return job_ids


def _reap_orphaned_jobs(db: Session, investigation_id: str) -> None:
    """A job left QUEUED/RUNNING when its worker crashed (see
    job_queue.reclaim_expired_leases) is never coming back -- this run is
    about to redo the investigation's work from scratch. Close out the stale
    row explicitly instead of leaving it looking perpetually in-progress,
    which would also stop _finalize_investigation_status from ever reaching
    a clean COMPLETED even when every job in *this* run succeeds. Real bug,
    caught live: kill -9'd a worker mid-job, its SearchJob row stayed RUNNING
    forever after a second worker reclaimed and finished the investigation."""
    stale = (
        db.query(SearchJob)
        .filter(SearchJob.investigation_id == investigation_id, SearchJob.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
        .all()
    )
    for job in stale:
        job.status = JobStatus.FAILED
        job.error = "orphaned: worker crashed or was restarted before this job finished"
        job.finished_at = datetime.now(UTC)
    if stale:
        db.commit()


async def process_investigation(investigation_id: str) -> None:
    """Runs the root identifier (and, budget permitting, its automatic
    pivots) for one investigation, synchronously within this coroutine
    (called by a worker, or directly in tests)."""
    db = SessionLocal()
    try:
        investigation = db.get(Investigation, investigation_id)
        if investigation is None:
            return
        _reap_orphaned_jobs(db, investigation_id)
        _set_status(db, investigation, JobStatus.RUNNING)
        db.commit()

        root = get_or_create_root_entity(db, investigation)
        db.commit()

        start_time = time.monotonic()
        mode_cfg = _mode_config(investigation.mode)
        await _investigate_identifier(
            db, investigation, root, investigation.identifier_type, investigation.normalized_identifier,
            0, start_time, mode_cfg,
        )

        _finalize_investigation_status(db, investigation)
        db.commit()
    finally:
        db.close()


async def run_direct_lookup(investigation_id: str, *, run_public_web: bool) -> None:
    """Synchronous, single-level lookup for the direct feature endpoints
    (routes/lookups.py: /phone/lookup, /email/lookup, etc.) -- runs only the
    per-identifier-type adapters (+ public_web when the caller asks for it)
    and does NOT pivot/recurse/mine documents; that's what distinguishes a
    direct lookup from a full POST /investigations run. Reuses
    _run_adapter_job/_run_public_web_job/_finalize_investigation_status --
    the exact functions the full pipeline uses -- so job tracking, retries,
    and source-health updates behave identically, per "do not duplicate
    business logic" (see routes/lookups.py)."""
    db = SessionLocal()
    try:
        investigation = db.get(Investigation, investigation_id)
        if investigation is None:
            return
        _reap_orphaned_jobs(db, investigation_id)
        _set_status(db, investigation, JobStatus.RUNNING)
        db.commit()

        entity = get_or_create_root_entity(db, investigation)
        db.commit()

        for adapter in adapters_for(investigation.identifier_type):
            await _run_adapter_job(
                db, investigation, adapter, lambda a=adapter: a.run(investigation.normalized_identifier), entity
            )

        if run_public_web:
            await _run_public_web_job(
                db, investigation, entity, investigation.identifier_type, investigation.normalized_identifier
            )

        _finalize_investigation_status(db, investigation)
        db.commit()
    finally:
        db.close()
