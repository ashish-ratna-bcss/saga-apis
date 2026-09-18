from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BackfillRequest(BaseModel):
    from_date: datetime | None = None
    to_date: datetime | None = None
    limit: int = Field(default=1000, ge=1, le=10000)


class BackfillJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int
    status: str
    from_date: datetime | None
    to_date: datetime | None
    requested_limit: int
    messages_found: int
    messages_inserted: int
    messages_skipped: int
    checkpoint_message_id: int | None
    error_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
