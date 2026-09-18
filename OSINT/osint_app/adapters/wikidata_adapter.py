"""Structured person/organization lookup via Wikidata
(https://www.wikidata.org) -- free, keyless, no CAPTCHA, and explicitly
automation-friendly per its own robots.txt (unlike every Indian government
portal checked for this project: MCA, eCourts, GST and data.gov.in are all
either CAPTCHA-gated or actively bot-blocked -- see README's source
feasibility matrix). The one dedicated PERSON_NAME adapter in Phase 1.

Deliberately conservative, per spec: a name match on Wikidata is evidence
that *some* notable person or organization has that exact name -- it is
NEVER treated as proof that the investigation subject IS that entity. Same
name is not the same person. Every result this adapter produces carries
SAME_NAME_MATCH_CONFIDENCE, set *below* the pivot engine's default
confidence threshold on purpose, so a Wikidata hit alone does not
automatically cascade into further investigation -- it only does if
corroborated by something else (e.g. the same website/handle also turns up
via public_web).

If a name has multiple notable exact-label matches (disambiguation -- e.g.
two different notable "Rahul Kumar"s), every one of them is returned as a
separate candidate. Silently picking "the most likely one" would be exactly
the kind of unproven identity merge the whole evidence model exists to avoid.
"""
from datetime import UTC, datetime

import httpx

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter, SourceUnavailable
from osint_app.config import settings
from osint_app.enums import ClaimType, EntityType

SEARCH_URL = "https://www.wikidata.org/w/api.php"
ENTITY_DATA_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
USER_AGENT = "Mozilla/5.0 (compatible; OSINT-Phase1/1.0; +wikidata-adapter)"

MAX_EXACT_MATCHES = 3
HUMAN_QID = "Q5"

P_INSTANCE_OF = "P31"
P_OFFICIAL_WEBSITE = "P856"
P_EMPLOYER = "P108"
P_TWITTER = "P2002"
P_INSTAGRAM = "P2003"
P_FACEBOOK = "P2013"
P_YOUTUBE_CHANNEL = "P2397"

# Deliberately below pivot_confidence_threshold's 0.5 default -- see module docstring.
SAME_NAME_MATCH_CONFIDENCE = 0.45
STRUCTURED_FACT_CONFIDENCE = 0.55  # a claim genuinely attached to the matched entity, not fabricated


