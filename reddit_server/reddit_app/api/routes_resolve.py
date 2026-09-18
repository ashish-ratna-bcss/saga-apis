"""POST /api/reddit/resolve -- classify a Reddit URL as subreddit/post/comment and
return the relevant normalized objects in one call."""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.resolve import ResolveRequest, ResolveResponse

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


@router.post("/resolve", response_model=ResolveResponse, summary="Resolve a Reddit URL")
async def resolve(payload: ResolveRequest, service: ProviderServiceDep) -> ResolveResponse:
    return ResolveResponse(**await service.resolve(payload.url))
