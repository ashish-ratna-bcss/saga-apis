"""Phone technical intelligence via the `phonenumbers` library.

phonenumbers is a local, offline library (a port of Google's libphonenumber).
It can tell you the carrier/region *assigned to the number range* -- it
cannot and does not tell you who owns the number. That distinction is
encoded via ClaimType.TECHNICAL on every result this adapter emits.
"""
from datetime import UTC, datetime

import phonenumbers
from phonenumbers import carrier as pn_carrier
from phonenumbers import geocoder as pn_geocoder
from phonenumbers import timezone as pn_timezone

from osint_app.adapters.base import AdapterEvidence, AdapterResult, AdapterStatus, SourceAdapter
from osint_app.enums import ClaimType, EntityType

_LINE_TYPE_NAMES = {
    0: "FIXED_LINE",
    1: "MOBILE",
    2: "FIXED_LINE_OR_MOBILE",
    3: "TOLL_FREE",
    4: "PREMIUM_RATE",
    5: "SHARED_COST",
    6: "VOIP",
    7: "PERSONAL_NUMBER",
    8: "PAGER",
    9: "UAN",
    10: "VOICEMAIL",
    99: "UNKNOWN",
}


def generate_search_queries(e164_number: str) -> list[str]:
    """Query variants a human investigator (or PublicWebAdapter) would run.
    Pure string generation -- no network calls, no execution here."""
    national = e164_number[3:] if e164_number.startswith("+91") else e164_number.lstrip("+")
    variants = {
        f'"{e164_number}"',
        f'"{national}"',
        f'"{e164_number}" whatsapp',
        f'"{national}" truecaller',
        f'"{national}" contact',
    }
    return sorted(variants)


class PhoneAdapter(SourceAdapter):
    name = "phonenumbers"
    accepts = EntityType.PHONE

    async def is_available(self) -> bool:
        return True  # pure offline library, always available

    async def run(self, normalized_identifier: str) -> list[AdapterResult]:
        parsed = phonenumbers.parse(normalized_identifier, None)
        region = phonenumbers.region_code_for_number(parsed)
        carrier_name = pn_carrier.name_for_number(parsed, "en") or None
        location = pn_geocoder.description_for_number(parsed, "en") or None
        line_type = phonenumbers.number_type(parsed)
        timezones = pn_timezone.time_zones_for_number(parsed)

        metadata = {
            "region_code": region,
            "country_code": parsed.country_code,
            "carrier": carrier_name,
            "location": location,
            "line_type": _LINE_TYPE_NAMES.get(line_type, "UNKNOWN"),
            "timezones": list(timezones),
            "is_valid": phonenumbers.is_valid_number(parsed),
            "is_possible": phonenumbers.is_possible_number(parsed),
        }
        search_queries = generate_search_queries(normalized_identifier)
        metadata["suggested_search_queries"] = search_queries

        return [
            AdapterResult(
                source=self.name,
                query=normalized_identifier,
                entity_type=EntityType.PHONE,
                value=normalized_identifier,
                status=AdapterStatus.FOUND,
                claim_type=ClaimType.TECHNICAL,
                confidence=1.0,  # deterministic library output, not a probabilistic claim
                observed_at=datetime.now(UTC),
                evidence=AdapterEvidence(url=None, title="phonenumbers technical lookup", metadata=metadata),
                extraction_method="phonenumbers_lib",
            )
        ]
