"""Free domain DNS/WHOIS recon -- ported from the original implementation's
run_domain_whois_recon + run_mx_security_check. Bundled into one adapter
(one source, multiple technical facts) the same way phone_adapter.py bundles
its phonenumbers facts into a single result. Keyless, subprocess-based
(nslookup/whois), so it fills root's previously-empty DOMAIN adapter slot
with zero external dependencies.
"""
import asyncio
import shutil
import subprocess
from datetime import UTC, datetime

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.enums import ClaimType, EntityType

_SUBPROCESS_TIMEOUT = 10


def _nslookup(record_type: str, target: str) -> str:
    proc = subprocess.run(
        ["nslookup", f"-type={record_type}", target], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT
    )
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _txt_records(target: str) -> list[str]:
    records = []
    for line in _nslookup("txt", target).splitlines():
        stripped = line.strip()
        if "text =" in stripped.lower():
            _, _, value = stripped.partition("=")
            records.append(value.strip().strip('"'))
    return records


def _run_recon_sync(domain: str) -> dict:
    ns_records = []
    for line in _nslookup("ns", domain).splitlines():
        if "nameserver" in line.lower():
            _, _, value = line.partition("=")
            if value:
                ns_records.append(value.strip().rstrip("."))

    mx_records = []
    for line in _nslookup("mx", domain).splitlines():
        if "mail exchanger" in line.lower():
            _, _, value = line.partition("=")
            hostname = value.strip().rstrip(".").split()[-1] if value.strip() else ""
            if hostname:
                mx_records.append(hostname)

    txt_records = _txt_records(domain)
    spf_records = [r for r in txt_records if "v=spf1" in r.lower()]
    dmarc_records = [r for r in _txt_records(f"_dmarc.{domain}") if "v=dmarc1" in r.lower()]

    whois_text = ""
    if shutil.which("whois"):
        try:
            proc = subprocess.run(["whois", domain], capture_output=True, text=True, timeout=15)
            whois_text = (proc.stdout or "").strip()
        except (subprocess.TimeoutExpired, OSError):
            whois_text = ""

    return {
        "domain": domain,
        "ns_records": sorted(set(ns_records)),
        "mx_records": sorted(set(mx_records)),
        "spf_present": len(spf_records) > 0,
        "spf_records": spf_records,
        "dmarc_present": len(dmarc_records) > 0,
        "dmarc_records": dmarc_records,
        "whois_available": bool(whois_text),
        "whois_preview": whois_text[:1200] if whois_text else None,
    }


class DomainReconAdapter(SourceAdapter):
    name = "domain_recon"
    accepts = EntityType.DOMAIN

    async def is_available(self) -> bool:
        return shutil.which("nslookup") is not None

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        if not await self.is_available():
            raise SourceUnavailable("nslookup is not installed")

        try:
            metadata = await asyncio.wait_for(
                asyncio.to_thread(_run_recon_sync, normalized_identifier), timeout=45
            )
        except TimeoutError as exc:
            raise SourceUnavailable("domain recon exceeded time budget") from exc
        except Exception as exc:
            raise SourceUnavailable(f"domain recon failed: {exc}") from exc

        return [
            AdapterResult(
                source=self.name,
                query=normalized_identifier,
                entity_type=EntityType.DOMAIN,
                value=normalized_identifier,
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.TECHNICAL,
                confidence=1.0,
                observed_at=datetime.now(UTC),
                evidence=AdapterEvidence(
                    url=None, title="DNS/WHOIS technical lookup", metadata=metadata
                ),
                extraction_method="nslookup_whois",
            )
        ]
