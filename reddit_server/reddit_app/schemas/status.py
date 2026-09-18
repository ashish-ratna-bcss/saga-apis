"""GET /api/reddit/status response shape."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AccountSummary(BaseModel):
    username: str | None = None


class StatusResponse(BaseModel):
    connected: bool
    authorized: bool
    account: AccountSummary | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"connected": True, "authorized": True, "account": {"username": "j***e"}}
        }
    )
