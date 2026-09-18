from __future__ import annotations

from pydantic import BaseModel


class SendCodeRequest(BaseModel):
    phone: str


class VerifyCodeRequest(BaseModel):
    phone: str
    code: str


class VerifyPasswordRequest(BaseModel):
    password: str


class AuthStepResponse(BaseModel):
    ok: bool
    requires_password: bool = False
    error: str | None = None


class AccountInfo(BaseModel):
    id: str | None
    username: str | None
    phone: str | None


class TelegramStatusResponse(BaseModel):
    connected: bool
    authenticated: bool
    account: AccountInfo | None = None
    error: str | None = None
    session_invalid: bool = False
