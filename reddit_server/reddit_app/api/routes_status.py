"""GET /api/reddit/status -- live Reddit connection/authentication check."""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.status import StatusResponse

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


@router.get("/status", response_model=StatusResponse, summary="Reddit connection/auth status")
async def reddit_status(service: ProviderServiceDep) -> StatusResponse:
    return StatusResponse(**await service.status())
