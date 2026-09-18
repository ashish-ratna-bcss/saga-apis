from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from osint_app.adapters.public_web_adapter import PublicWebAdapter
from osint_app.adapters.registry import all_adapters
from osint_app.db import get_db
from osint_app.models import SourceHealth
from osint_app.schemas import SourceHealthOut

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


def _compute_status(health: SourceHealth | None, installed: bool) -> str:
    """working | unavailable | disabled | degraded -- see STEP 9.
    `installed` here means "dependency/config present and reachable at last
    check", independent of whether it has ever actually run."""
    if health is not None and not health.enabled:
        return "disabled"
    if not installed:
        return "unavailable"
    if health is None or health.run_count == 0:
        return "working"  # installed, just never run yet -- optimistic until proven otherwise
    success_rate = health.success_count / health.run_count
    if success_rate == 0:
        return "unavailable"
    if success_rate < 0.5:
        return "degraded"
    return "working"


@router.get("/health", response_model=list[SourceHealthOut])
async def source_health(db: Session = Depends(get_db)):
    existing = {h.source_name: h for h in db.query(SourceHealth).all()}
    output = []
    for adapter in all_adapters():
        health = existing.get(adapter.name)
        if health is None:
            output.append(
                SourceHealthOut(
                    source_name=adapter.name,
                    enabled=True,
                    installed=False,
                    version=None,
                    last_success=None,
                    last_failure=None,
                    last_error=None,
                    success_count=0,
                    failure_count=0,
                    timeout_count=0,
                    success_rate=None,
                    average_duration_seconds=None,
                    run_count=0,
                    status=_compute_status(None, installed=False),
                )
            )
        else:
            item = SourceHealthOut.model_validate(health)
            item = item.model_copy(update={"status": _compute_status(health, health.installed)})
            output.append(item)

    # SearxNG backend itself -- a live reachability probe (reuses the exact
    # check PublicWebAdapter.is_available() does), separate from
    # `public_web`'s historical adapter run/success stats above: this row
    # answers "is the search backend reachable right now", not "how has the
    # public_web adapter performed over time".
    searxng_ok = await PublicWebAdapter().is_available()
    output.append(
        SourceHealthOut(
            source_name="searxng",
            enabled=True,
            installed=searxng_ok,
            version=None,
            last_success=None,
            last_failure=None,
            last_error=None if searxng_ok else "unreachable or OSINT_SEARXNG_URL not configured",
            success_count=0,
            failure_count=0,
            timeout_count=0,
            success_rate=None,
            average_duration_seconds=None,
            run_count=0,
            status="working" if searxng_ok else "unavailable",
        )
    )
    return output
