from fastapi import APIRouter, HTTPException

from osint_app.adapters.hibp_adapter import HibpUnavailable, check_pwned_password
from osint_app.schemas import PwnedPasswordRequest, PwnedPasswordResponse

router = APIRouter(prefix="/api/v1/utils", tags=["utils"])


@router.post("/pwned-password", response_model=PwnedPasswordResponse)
async def pwned_password(payload: PwnedPasswordRequest):
    """Standalone utility, NOT part of the identifier investigation pipeline
    -- password is not a supported identifier type. See hibp_adapter.py."""
    try:
        result = await check_pwned_password(payload.password)
    except (ValueError, HibpUnavailable) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PwnedPasswordResponse(pwned=result.pwned, times_seen=result.times_seen)
