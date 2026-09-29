from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, status

from bluweb_app.api.deps import get_security_service
from bluweb_app.core.config import Settings, get_settings
from bluweb_app.core.metrics import preflight_count
from bluweb_app.schemas.preflight import PreflightReportResponse, PreflightRequest
from bluweb_app.services.events import Event, default_bus
from bluweb_app.services.preflight.models import PreflightReport
from bluweb_app.services.preflight.service import PreflightService
from bluweb_app.services.security.url_security import URLSecurityService

router = APIRouter(prefix="/preflight", tags=["preflight"])


@router.post(
    "",
    response_model=PreflightReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Run a pre-flight capability assessment against a URL",
)
async def create_preflight(
    body: PreflightRequest,
    settings: Settings = Depends(get_settings),
    security: URLSecurityService = Depends(get_security_service),
) -> PreflightReportResponse:
    """Assess a URL and return the report in the response body.

    Nothing is stored. ``preflight_id`` is ephemeral (for correlation only).
    """
    service = PreflightService(settings, security)
    report = await service.run(body.url)
    preflight_count.labels(status=report.status).inc()
    await default_bus.publish(Event("PREFLIGHT_COMPLETED", {
        "url": report.url,
        "status": report.status,
        "score": report.capability.score if report.capability else None,
        "recommended_fetch": report.fetch.recommended.value if report.fetch else None,
    }))
    return _report_to_response(report, settings)


def _report_to_response(report: PreflightReport, settings: Settings) -> PreflightReportResponse:
    now = datetime.now(timezone.utc)
    data = asdict(report)
    data["capability"]["confidence"] = report.capability.confidence.value
    data["fetch"]["recommended"] = report.fetch.recommended.value
    data["preflight_id"] = uuid.uuid4()
    data["created_at"] = now
    data["expires_at"] = now + timedelta(hours=settings.preflight_report_ttl_hours)
    return PreflightReportResponse.model_validate(data)
