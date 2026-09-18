"""Username presence via sherlock-project. Checks a username against
sherlock's community-maintained site list (fetched from its own GitHub repo,
cached in-process). Blocking library -- run via a thread, not natively async.
"""
import asyncio
import threading
from datetime import UTC, datetime

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.config import settings
from osint_app.enums import ClaimType, EntityType

FOUND_CONFIDENCE = 0.55

_sites_cache: dict[str, dict] | None = None
_sites_lock = threading.Lock()


def _get_site_data() -> dict[str, dict]:
    global _sites_cache
    with _sites_lock:
        if _sites_cache is None:
            from sherlock_project.sites import SitesInformation

            sites = SitesInformation()
            _sites_cache = {site.name: site.information for site in sites}
        return _sites_cache


def _run_sherlock_sync(username: str, timeout: int) -> dict:
    from sherlock_project.notify import QueryNotify
    from sherlock_project.sherlock import sherlock

    site_data = _get_site_data()
    return sherlock(username, site_data, QueryNotify(), timeout=timeout)


class SherlockAdapter(SourceAdapter):
    name = "sherlock"
    accepts = EntityType.USERNAME

    async def is_available(self) -> bool:
        try:
            import sherlock_project.sherlock  # noqa: F401
        except ImportError:
            return False
        return True

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        if not await self.is_available():
            raise SourceUnavailable("sherlock-project is not installed")

        per_request_timeout = min(settings.adapter_timeout_seconds, 15)
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_run_sherlock_sync, normalized_identifier, per_request_timeout),
                # Confirmed live (2026-09-09): fetching sherlock's own bundled
                # site list (SitesInformation(), no local cache -- see
                # _get_site_data above) alone took >120s in this environment,
                # before any of the 429 sites are even queried -- 180s total
                # was consistently exceeded before a single scan could
                # complete. Raised to accommodate a slow first fetch + full
                # scan; _sites_cache makes every call after the first in this
                # process much faster.
                timeout=420,
            )
        except TimeoutError as exc:
            raise SourceUnavailable("sherlock scan exceeded overall time budget") from exc
        except Exception as exc:
            raise SourceUnavailable(f"sherlock execution failed: {exc}") from exc

        from sherlock_project.result import QueryStatus

        observed_at = datetime.now(UTC)
        results: list[AdapterResult] = []
        for site_name, info in raw.items():
            status = info.get("status")
            if status is None or status.status != QueryStatus.CLAIMED:
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
                    extraction_method="sherlock_module",
                )
            )
        return results
