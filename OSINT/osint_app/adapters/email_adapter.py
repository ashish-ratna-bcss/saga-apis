"""Email OSINT via Holehe: probes public register/login endpoints across
~140 sites to test whether an email is registered there. Read-only response
inspection -- no login attempt, no password-recovery flows (explicitly
excluded below), no credential use. See APPROVED PHASE-1 TOOLS.
"""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from osint_app.adapters.base import (
    AdapterEvidence,
    AdapterResult,
    AdapterStatus,
    SourceAdapter,
    SourceUnavailable,
)
from osint_app.config import settings
from osint_app.enums import ClaimType, EntityType

FOUND_CONFIDENCE = 0.55  # single-source signal; final confidence is recomputed by the confidence engine


def _run_holehe_sync(email: str, timeout: int) -> list[dict]:
    """Runs holehe's own trio-based module runner in a plain thread (holehe
    uses trio, the rest of this app uses asyncio -- see run() below)."""
    import httpx
    import trio
    from holehe.core import get_functions, import_submodules, launch_module

    async def _main() -> list[dict]:
        modules = import_submodules("holehe.modules")
        websites = get_functions(modules, SimpleNamespace(nopasswordrecovery=True))
        client = httpx.AsyncClient(timeout=timeout)
        out: list[dict] = []
        async with trio.open_nursery() as nursery:
            for website in websites:
                nursery.start_soon(launch_module, website, email, client, out)
        await client.aclose()
        return out

    return trio.run(_main)


class HoleheAdapter(SourceAdapter):
    name = "holehe"
    accepts = EntityType.EMAIL

    async def is_available(self) -> bool:
        try:
            import holehe.core  # noqa: F401
            import trio  # noqa: F401
        except ImportError:
            return False
        return True

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        if not await self.is_available():
            raise SourceUnavailable("holehe or trio is not installed")

        try:
            raw_results = await asyncio.to_thread(
                _run_holehe_sync, normalized_identifier, settings.adapter_timeout_seconds
            )
        except Exception as exc:
            raise SourceUnavailable(f"holehe execution failed: {exc}") from exc

        observed_at = datetime.now(UTC)
        results: list[AdapterResult] = []
        for entry in raw_results:
            if not entry.get("exists"):
                continue
            domain = entry.get("domain", entry.get("name"))
            results.append(
                AdapterResult(
                    source=self.name,
                    query=normalized_identifier,
                    entity_type=EntityType.SOCIAL_PROFILE,
                    value=domain,
                    status=AdapterStatus.FOUND,
                    claim_type=ClaimType.PUBLIC_ASSOCIATION,
                    confidence=FOUND_CONFIDENCE,
                    observed_at=observed_at,
                    evidence=AdapterEvidence(
                        url=f"https://{domain}",
                        title=f"Account registered on {entry.get('name')}",
                        metadata={k: v for k, v in entry.items() if k not in {"name", "domain"}},
                    ),
                    extraction_method="holehe_module",
                )
            )
        return results
