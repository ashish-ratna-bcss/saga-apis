"""Explicit Telegram bot-start interaction and invite-link join.

Both endpoints here are operator-initiated actions with no automatic or
scheduled equivalent - see app/services/bot_service.py and
SourceService.join_by_invite for the orchestration, which is built entirely
on the existing SourceService/AccessManager/state machine/audit service/URL
classifier rather than a parallel implementation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from telegram_app.api.deps import get_bot_service, get_source_service
from telegram_app.schemas.bot import (
    BotButtonOut,
    DiscoveredSourceOut,
    JoinByInviteRequest,
    JoinByInviteResponse,
    StartBotResponse,
)
from telegram_app.services.bot_service import BotNotFoundError, BotService, SourceIsNotABotError
from telegram_app.services.source_service import SourceService

router = APIRouter(prefix="/api/telegram", tags=["telegram-bots"])


@router.post(
    "/bots/{source_id}/start",
    response_model=StartBotResponse,
    summary="Start a registered Telegram bot (sends /start, waits for its reply)",
    description=(
        "The API equivalent of pressing 'Start Bot' in the Telegram app: sends exactly one "
        "/start message via the existing authenticated session and waits, bounded by "
        "BOT_START_TIMEOUT_SECONDS, for the bot's reply. The reply's text and any inline URL "
        "buttons are scanned for Telegram links only (t.me/telegram.me) - every other URL is "
        "classified and discarded, never fetched. Any Telegram channel/group/invite found is "
        "registered as a DORMANT source (monitoring disabled) for the operator to act on via "
        "the existing POST /api/sources/{id}/request-access and .../monitoring/start - this "
        "endpoint never joins or starts monitoring anything on its own. 404 if the source "
        "doesn't exist; 422 if it exists but isn't a Telegram bot."
    ),
)
async def start_bot(source_id: int, service: BotService = Depends(get_bot_service)):
    try:
        result = await service.start_bot(source_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SourceIsNotABotError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return StartBotResponse(
        source=result.source,
        status=result.status,
        response_text=result.response_text,
        response_message_id=result.response_message_id,
        response_date=result.response_date,
        buttons=[BotButtonOut(text=b.text, url=b.url) for b in result.buttons],
        discovered=[
            DiscoveredSourceOut(
                source_id=d.source_id, identifier=d.identifier, access_status=d.access_status,
                source_type=d.source_type, already_existed=d.already_existed, raw_url=d.raw_url,
            )
            for d in result.discovered
        ],
        error=result.error,
    )


@router.post(
    "/invites/join",
    response_model=JoinByInviteResponse,
    summary="Join (or request to join) a Telegram source by invite link",
    description=(
        "Explicit join by a raw Telegram invite link (t.me/+hash or t.me/joinchat/hash) - not "
        "for plain @usernames, which go through POST /api/sources + .../request-access instead. "
        "Reuses the existing register/dedup + request_access path in full: an already-accessible "
        "source is reported as already_member with no RPC sent; a private source with approval "
        "required is submitted exactly once and reported pending_approval / already_pending on "
        "repeat calls - never a duplicate join attempt."
    ),
)
async def join_by_invite(payload: JoinByInviteRequest, service: SourceService = Depends(get_source_service)):
    try:
        outcome = await service.join_by_invite(payload.identifier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JoinByInviteResponse(source=outcome.source, status=outcome.status, reason=outcome.reason)
