"""Service liveness/readiness endpoints.

Both are pure process checks -- no Reddit call, no credentials to be "configured"
(this service only ever talks to Reddit's public, unauthenticated .rss endpoints),
never fail, never require auth. Kept open for load balancers and uptime probes.
"""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.schemas.health import HealthResponse, ReadyResponse

router = APIRouter(tags=["health"])

SERVICE_VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse, summary="Process liveness")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse, summary="Process readiness")
async def ready() -> ReadyResponse:
    return ReadyResponse(status="ok")
