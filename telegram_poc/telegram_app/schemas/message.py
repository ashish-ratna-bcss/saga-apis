from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    telegram_message_id: int
    sender_id: str | None
    sender_username: str | None
    sender_display_name: str | None
    message_date: datetime | None
    edit_date: datetime | None
    text: str | None
    views: int | None
    forwards: int | None
    reply_count: int | None
    media_type: str | None
    source_url: str | None
    collected_at: datetime
    raw_data_hash: str | None
    processing_status: str


class MessageSearchQuery(BaseModel):
    keyword: str | None = None
    source_id: int | None = None
    sender_username: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    media_type: str | None = None
    limit: int = 50
    offset: int = 0
