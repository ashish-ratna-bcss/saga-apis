"""Monitoring start/stop and status."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from telegram_app.api.deps import get_monitoring_service
from telegram_app.schemas.monitoring import MonitoringStatusOut
from telegram_app.services.monitoring_service import MonitoringService, MonitoringStateError

router = APIRouter(prefix="/api/sources", tags=["monitoring"])


@router.post("/{source_id}/monitoring/start", response_model=MonitoringStatusOut)
async def start_monitoring(source_id: int, service: MonitoringService = Depends(get_monitoring_service)):
    try:
        source = await service.start_monitoring(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MonitoringStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_status(source)


@router.post("/{source_id}/monitoring/stop", response_model=MonitoringStatusOut)
async def stop_monitoring(source_id: int, service: MonitoringService = Depends(get_monitoring_service)):
    try:
        source = await service.stop_monitoring(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_status(source)


@router.get("/{source_id}/monitoring-status", response_model=MonitoringStatusOut)
async def monitoring_status(source_id: int, service: MonitoringService = Depends(get_monitoring_service)):
    try:
        source = await service.get_monitoring_status(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_status(source)


def _to_status(source) -> MonitoringStatusOut:
    return MonitoringStatusOut(
        source_id=source.id,
        monitoring_enabled=source.monitoring_enabled,
        access_status=source.access_status,
        monitoring_started_at=source.monitoring_started_at,
        last_collected_at=source.last_collected_at,
        last_message_id=source.last_message_id,
    )
