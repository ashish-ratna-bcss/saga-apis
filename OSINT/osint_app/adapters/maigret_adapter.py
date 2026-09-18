"""Username presence via maigret. Unlike sherlock, maigret's checker is
natively asyncio/aiohttp-based, so it runs directly on the event loop --
no thread offload needed. Uses maigret's own bundled site database (no
network fetch required just to get the site list).
"""
import asyncio
import logging
import threading
from datetime import UTC, datetime

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.config import settings
from osint_app.enums import ClaimType, EntityType

FOUND_CONFIDENCE = 0.55

_db_cache = None
_db_lock = threading.Lock()
_logger = logging.getLogger("maigret_adapter")
_logger.addHandler(logging.NullHandler())


def _get_database():
    global _db_cache
    with _db_lock:
        if _db_cache is None:
            from maigret.db_updater import BUNDLED_DB_PATH
            from maigret.sites import MaigretDatabase

            _db_cache = MaigretDatabase().load_from_path(BUNDLED_DB_PATH)
        return _db_cache


class MaigretAdapter(SourceAdapter):
    name = "maigret"
    accepts = EntityType.USERNAME

    async def is_available(self) -> bool:
        try:
            import maigret.checking  # noqa: F401
        except ImportError:
            return False
        return True

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        if not await self.is_available():
            raise SourceUnavailable("maigret is not installed")

        from maigret.checking import maigret as run_maigret

        try:
            db = await asyncio.to_thread(_get_database)
            site_dict = db.ranked_sites_dict(top=settings.maigret_top_sites)
            raw = await asyncio.wait_for(
                run_maigret(
                    username=normalized_identifier,
                    site_dict=site_dict,
                    logger=_logger,
                    timeout=min(settings.adapter_timeout_seconds, 15),
                    no_progressbar=True,
                ),
                timeout=180,
            )
        except TimeoutError as exc:
            raise SourceUnavailable("maigret scan exceeded overall time budget") from exc
        except Exception as exc:
            raise SourceUnavailable(f"maigret execution failed: {exc}") from exc

        observed_at = datetime.now(UTC)
        results: list[AdapterResult] = []
        for site_name, info in raw.items():
            status = info.get("status")
            if status is None or not status.is_found():
                continue
            url_user = info.get("url_user")
            results.append(
                AdapterResult(
                    source=self.name,
                    query=normalized_identifier,
                    entity_type=EntityType.SOCIAL_PROFILE,
                    value=url_user,
                    status=AdapterStatus.FOUND,
                    claim_type=ClaimType.PUBLIC_ASSOCIATION,
                    confidence=FOUND_CONFIDENCE,
                    observed_at=observed_at,
                    evidence=AdapterEvidence(
                        url=url_user,
                        title=f"{site_name} profile",
                        metadata={"site": site_name, "http_status": info.get("http_status")},
                    ),
                    extraction_method="maigret_module",
                )
            )
        return results
