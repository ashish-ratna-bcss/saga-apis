"""Application notifications (e.g. TELEGRAM_ACCESS_GRANTED)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from telegram_app.api.deps import get_notification_service
from telegram_app.schemas.monitoring import NotificationOut
from telegram_app.services.notification_service import NotificationService

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    limit: int = 50, offset: int = 0, unread_only: bool = False, service: NotificationService = Depends(get_notification_service)
):
    return await service.list_notifications(limit=limit, offset=offset, unread_only=unread_only)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(notification_id: int, service: NotificationService = Depends(get_notification_service)):
    notification = await service.mark_read(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return notification
