"""Historical collection (backfill) for one already-accessible source.

Backfill walks *backward* into a source's history, unlike the forward-
incremental monitoring pipeline, and runs as a background task - so the
start call returns 202 with a job id rather than blocking, and the job
endpoints below are how its progress is read.

These are the routes app/services/backfill_service.py was written against;
its own module docstring names GET /api/sources/{id}/backfill/{job_id}.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from telegram_app.api.deps import get_backfill_service
from telegram_app.schemas.backfill import BackfillJobResponse, BackfillRequest
from telegram_app.services.backfill_service import (
    BackfillJobNotFoundError,
    BackfillNotAllowedError,
    BackfillService,
)

router = APIRouter(prefix="/api/sources", tags=["backfill"])


@router.post(
    "/{source_id}/backfill",
    response_model=BackfillJobResponse,
    status_code=202,
    summary="Start a historical backfill for one source",
    description=(
        "Queues a background job that collects this source's *older* messages, "
        "walking backward from the oldest message already stored. Returns "
        "immediately with a job id - poll GET /api/sources/{source_id}/backfill/"
        "{job_id} for progress. Requires a source whose access has been "
        "positively verified (PUBLIC_ACCESSIBLE, ACCESSIBLE, JOINED or "
        "MONITORING); anything else is refused rather than silently queued."
    ),
)
async def start_backfill(
    source_id: int,
    payload: BackfillRequest | None = None,
    service: BackfillService = Depends(get_backfill_service),
):
    request = payload or BackfillRequest()
    try:
        return await service.start_backfill(
            source_id,
            from_date=request.from_date,
            to_date=request.to_date,
            limit=request.limit,
        )
    except BackfillJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BackfillNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/{source_id}/backfill",
    response_model=list[BackfillJobResponse],
    summary="List backfill jobs for one source",
)
async def list_backfill_jobs(source_id: int, service: BackfillService = Depends(get_backfill_service)):
    return await service.list_backfill_jobs(source_id)


@router.get(
    "/{source_id}/backfill/{job_id}",
    response_model=BackfillJobResponse,
    summary="Poll one backfill job's progress",
)
async def get_backfill_job(source_id: int, job_id: int, service: BackfillService = Depends(get_backfill_service)):
    try:
        return await service.get_backfill_job(source_id, job_id)
    except BackfillJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
