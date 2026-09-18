"""Telegram connection status + non-interactive login flow.

Never returns session data or api_hash - see authentication.get_status /
authentication._mask_phone.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from telegram_app.api.deps import get_client_manager_dep
from telegram_app.schemas.auth import AuthStepResponse, SendCodeRequest, TelegramStatusResponse, VerifyCodeRequest, VerifyPasswordRequest
from telegram_app.telegram import authentication
from telegram_app.telegram.client import TelegramClientManager

router = APIRouter(prefix="/api/telegram", tags=["telegram-auth"])


@router.get("/status", response_model=TelegramStatusResponse)
async def get_status(client_manager: TelegramClientManager = Depends(get_client_manager_dep)):
    status = await authentication.get_status(client_manager)
    return status


@router.post("/auth/send-code", response_model=AuthStepResponse)
async def send_code(payload: SendCodeRequest, client_manager: TelegramClientManager = Depends(get_client_manager_dep)):
    result = await authentication.send_code(client_manager, payload.phone)
    return AuthStepResponse(ok=result.ok, requires_password=result.requires_password, error=result.error)


@router.post("/auth/verify-code", response_model=AuthStepResponse)
async def verify_code(payload: VerifyCodeRequest, client_manager: TelegramClientManager = Depends(get_client_manager_dep)):
    result = await authentication.verify_code(client_manager, payload.phone, payload.code)
    return AuthStepResponse(ok=result.ok, requires_password=result.requires_password, error=result.error)


@router.post("/auth/verify-password", response_model=AuthStepResponse)
async def verify_password(payload: VerifyPasswordRequest, client_manager: TelegramClientManager = Depends(get_client_manager_dep)):
    result = await authentication.verify_password(client_manager, payload.password)
    return AuthStepResponse(ok=result.ok, requires_password=result.requires_password, error=result.error)
