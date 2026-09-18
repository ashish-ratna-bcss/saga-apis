"""User request/response shapes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    id: str | None = None
    username: str
    created_at: str | None = None
    link_karma: int = 0
    comment_karma: int = 0
    is_mod: bool = False

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "t2_user_id",
                "username": "someuser",
                "created_at": "2020-01-01T00:00:00Z",
                "link_karma": 1234,
                "comment_karma": 5678,
                "is_mod": False,
            }
        }
    )


class UsernameRequest(BaseModel):
    username: str = Field(..., description="e.g. 'someuser' or 'u/someuser'.")


class UserListingRequest(BaseModel):
    username: str
    limit: int | None = Field(default=None, ge=1, le=100)
    cursor: str | None = None
