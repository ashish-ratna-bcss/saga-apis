"""Shared response shapes: the error envelope and cursor-paginated lists."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str = Field(..., description="Stable machine-readable error code, e.g. SUBREDDIT_NOT_FOUND.")
    message: str = Field(..., description="Human-readable error message.")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"code": "REDDIT_API_ERROR", "message": "Human-readable error"}
        }
    )


class ErrorResponse(BaseModel):
    """The wire shape of every non-2xx response from this service."""

    error: ErrorDetail


class CursorPage(BaseModel, Generic[T]):
    """The pagination envelope every list endpoint returns."""

    items: list[T]
    cursor: str | None = Field(
        default=None, description="Opaque cursor for the next page, or null when exhausted."
    )
