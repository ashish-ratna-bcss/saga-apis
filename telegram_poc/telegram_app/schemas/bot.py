"""Request/response schemas for the bot-start workflow and the explicit
invite-link join action (app/api/routes_bots.py)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from telegram_app.schemas.source import SourceOut


class BotButtonOut(BaseModel):
    text: str
    url: str | None = None


class DiscoveredSourceOut(BaseModel):
    source_id: int
    identifier: str
    access_status: str
    source_type: str
    already_existed: bool
    raw_url: str


class StartBotResponse(BaseModel):
    source: SourceOut
    status: str  # "started" | "timed_out" | "failed"
    response_text: str | None = None
    response_message_id: int | None = None
    response_date: datetime | None = None
    buttons: list[BotButtonOut] = []
    discovered: list[DiscoveredSourceOut] = []
    error: str | None = None


class JoinByInviteRequest(BaseModel):
    identifier: str  # t.me/+hash or t.me/joinchat/hash (or the bare hash)


class JoinByInviteResponse(BaseModel):
    source: SourceOut
    status: str  # "joined" | "already_member" | "pending_approval" | "already_pending" | "failed"
    reason: str | None = None