class WikidataAdapter(SourceAdapter):
    name = "wikidata"
    accepts = EntityType.PERSON

    async def is_available(self) -> bool:
        return True  # free, keyless HTTP API -- run() surfaces any real failure as SourceUnavailable

    async def run(self, name: str) -> list[AdapterResult]:
        async with httpx.AsyncClient(timeout=settings.adapter_timeout_seconds, headers={"User-Agent": USER_AGENT}) as client:
            candidates = await self._search(client, name)
            exact_matches = [c for c in candidates if c["label"].strip().lower() == name.strip().lower()]

            results: list[AdapterResult] = []
            for candidate in exact_matches[:MAX_EXACT_MATCHES]:
                entity = await self._fetch_entity(client, candidate["id"])
                if entity is None:
                    continue
                results.extend(await self._build_results(client, name, candidate, entity))
            return results

    async def _search(self, client: httpx.AsyncClient, name: str) -> list[dict]:
        try:
            resp = await client.get(
                SEARCH_URL,
                params={
                    "action": "wbsearchentities", "search": name, "language": "en",
                    "type": "item", "format": "json", "limit": 10,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"Wikidata search request failed: {exc}") from exc

        payload = resp.json()
        return [
            {"id": item["id"], "label": item.get("label", ""), "description": item.get("description", "")}
            for item in payload.get("search", [])
        ]

    async def _fetch_entity(self, client: httpx.AsyncClient, qid: str) -> dict | None:
        try:
            resp = await client.get(ENTITY_DATA_URL.format(qid=qid))
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"Wikidata entity fetch failed for {qid}: {exc}") from exc
        return resp.json().get("entities", {}).get(qid)

    async def _resolve_label(self, client: httpx.AsyncClient, qid: str) -> str:
        entity = await self._fetch_entity(client, qid)
        if entity is None:
            return qid
        return entity.get("labels", {}).get("en", {}).get("value", qid)

    @staticmethod
    def _claim_values(entity: dict, prop: str) -> list:
        claims = entity.get("claims", {}).get(prop, [])
        values = []
        for c in claims:
            datavalue = c.get("mainsnak", {}).get("datavalue")
            if datavalue is not None:
                values.append(datavalue["value"])
        return values

    async def _build_results(
        self, client: httpx.AsyncClient, queried_name: str, candidate: dict, entity: dict
    ) -> list[AdapterResult]:
        observed_at = datetime.now(UTC)
        qid = candidate["id"]
        entity_page_url = f"https://www.wikidata.org/wiki/{qid}"
        instance_of = [v.get("id") for v in self._claim_values(entity, P_INSTANCE_OF) if isinstance(v, dict)]
        is_human = HUMAN_QID in instance_of
        matched_entity_type = EntityType.PERSON if is_human else EntityType.COMPANY

        results = [
            AdapterResult(
                source=self.name,
                query=queried_name,
                entity_type=matched_entity_type,
                value=f"{candidate['label']} ({qid})",
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.PUBLIC_ASSOCIATION,
                confidence=SAME_NAME_MATCH_CONFIDENCE,
                observed_at=observed_at,
                evidence=AdapterEvidence(
                    url=entity_page_url,
                    title=f"Wikidata: {candidate['label']}",
                    metadata={
                        "snippet": f"{candidate['label']} -- {candidate['description']}",
                        "wikidata_id": qid,
                        "is_human": is_human,
                        "match_type": "exact_label",
                    },
                ),
                extraction_method="wikidata_exact_label_match",
            )
        ]

        for website in self._claim_values(entity, P_OFFICIAL_WEBSITE):
            results.append(
                AdapterResult(
                    source=self.name,
                    query=queried_name,
                    entity_type=EntityType.URL,
                    value=website,
                    status=AdapterStatus.FOUND,
                    claim_type=ClaimType.PUBLIC_ASSOCIATION,
                    confidence=STRUCTURED_FACT_CONFIDENCE,
                    observed_at=observed_at,
                    evidence=AdapterEvidence(
                        url=website,
                        title=f"Official website of {candidate['label']}",
                        metadata={"snippet": f"Official website (P856) of {candidate['label']}", "wikidata_id": qid},
                    ),
                    extraction_method="wikidata_claim_P856",
                )
            )

        social_props = {P_TWITTER: "twitter.com", P_INSTAGRAM: "instagram.com", P_FACEBOOK: "facebook.com"}
        for prop, domain in social_props.items():
            for handle in self._claim_values(entity, prop):
                if not isinstance(handle, str):
                    continue
                profile_url = f"https://{domain}/{handle}"
                results.append(
                    AdapterResult(
                        source=self.name,
                        query=queried_name,
                        entity_type=EntityType.SOCIAL_PROFILE,
                        value=profile_url,
                        status=AdapterStatus.FOUND,
                        claim_type=ClaimType.PUBLIC_ASSOCIATION,
                        confidence=STRUCTURED_FACT_CONFIDENCE,
                        observed_at=observed_at,
                        evidence=AdapterEvidence(
                            url=profile_url,
                            title=f"{domain} profile of {candidate['label']}",
                            metadata={"snippet": f"{domain} handle from Wikidata claim", "wikidata_id": qid},
                        ),
                        extraction_method=f"wikidata_claim_{prop}",
                    )
                )
        for channel_id in self._claim_values(entity, P_YOUTUBE_CHANNEL):
            profile_url = f"https://www.youtube.com/channel/{channel_id}"
            results.append(
                AdapterResult(
                    source=self.name, query=queried_name, entity_type=EntityType.SOCIAL_PROFILE, value=profile_url,
                    status=AdapterStatus.FOUND, claim_type=ClaimType.PUBLIC_ASSOCIATION,
                    confidence=STRUCTURED_FACT_CONFIDENCE, observed_at=observed_at,
                    evidence=AdapterEvidence(
                        url=profile_url, title=f"YouTube channel of {candidate['label']}",
                        metadata={"snippet": "YouTube channel from Wikidata claim", "wikidata_id": qid},
                    ),
                    extraction_method="wikidata_claim_P2397",
                )
            )

        if is_human:
            for employer_ref in self._claim_values(entity, P_EMPLOYER):
                if not isinstance(employer_ref, dict) or "id" not in employer_ref:
                    continue
                employer_label = await self._resolve_label(client, employer_ref["id"])
                results.append(
                    AdapterResult(
                        source=self.name,
                        query=queried_name,
                        entity_type=EntityType.COMPANY,
                        value=employer_label,
                        status=AdapterStatus.FOUND,
                        claim_type=ClaimType.PUBLIC_ASSOCIATION,
                        confidence=STRUCTURED_FACT_CONFIDENCE,
                        observed_at=observed_at,
                        evidence=AdapterEvidence(
                            url=f"https://www.wikidata.org/wiki/{employer_ref['id']}",
                            title=f"Employer of {candidate['label']}",
                            metadata={"snippet": f"Employer (P108): {employer_label}", "wikidata_id": qid},
                        ),
                        extraction_method="wikidata_claim_P108",
                    )
                )

        return results
