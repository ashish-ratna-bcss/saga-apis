"""Service liveness and configuration endpoints.

``/health`` is pure process liveness (no Reddit call, never fails, never requires
auth) and ``/ready`` only checks that credentials are configured (also no network
call, so it stays fast/cheap for k8s-style readiness probes). Whether the configured
credentials actually work is a network fact, checked live by ``GET
/api/reddit/status`` (see routes_status.py).
"""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import SettingsDep
from reddit_app.schemas.health import HealthResponse, ReadyResponse

router = APIRouter(tags=["health"])

SERVICE_VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse, summary="Process liveness")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse, summary="Reddit provider configuration check")
async def ready(settings: SettingsDep) -> ReadyResponse:
    configured = settings.reddit_configured
    return ReadyResponse(status="ok" if configured else "not_configured", reddit_configured=configured)
