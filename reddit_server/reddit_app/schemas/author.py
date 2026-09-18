"""The nested author sub-object shared by Post and Comment."""

from __future__ import annotations

from pydantic import BaseModel


class AuthorOut(BaseModel):
    id: str | None = None
    username: str | None = None
