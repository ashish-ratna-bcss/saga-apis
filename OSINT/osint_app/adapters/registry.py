from osint_app.adapters.base import SourceAdapter
from osint_app.adapters.document_adapter import DocumentAdapter
from osint_app.adapters.domain_recon_adapter import DomainReconAdapter
from osint_app.adapters.email_adapter import HoleheAdapter
from osint_app.adapters.maigret_adapter import MaigretAdapter
from osint_app.adapters.phone_adapter import PhoneAdapter
from osint_app.adapters.public_web_adapter import PublicWebAdapter
from osint_app.adapters.sherlock_adapter import SherlockAdapter
from osint_app.adapters.wikidata_adapter import WikidataAdapter
from osint_app.enums import IdentifierType

# Per-identifier-type adapters -- dispatched by orchestrator based on the
# investigation's identifier_type. Free-only by decision: no paid provider is
# wired in (see app/adapters/hunter_adapter.py etc. -- removed, not just
# unregistered; every provider tried was either UNCONFIGURED-until-a-key,
# NO_RESULT on every live test run so far, or (Hunter) structurally refused
# the personal-webmail addresses that are this project's actual targets --
# see memory for the full live-qualification history before re-adding one).
ADAPTERS_BY_IDENTIFIER_TYPE: dict[IdentifierType, list[SourceAdapter]] = {
    IdentifierType.PHONE: [PhoneAdapter()],
    IdentifierType.EMAIL: [HoleheAdapter()],
    IdentifierType.USERNAME: [SherlockAdapter(), MaigretAdapter()],
    IdentifierType.DOMAIN: [DomainReconAdapter()],
    IdentifierType.PERSON_NAME: [WikidataAdapter()],
}

# Runs for every investigation regardless of identifier_type (query text is
# generated per-identifier by the orchestrator).
PUBLIC_WEB_ADAPTER = PublicWebAdapter()

# Triggered by discovered URL entities (see document_mining.py), not by
# identifier_type -- a document isn't one of the five supported identifiers.
DOCUMENT_ADAPTER = DocumentAdapter()


def adapters_for(identifier_type: IdentifierType) -> list[SourceAdapter]:
    return list(ADAPTERS_BY_IDENTIFIER_TYPE.get(identifier_type, []))


def all_adapters() -> list[SourceAdapter]:
    """Every adapter this app knows about, for source-health enumeration."""
    seen: dict[str, SourceAdapter] = {}
    for adapters in ADAPTERS_BY_IDENTIFIER_TYPE.values():
        for adapter in adapters:
            seen[adapter.name] = adapter
    seen[PUBLIC_WEB_ADAPTER.name] = PUBLIC_WEB_ADAPTER
    seen[DOCUMENT_ADAPTER.name] = DOCUMENT_ADAPTER
    return list(seen.values())
