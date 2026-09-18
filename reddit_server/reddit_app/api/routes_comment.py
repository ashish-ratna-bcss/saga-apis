"""Single-comment lookup by id."""

from __future__ import annotations

from fastapi import APIRouter

from reddit_app.api.deps import ProviderServiceDep
from reddit_app.schemas.comment import CommentIdentifierRequest, CommentOut

router = APIRouter(prefix="/api/reddit", tags=["reddit"])


@router.post("/comment", response_model=CommentOut, summary="Comment details by comment_id")
async def comment_details(payload: CommentIdentifierRequest, service: ProviderServiceDep) -> CommentOut:
    return CommentOut(**await service.comment_details(payload.comment_id))
