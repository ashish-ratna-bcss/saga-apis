from fastapi import APIRouter
from fastapi.responses import Response

from bluweb_app.core.metrics import render_metrics

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/live")
async def liveness() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> dict:
    # Stateless API service — ready if the process is up. No owned database.
    return {"status": "ready"}


@router.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)
