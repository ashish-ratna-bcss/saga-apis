"""Source registration, listing, and access-status management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from telegram_app.api.deps import get_source_service
from telegram_app.schemas.source import AccessStatusOut, SourceCreate, SourceOut, SourceUpdate
from telegram_app.services.source_service import (
    InvalidAccessRequestStateError,
    SourceAlreadyExistsError,
    SourceNotFoundError,
    SourceService,
)

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.post("", response_model=SourceOut, status_code=201)
async def create_source(payload: SourceCreate, service: SourceService = Depends(get_source_service)):
    try:
        source = await service.register_source(payload.identifier, monitoring_enabled=payload.monitoring_enabled)
    except SourceAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return source


@router.get("", response_model=list[SourceOut])
async def list_sources(limit: int = 100, offset: int = 0, service: SourceService = Depends(get_source_service)):
    return await service.list_sources(limit=limit, offset=offset)


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: int, service: SourceService = Depends(get_source_service)):
    source = await service.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return source


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(source_id: int, payload: SourceUpdate, service: SourceService = Depends(get_source_service)):
    try:
        return await service.update_source(source_id, **payload.model_dump(exclude_unset=True))
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: int, service: SourceService = Depends(get_source_service)):
    try:
        await service.delete_source(source_id)
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{source_id}/request-access")
async def request_access(source_id: int, service: SourceService = Depends(get_source_service)):
    try:
        request = await service.request_access(source_id)
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidAccessRequestStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": request.id,
        "source_id": request.source_id,
        "status": request.status,
        "requested_at": request.requested_at,
        "next_check_at": request.next_check_at,
        "attempt_count": request.attempt_count,
    }


@router.get("/{source_id}/access-status", response_model=AccessStatusOut)
async def get_access_status(source_id: int, service: SourceService = Depends(get_source_service)):
    try:
        source = await service.get_access_status(source_id)
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AccessStatusOut(
        source_id=source.id,
        access_status=source.access_status,
        status_reason=source.status_reason,
        last_status_check_at=source.last_status_check_at,
        next_status_check_at=source.next_status_check_at,
        last_probe_status=source.last_probe_status,
        last_probe_reason=source.last_probe_reason,
        last_probe_at=source.last_probe_at,
    )


@router.post("/{source_id}/check-access", response_model=SourceOut)
async def check_access(source_id: int, service: SourceService = Depends(get_source_service)):
    try:
        return await service.check_access(source_id)
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
