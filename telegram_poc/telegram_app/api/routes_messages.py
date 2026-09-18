"""Reading collected messages + application-level search.

STORED/LOCAL only: every endpoint here reads the locally persisted
telegram_messages table (rows written by monitoring/backfill/search-result-
save) - none of them ever calls Telegram. For a live query against Telegram
itself, see app/api/routes_search.py (GET /api/telegram/search/global, GET
/api/sources/{source_id}/search, GET /api/telegram/channels/{id}/search).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from telegram_app.api.deps import get_message_service
from telegram_app.schemas.message import MessageOut
from telegram_app.services.message_service import MessageService

router = APIRouter(tags=["messages"])


@router.get(
    "/api/sources/{source_id}/messages",
    response_model=list[MessageOut],
    summary="Locally stored messages for one source",
    description="STORED/LOCAL search - reads telegram_messages, never Telegram. For a live query against this source, see GET /api/sources/{source_id}/search.",
)
async def list_source_messages(
    source_id: int,
    limit: int = 50,
    offset: int = 0,
    keyword: str | None = None,
    sender_username: str | None = None,
    media_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    service: MessageService = Depends(get_message_service),
):
    return await service.list_for_source(
        source_id,
        limit=limit,
        offset=offset,
        keyword=keyword,
        sender_username=sender_username,
        media_type=media_type,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/api/messages/{message_id}", response_model=MessageOut)
async def get_message(message_id: int, service: MessageService = Depends(get_message_service)):
    message = await service.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="message not found")
    return message


@router.get(
    "/api/messages",
    response_model=list[MessageOut],
    summary="Search locally stored messages",
    description="STORED/LOCAL search - reads telegram_messages, never Telegram. For a live cross-channel query against Telegram, see GET /api/telegram/search/global.",
)
async def search_messages(
    keyword: str | None = None,
    source_id: int | None = None,
    sender_username: str | None = None,
    media_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    service: MessageService = Depends(get_message_service),
):
    return await service.search(
        keyword=keyword,
        source_id=source_id,
        sender_username=sender_username,
        media_type=media_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
