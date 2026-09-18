from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MonitoringStatusOut(BaseModel):
    source_id: int
    monitoring_enabled: bool
    access_status: str
    monitoring_started_at: datetime | None
    last_collected_at: datetime | None
    last_message_id: int | None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    source_id: int | None
    telegram_entity_id: str | None
    source_name: str | None
    previous_status: str | None
    new_status: str | None
    payload: dict
    is_read: bool
    created_at: datetime
