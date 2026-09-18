from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceCreate(BaseModel):
    identifier: str = Field(..., description="@username, t.me URL/invite link, or numeric Telegram ID")
    source_type: str | None = Field(default=None, description="Hint only - the real type is set from resolution")
    monitoring_enabled: bool = Field(default=True)


class SourceUpdate(BaseModel):
    monitoring_enabled: bool | None = None
    title: str | None = None


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    identifier: str
    telegram_entity_id: str | None
    username: str | None
    title: str | None
    source_type: str
    access_status: str
    status_reason: str | None
    monitoring_enabled: bool
    monitoring_started_at: datetime | None
    last_status_check_at: datetime | None
    next_status_check_at: datetime | None
    last_message_id: int | None
    last_collected_at: datetime | None
    last_probe_status: str | None = None
    last_probe_reason: str | None = None
    last_probe_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AccessStatusOut(BaseModel):
    """`access_status` is the operational status (e.g. MONITORING); the
    `last_probe_*` fields are the most recent live-probe result, recorded
    independently - a probe finding PUBLIC_ACCESSIBLE never overwrites an
    operational MONITORING status, so both are surfaced separately here
    rather than conflated into one field."""

    source_id: int
    access_status: str
    status_reason: str | None
    last_status_check_at: datetime | None
    next_status_check_at: datetime | None
    last_probe_status: str | None = None
    last_probe_reason: str | None = None
    last_probe_at: datetime | None = None
