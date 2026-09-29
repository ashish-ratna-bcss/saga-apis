"""In-memory crawl job registry for a single self-hosted instance.

Results live only until TTL expiry or process restart. Callers must copy
pages into their own storage — Bluweb does not own a content database.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CrawlJob:
    crawl_id: str
    seed_url: str
    status: str = "queued"  # queued | running | completed | failed | cancelled
    max_pages: int = 25
    max_depth: int = 2
    same_domain_only: bool = True
    use_browser: bool | None = None
    discover_seeds: bool = True
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False


_jobs: dict[str, CrawlJob] = {}
_lock = asyncio.Lock()
_TTL_SECONDS = 3600  # drop finished jobs after 1h to bound memory


async def create_job(**kwargs: Any) -> CrawlJob:
    await _purge_expired()
    job = CrawlJob(crawl_id=str(uuid.uuid4()), **kwargs)
    async with _lock:
        _jobs[job.crawl_id] = job
    return job


async def get_job(crawl_id: str) -> CrawlJob | None:
    async with _lock:
        return _jobs.get(crawl_id)


async def _purge_expired() -> None:
    now = time.time()
    async with _lock:
        dead = [
            jid
            for jid, job in _jobs.items()
            if job.completed_at and (now - job.completed_at) > _TTL_SECONDS
        ]
        for jid in dead:
            del _jobs[jid]
