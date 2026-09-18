"""Direct feature endpoints -- one identifier type each, synchronous (await
adapters directly in the request, no queue/poll needed), reusing the same
adapter registry + entity/evidence/confidence pipeline as
POST /investigations (see orchestrator.run_direct_lookup, which does the
actual work -- routes here only normalize input and shape the response). No
pivoting/recursion/document-mining: that's what /investigations is for.
These are a single leaf-level check against one identifier's own sources,
per "Do NOT duplicate business logic" -- everything below the normalize
call is the exact same adapter/evidence pipeline the full investigation
engine uses.
"""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from osint_app.confidence import compute_confidence
from osint_app.db import get_db
from osint_app.enums import IdentifierType
from osint_app.models import Entity, Evidence, Investigation, SearchJob
from osint_app.normalization import NormalizationError, normalize_identifier
from osint_app.orchestrator import run_direct_lookup
from osint_app.schemas import (
    DomainLookupRequest,
    EmailLookupRequest,
    EntityOut,
    EvidenceOut,
    JobOut,
    LookupResultOut,
    PersonLookupRequest,
    PhoneLookupRequest,
    UsernameLookupRequest,
)

router = APIRouter(prefix="/api/v1", tags=["lookups"])


def _site_key(url: str) -> str:
    """Sherlock and maigret confirm the same real site with structurally
    different URLs for the *same* username -- e.g. live-observed:
    hackerrank.com/darlaashishratna (sherlock) vs
    hackerrank.com/profile/darlaashishratna (maigret), or a bare vs
    /about-suffixed profile path on the same domain. Since each tool checks
    a given site at most once per username, the registrable host is the
    right merge key -- exact-URL matching would silently under-merge most
    real corroboration (caught by testing against real sherlock/maigret
    output before shipping this, not assumed)."""
    return urlparse(url).netloc.removeprefix("www.").lower() or url


def _merge_username_evidence(evidence: list[EvidenceOut]) -> list[EvidenceOut]:
    """Collapse hits sherlock and maigret both confirm on the same site into
    one response row instead of showing the same hit twice, surfacing every
    contributing source and re-running the confidence engine so a
    corroborated hit actually scores higher than a single-tool one. Only
    reshapes this endpoint's response, never the stored Evidence rows --
    /investigations and /report keep full per-source provenance."""
    merged: dict[str, EvidenceOut] = {}
    for item in evidence:
        key = _site_key(item.normalized_value)
        existing = merged.get(key)
        if existing is None:
            # raw_metadata always keyed by source name from the start, even
            # for a single-source hit -- otherwise a later merge would mix
            # this item's un-keyed metadata with a second source's keyed
            # metadata in the same dict (a real bug caught by this file's
            # own test -- see test_lookups_username_merge.py).
            merged[key] = item.model_copy(
                update={"raw_metadata": {item.source_name: item.raw_metadata}}
            )
            continue
        sources = sorted(set(existing.source_name.split("+")) | {item.source_name})
        merged[key] = existing.model_copy(
            update={
                "source_name": "+".join(sources),
                "confidence": compute_confidence(source_names=sources, exact_match=True).score,
                "raw_metadata": {**existing.raw_metadata, item.source_name: item.raw_metadata},
            }
        )
    return list(merged.values())


def _build_result(db: Session, investigation: Investigation) -> LookupResultOut:
    jobs = db.query(SearchJob).filter_by(investigation_id=investigation.id).all()
    entities = db.query(Entity).filter_by(investigation_id=investigation.id).all()
    evidence = db.query(Evidence).filter_by(investigation_id=investigation.id).all()
    evidence_out = [EvidenceOut.model_validate(e) for e in evidence]
    if investigation.identifier_type == IdentifierType.USERNAME:
        evidence_out = _merge_username_evidence(evidence_out)
    return LookupResultOut(
        investigation_id=investigation.id,
        input_identifier=investigation.input_identifier,
        identifier_type=investigation.identifier_type,
        normalized_identifier=investigation.normalized_identifier,
        status=investigation.status,
        jobs=[JobOut.model_validate(j) for j in jobs],
        entities=[EntityOut.model_validate(e) for e in entities],
        evidence=evidence_out,
    )


async def _do_lookup(
    db: Session, raw_identifier: str, identifier_type: IdentifierType, *, run_public_web: bool
) -> LookupResultOut:
    try:
        resolved_type, normalized = normalize_identifier(raw_identifier, identifier_type)
    except NormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    investigation = Investigation(
        input_identifier=raw_identifier, identifier_type=resolved_type, normalized_identifier=normalized, mode="quick"
    )
    db.add(investigation)
    db.commit()
    db.refresh(investigation)

    # run_direct_lookup opens its own session (SessionLocal()), same as
    # every other orchestrator entry point -- refresh() below picks up its
    # committed writes through this request's session.
    await run_direct_lookup(investigation.id, run_public_web=run_public_web)

    db.refresh(investigation)
    return _build_result(db, investigation)


@router.post("/phone/lookup", response_model=LookupResultOut)
async def phone_lookup(payload: PhoneLookupRequest, db: Session = Depends(get_db)):
    """Uses phonenumbers only (carrier/region, deterministic) -- for public
    co-occurrence hits too, use POST /investigations (mode=standard/deep)."""
    return await _do_lookup(db, payload.phone, IdentifierType.PHONE, run_public_web=False)


@router.post("/email/lookup", response_model=LookupResultOut)
async def email_lookup(payload: EmailLookupRequest, db: Session = Depends(get_db)):
    """Uses holehe only (account-existence probes across ~140 sites)."""
    return await _do_lookup(db, payload.email, IdentifierType.EMAIL, run_public_web=False)


@router.post("/username/lookup", response_model=LookupResultOut)
async def username_lookup(payload: UsernameLookupRequest, db: Session = Depends(get_db)):
    """Uses sherlock + maigret, run sequentially -- can take a couple of
    minutes for a real answer, expect the request to take a while, this
    endpoint doesn't queue/poll. Hits confirmed by both tools on the same
    URL are merged into one response row (see _merge_username_evidence)."""
    return await _do_lookup(db, payload.username, IdentifierType.USERNAME, run_public_web=False)


@router.post("/person/lookup", response_model=LookupResultOut)
async def person_lookup(payload: PersonLookupRequest, db: Session = Depends(get_db)):
    """Uses Wikidata (notable-exact-name-match structured facts) + public_web."""
    return await _do_lookup(db, payload.person_name, IdentifierType.PERSON_NAME, run_public_web=True)


@router.post("/domain/lookup", response_model=LookupResultOut)
async def domain_lookup(payload: DomainLookupRequest, db: Session = Depends(get_db)):
    """DNS/WHOIS recon + public_web (see README)."""
    return await _do_lookup(db, payload.domain, IdentifierType.DOMAIN, run_public_web=True)
